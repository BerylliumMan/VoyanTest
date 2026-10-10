# app/services/chat_run.py
# 034 对话内执行（US4）：
#   - start_run：复用既有批量执行语义（服务端 / 客户端 Agent），对话绑定项目
#   - watcher：轮询批次状态 → 运行卡片回填（进度/结果/失败证据）+ WS 推送
#   - 失败证据：run_logs 口径（失败步骤/断言文本/console 摘要/截图入口）
#   - 取消：复用既有批次 stop 语义
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app import crud
from app.config import get_settings
from app.crud import chat as chat_crud
from core.chat.events import emit
from core.chat.orchestrator import ActionOutcome, register_action_handler, serialize_message

logger = logging.getLogger(__name__)

_TERMINAL_BATCH_STATUS = ("passed", "failed", "partial", "cancelled")


# ----------------------------
# 纯函数（可单测）
# ----------------------------

def split_log_segments(message: str) -> list[str]:
    """run_logs 行内多段合并（` | ` 分隔）→ 独立证据段。

    032 起 console 条目会合并进失败步骤行（报告 UI 折叠口径），
    失败证据提取需要把它们拆开分别归类。
    """
    return [seg.strip() for seg in (message or "").split(" | ") if seg.strip()]


def build_failure_summary(steps: list[dict], *, console_max: int = 20) -> dict:
    """从步骤列表（run_logs 口径）提取失败证据。

    console 行不计入失败步骤（032 起 console 条目在运行收尾采集，
    否则会顶掉真正的失败步骤），只进 console 桶。
    """
    console = []
    failed = []
    for s in steps:
        desc = s.get("description") or ""
        if "[console]" in desc:
            console.append(desc)
            continue
        if not s.get("success"):
            failed.append(s)
    last_failed = failed[-1] if failed else None
    screenshot = None
    for s in reversed(steps):
        if s.get("screenshot_path"):
            screenshot = s["screenshot_path"]
            break
    return {
        "step": (last_failed or {}).get("description"),
        "screenshot_url": screenshot,
        "console": console[: max(1, console_max)],
        "failed_count": len(failed),
    }


def card_status_from_batch(batch_status: str) -> str:
    """批次状态 → 卡片状态。"""
    return {
        "running": "running",
        "paused": "paused",
        "passed": "done",
        "partial": "done",
        "failed": "failed",
        "cancelled": "canceled",
    }.get(batch_status, "running")


# ----------------------------
# 卡片读写
# ----------------------------

async def _find_run_card_message(db, conversation_id: int, batch_id: int):
    msgs = await chat_crud.list_messages_after(db, conversation_id)
    for m in reversed(msgs):
        for c in reversed(m.cards or []):
            if isinstance(c, dict) and c.get("type") == "run" and c.get("batch_id") == batch_id:
                return m, c
    return None, None


async def _write_run_card(db, message, card: dict) -> None:
    cards = []
    replaced = False
    for c in message.cards or []:
        if isinstance(c, dict) and c.get("type") == "run" and c.get("batch_id") == card.get("batch_id"):
            cards.append(card)
            replaced = True
        else:
            cards.append(c)
    if not replaced:
        cards.append(card)
    updated = await chat_crud.update_message_cards(db, message, cards)
    await emit(updated.conversation_id, {"type": "message.updated", "message": serialize_message(updated)})


# ----------------------------
# 发起执行
# ----------------------------

async def _load_user(db, user_id: int):
    from app import db_models

    return await db.get(db_models.User, user_id)


async def _pick_environment(db, project_id: int) -> Optional[int]:
    """服务端形态默认环境：项目第一个环境（没有则建默认）。"""
    try:
        envs = await crud.get_environments(db, project_id)
        if envs:
            return envs[0].id
        await crud.ensure_default_environment(db, project_id)
        envs = await crud.get_environments(db, project_id)
        return envs[0].id if envs else None
    except Exception:
        logger.debug("pick environment failed", exc_info=True)
        return None


