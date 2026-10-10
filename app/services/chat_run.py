# app/services/chat_run.py
# 034 对话内执行（US4）：
#   - start_run：复用既有批量执行语义（服务端 / 客户端 Agent），对话绑定项目
#   - watcher：轮询批次状态 → 运行卡片回填（进度/结果/失败证据）+ WS 推送
#   - 失败证据：run_logs 口径（失败步骤/断言文本/console 摘要/截图入口）
#   - 取消：复用既有批次 stop 语义
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from app import crud
from app.config import get_settings
from app.crud import chat as chat_crud
from core.chat.events import emit
from core.chat.orchestrator import ActionOutcome, register_action_handler, serialize_message

logger = logging.getLogger(__name__)

_TERMINAL_BATCH_STATUS = ("passed", "failed", "partial", "cancelled")
_ENV_PROBE_TIMEOUT_S = 3.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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


def format_activity(tool_call) -> str:
    """最近一个工具调用 → 卡片一行活动提示（纯函数，可单测）。"""
    turn = int(getattr(tool_call, "turn_number", 0) or 0)
    name = str(getattr(tool_call, "tool_name", "") or "").strip() or "动作"
    text = f"第 {turn} 轮 · {name}"
    try:
        ok = bool(int(getattr(tool_call, "success", 1) or 0))
    except (TypeError, ValueError):
        ok = bool(getattr(tool_call, "success", 1))
    if not ok:
        detail = ""
        for line in _ANSI_RE.sub("", str(getattr(tool_call, "error_message", "") or "")).splitlines():
            line = line.strip()
            if not line or line.startswith("###"):
                continue
            detail = line[:80]
            break
        text += "（失败" + (f"：{detail}" if detail else "") + "）"
    return text


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


async def _pick_environment(db, project_id: int):
    """服务端形态默认环境：与用例页口径一致 —— 优先生效 ``is_default``，否则第一个环境。

    返回 Environment 对象（可能为 None）。
    """
    try:
        envs = await crud.get_environments(db, project_id)
        if not envs:
            await crud.ensure_default_environment(db, project_id)
            envs = await crud.get_environments(db, project_id)
        if not envs:
            return None
        default_env = next((e for e in envs if getattr(e, "is_default", False)), None)
        return default_env or envs[0]
    except Exception:
        logger.debug("pick environment failed", exc_info=True)
        return None


async def _resolve_environment(db, project_id: int, environment_id: Optional[int]):
    """解析本次执行环境：用户/模型显式指定优先（校验同项目），否则用默认。

    返回 ``(env | None, err)``。
    """
    if environment_id:
        from app import db_models

        env = await db.get(db_models.Environment, int(environment_id))
        if env is None:
            return None, f"环境 #{environment_id} 不存在"
        if int(env.project_id) != int(project_id):
            return None, f"环境 #{environment_id} 不属于当前对话绑定的项目"
        return env, ""
    return await _pick_environment(db, project_id), ""


async def _probe_env_reachable(url: str) -> Optional[bool]:
    """环境 base_url 可达性预检（软失败）：True/False，无 url 或异常探测返回 None。"""
    url = (url or "").strip()
    if not url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=_ENV_PROBE_TIMEOUT_S, verify=False, follow_redirects=False,
        ) as client:
            await client.get(url)
        return True
    except Exception:
        return False


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


async def _client_agent_online(db, name: str) -> bool:
    """客户端 Agent 是否在线（与 /api/agents 同口径：WS last_seen 或心跳 120s 内）。

    检查失败（如无 DB 会话）时不误报，返回 True。
    """
    if not name:
        return True
    try:
        from agent.manager import agent_manager

        from app import db_models
        from app.tz import now as tz_now

        now = tz_now()
        session = agent_manager.sessions.get(name)
        if session is not None and session.agent.last_seen is not None:
            ls = session.agent.last_seen
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=now.tzinfo)
            if (now - ls).total_seconds() < 120:
                return True
        if db is not None:
            from sqlalchemy import select as _select

            row = (
                await db.execute(_select(db_models.Agent).where(db_models.Agent.name == name))
            ).scalars().first()
            if row is not None and row.last_heartbeat is not None:
                hb = row.last_heartbeat
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=now.tzinfo)
                if (now - hb).total_seconds() < 120:
                    return True
        return False
    except Exception:
        logger.debug("client online check failed name=%s", name, exc_info=True)
        return True