async def _invoke_batch(
    db, *, user, case_ids: list[int], mode: str, agent_name: Optional[str],
    environment_id: Optional[int],
) -> int:
    """调用既有批量执行端点函数（server/client）；返回 batch_id。"""
    from fastapi import BackgroundTasks

    from app.routers.testcase.execution._schemas import BatchRunRequest

    if mode == "client":
        from app.routers.testcase.execution._client import batch_run_client
        from app.routers.testcase.execution._schemas import BatchCaseIdsRequest

        result = await batch_run_client(
            BatchCaseIdsRequest(
                case_ids=case_ids, agent_name=agent_name or "",
                init_case_ids=[], environment_id=environment_id, init_policy="before_each",
            ),
            user=user, db=db,
        )
    else:
        from app.routers.testcase.execution._server import batch_run_cases

        result = await batch_run_cases(
            BatchRunRequest(case_ids=case_ids, environment_id=environment_id),
            background_tasks=BackgroundTasks(),
            user=user, db=db,
        )
    return int(result["batch_id"])


async def _active_run_count(db, conversation_id: int) -> int:
    msgs = await chat_crud.list_messages_after(db, conversation_id)
    n = 0
    for m in msgs[-100:]:
        for c in (m.cards or []):
            if isinstance(c, dict) and c.get("type") == "run" and c.get("status") in ("running", "paused"):
                n += 1
    return n


async def start_run(
    db,
    *,
    conversation,
    case_ids: list[int],
    mode: str,
    agent_id: Optional[int],
    user_id: int,
) -> tuple[Optional[dict], str, Optional[str]]:
    """发起对话内执行；返回 (card, error, task_id)。"""
    project_id = getattr(conversation, "project_id", None)
    if project_id is None:
        return None, "对话未绑定项目", None

    settings = get_settings()
    limit = int(getattr(settings, "chat_max_parallel_runs", 3) or 3)
    if await _active_run_count(db, conversation.id) >= limit:
        return None, f"并行执行已达上限（{limit}），请等待现有执行完成", None

    # 用例校验（存在 + 同项目）
    cases = []
    for cid in case_ids[:20]:
        tc = await crud.get_test_case(db, cid)
        if tc is None:
            return None, f"用例 #{cid} 不存在", None
        if tc.project_id != project_id:
            return None, f"用例 #{cid} 不属于当前对话绑定的项目", None
        cases.append(tc)
    if not cases:
        return None, "没有可执行的用例", None

    agent_name = None
    if mode == "client":
        if not agent_id:
            return None, "客户端形态需要指定 Agent", None
        from app import db_models

        agent = await db.get(db_models.Agent, agent_id)
        if agent is None:
            return None, f"客户端 Agent #{agent_id} 不存在", None
        agent_name = agent.name

    environment_id = await _pick_environment(db, project_id)

    user = await _load_user(db, user_id)
    if user is None:
        return None, "用户不存在", None

    try:
        batch_id = await _invoke_batch(
            db, user=user, case_ids=[c.id for c in cases], mode=mode,
            agent_name=agent_name, environment_id=environment_id,
        )
    except Exception as exc:
        logger.exception("chat run invoke failed")
        detail = getattr(exc, "detail", None) or str(exc)
        return None, f"发起执行失败：{detail}", None

    card = {
        "type": "run",
        "batch_id": batch_id,
        "mode": mode,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "case_ids": [c.id for c in cases],
        "case_titles": [c.name for c in cases],
        "status": "running",
        "progress": {"total": len(cases), "done": 0},
        "result": None,
        "failures": [],
    }
    asyncio.create_task(_watch_run(conversation.id, batch_id))
    return card, "", str(batch_id)


# ----------------------------
# 状态 watcher
# ----------------------------

async def _collect_evidence(db, batch_id: int, case_ids: list[int]) -> list[dict]:
    """失败证据：按失败 run 汇总（失败步骤/断言文本/console/截图）。"""
    from sqlalchemy import select as _select

    from app import db_models

    settings = get_settings()
    console_max = int(getattr(settings, "chat_evidence_console_max", 20) or 20)
    failures = []
    rows = (
        await db.execute(
            _select(db_models.TestRun).where(
                db_models.TestRun.batch_id == batch_id,
                db_models.TestRun.status == "failed",
            )
        )
    ).scalars().all()
    for run in rows[:10]:
        logs = (
            await db.execute(
                _select(db_models.RunLog).where(db_models.RunLog.run_id == run.id).order_by(db_models.RunLog.id)
            )
        ).scalars().all()
        steps = []
        for lg in logs:
            level_ok = lg.level in ("info", "success", "passed")
            for seg in split_log_segments(lg.message or ""):
                is_console = "[console]" in seg
                steps.append({
                    "success": level_ok or is_console,
                    "description": seg,
                    "screenshot_path": lg.screenshot_path,
                })
        summary = build_failure_summary(steps, console_max=console_max)
        case_name = None
        try:
            detail = await crud.get_run_detail_with_case(db, run.id)
            if detail:
                case_name = detail[1]
        except Exception:
            pass
        failures.append({
            "case_id": run.case_id,
            "case_title": case_name or run.display_name or f"用例 #{run.case_id}",
            "step": summary["step"],
            "screenshot_url": summary["screenshot_url"],
            "console": summary["console"],
            "failed_count": summary["failed_count"],
        })
    return failures


async def _watch_run(conversation_id: int, batch_id: int, *, max_iterations: int = 900) -> None:
    from app.database import AsyncSessionLocal

    for _ in range(max_iterations):
        await asyncio.sleep(2.0)
        try:
            async with AsyncSessionLocal() as db:
                batch = await crud.get_run_batch(db, batch_id)
                if batch is None:
                    return
                # 触发状态计算（保持与报告页一致的口径）
                try:
                    from app.crud.run import _compute_batch_status

                    await _compute_batch_status(db, batch)
                    await db.commit()
                except Exception:
                    logger.debug("compute batch status failed", exc_info=True)

                msg, card = await _find_run_card_message(db, conversation_id, batch_id)
                if msg is None or card is None:
                    return

                status = batch.status or "running"
                done = int(batch.passed or 0) + int(batch.failed or 0)
                card = {
                    **card,
                    "status": card_status_from_batch(status),
                    "progress": {"total": batch.total_cases or len(card.get("case_ids") or []), "done": done},
                    "result": {
                        "passed": int(batch.passed or 0),
                        "failed": int(batch.failed or 0),
                    },
                }
                if status in _TERMINAL_BATCH_STATUS:
                    card["failures"] = await _collect_evidence(db, batch_id, card.get("case_ids") or [])
                    await _write_run_card(db, msg, card)
                    await emit(conversation_id, {
                        "type": "task.updated", "task_id": str(batch_id), "kind": "run",
                        "status": "done" if card["status"] == "done" else card["status"],
                    })
                    return
                await _write_run_card(db, msg, card)
                await emit(conversation_id, {
                    "type": "task.updated", "task_id": str(batch_id), "kind": "run",
                    "status": "running", "progress": done,
                })
        except Exception:
            logger.debug("watch run failed batch=%s", batch_id, exc_info=True)
            return


# ----------------------------
# 动作处理器 / 取消
# ----------------------------

@register_action_handler("run_case")
async def run_case_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    card, err, task_id = await start_run(
        db, conversation=conversation, case_ids=action["case_ids"],
        mode=action.get("mode") or "server", agent_id=action.get("agent_id"),
        user_id=ctx.get("user_id"),
    )
    if card is None:
        return ActionOutcome(ok=False, note=f"（执行未发起：{err}）")
    return ActionOutcome(card=card, task_id=task_id, note="执行已发起，进度与结果会实时回显。")


async def cancel_run(db, *, conversation, batch_id: int, user_id: int) -> tuple[Optional[dict], str]:
    """取消执行：复用既有批次 stop 语义。"""
    msg, card = await _find_run_card_message(db, conversation.id, batch_id)
    if card is None:
        return None, "运行卡片不存在"
    if card.get("status") not in ("running", "paused"):
        return None, "该执行已结束"
    user = await _load_user(db, user_id)
    if user is None:
        return None, "用户不存在"
    try:
        from app.routers.report_router import stop_batch

        await stop_batch(batch_id, user=user, db=db)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return None, f"取消失败：{detail}"
    card = {**card, "status": "canceled"}
    await _write_run_card(db, msg, card)
    return {"card": card}, ""