async def start_run(
    db,
    *,
    conversation,
    case_ids: list[int],
    mode: str,
    agent_id: Optional[int],
    user_id: int,
    environment_id: Optional[int] = None,
) -> tuple[Optional[dict], str, Optional[str]]:
    """发起对话内执行；返回 (card, error, task_id)。

    ``environment_id``：显式指定执行环境（须属于对话绑定项目）；缺省用项目默认环境。
    """
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

    # 形态与用例类型对齐：客户端形态仅用于 UI 用例；接口用例只在服务端执行
    kinds = {(getattr(c, "case_kind", None) or "ui") for c in cases}
    if mode == "client" and "ui" not in kinds:
        mode = "server"
        agent_id = None
    elif mode == "client" and "api" in kinds:
        return None, "接口用例只支持服务端形态执行（可把顶部「客户端」切换为服务端，或只选择 UI 用例）", None

    agent_name = None
    if mode == "client":
        if not agent_id:
            return None, "客户端形态需要指定 Agent", None
        from app import db_models

        agent = await db.get(db_models.Agent, agent_id)
        if agent is None:
            return None, f"客户端 Agent #{agent_id} 不存在", None
        agent_name = agent.name

    agent_warning = ""
    if mode == "client" and agent_name and not await _client_agent_online(db, agent_name):
        agent_warning = (
            f"客户端「{agent_name}」当前未连接（超过 2 分钟无心跳），执行可能无法开始；"
            "请确认该客户端已启动并连接平台。"
        )

    env, env_err = await _resolve_environment(db, project_id, environment_id)
    if env_err:
        return None, env_err, None
    env_id = int(env.id) if env is not None else None
    env_warning = ""
    if env is not None and (env.base_url or "").strip():
        reachable = await _probe_env_reachable(env.base_url)
        if reachable is False:
            env_warning = (
                f"目标环境「{env.name}」地址 {(env.base_url or '').strip()} 当前不可达（连接失败），"
                "执行可能在开始阶段失败；可在对话中说「用 <环境名> 环境执行」指定其他环境。"
            )

    user = await _load_user(db, user_id)
    if user is None:
        return None, "用户不存在", None

    try:
        batch_id = await _invoke_batch(
            db, user=user, case_ids=[c.id for c in cases], mode=mode,
            agent_name=agent_name, environment_id=env_id,
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
        "environment_id": env_id,
        "environment_name": getattr(env, "name", None) if env is not None else None,
        "environment_url": (getattr(env, "base_url", "") or "").strip() or None if env is not None else None,
        "env_warning": env_warning or None,
        "agent_warning": agent_warning or None,
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


async def _current_activity(db, batch_id: int) -> Optional[str]:
    """批次关联 agent_run 的最近工具调用 → 一行活性提示（失败返回 None，不影响主流程）。"""
    from sqlalchemy import select as _select

    from app import db_models

    try:
        run = (
            await db.execute(
                _select(db_models.AgentRun)
                .where(db_models.AgentRun.goal.op("->>")("batch_id") == str(batch_id))
                .order_by(db_models.AgentRun.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if run is None:
            return None
        tool_call = (
            await db.execute(
                _select(db_models.AgentToolCall)
                .where(db_models.AgentToolCall.run_id == run.id)
                .order_by(db_models.AgentToolCall.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if tool_call is None:
            turns = int(getattr(run, "turns_used", 0) or 0)
            return f"第 {turns} 轮" if turns else None
        return format_activity(tool_call)
    except Exception:
        logger.debug("current activity lookup failed batch=%s", batch_id, exc_info=True)
        return None


async def _watch_run(conversation_id: int, batch_id: int, *, max_iterations: int = 900) -> None:
    from app.database import AsyncSessionLocal

    consecutive_errors = 0
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
                # 卡片活性反馈：已用时长（运行中 + 终态都给）
                created = getattr(batch, "created_at", None)
                if created is not None:
                    try:
                        from app.tz import now as tz_now

                        card["elapsed_s"] = max(0, int((tz_now() - created).total_seconds()))
                    except Exception:
                        pass
                if status in _TERMINAL_BATCH_STATUS:
                    card["failures"] = await _collect_evidence(db, batch_id, card.get("case_ids") or [])
                    await _write_run_card(db, msg, card)
                    await emit(conversation_id, {
                        "type": "task.updated", "task_id": str(batch_id), "kind": "run",
                        "status": "done" if card["status"] == "done" else card["status"],
                    })
                    return
                # 运行中：附带最近动作，避免长时间「执行中」无反馈
                activity = await _current_activity(db, batch_id)
                if activity:
                    card["current"] = activity
                await _write_run_card(db, msg, card)
                await emit(conversation_id, {
                    "type": "task.updated", "task_id": str(batch_id), "kind": "run",
                    "status": "running", "progress": done,
                })
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            logger.debug(
                "watch run failed batch=%s consecutive=%s", batch_id, consecutive_errors, exc_info=True,
            )
            if consecutive_errors >= 15:
                return


# ----------------------------
# 动作处理器 / 取消
# ----------------------------

@register_action_handler("run_case")
async def run_case_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    """执行用例：默认使用对话顶部选择的「环境 / 客户端」，仅当本条消息显式指定时才覆盖。"""
    conv_agent_id = getattr(conversation, "agent_id", None)
    conv_env_id = getattr(conversation, "environment_id", None)
    agent_id = action.get("agent_id") or conv_agent_id
    mode = action.get("mode") or ("client" if agent_id else "server")
    if mode == "server":
        agent_id = action.get("agent_id") or None
    environment_id = action.get("environment_id") or conv_env_id
    card, err, task_id = await start_run(
        db, conversation=conversation, case_ids=action["case_ids"],
        mode=mode, agent_id=agent_id,
        user_id=ctx.get("user_id"), environment_id=environment_id,
    )
    if card is None:
        return ActionOutcome(ok=False, note=f"（执行未发起：{err}）")
    env_name = card.get("environment_name")
    note = (
        f"执行已发起（环境：{env_name}），进度与结果会实时回显。"
        if env_name else "执行已发起，进度与结果会实时回显。"
    )
    if card.get("env_warning"):
        note += f"注意：{card['env_warning']}"
    if card.get("agent_warning"):
        note += f"注意：{card['agent_warning']}"
    return ActionOutcome(card=card, task_id=task_id, note=note)


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
