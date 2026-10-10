# app/services/chat_evidence.py
# 034 失败归因追问（US5）：
#   - 证据组装：失败 run 的步骤级证据（失败步骤/console/截图）+ 用例当前步骤定义
#   - 无证据 → 固定话术（**不调用模型**，FR-025 红线）
#   - 有证据 → LLM 解读（grounded_on 标注 + 可执行建议 apply_prompt → 转 edit_case）
#   - LLM/解析失败 → 退化为纯证据卡（不臆造结论）
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

from core.chat import contracts
from core.chat.orchestrator import ActionOutcome, register_action_handler

logger = logging.getLogger(__name__)


# ----------------------------
# 证据组装（纯逻辑 + DB 读取）
# ----------------------------

def build_case_evidence(
    *,
    case_id: int,
    case_title: str,
    logs: list[dict],
    steps_definition: list[dict],
) -> dict:
    """单个用例的证据包（run_logs 口径，含 ` | ` 合并段拆分）。"""
    from app.services.chat_run import build_failure_summary, split_log_segments

    steps = []
    for lg in logs:
        level_ok = lg.get("level") in ("info", "success", "passed")
        for seg in split_log_segments(lg.get("message") or ""):
            is_console = "[console]" in seg
            steps.append({
                "success": level_ok or is_console,
                "description": seg,
                "screenshot_path": lg.get("screenshot_path"),
            })
    summary = build_failure_summary(steps)
    return {
        "case_id": case_id,
        "case_title": case_title,
        "step": summary["step"],
        "console": summary["console"],
        "screenshot_url": summary["screenshot_url"],
        "failed_count": summary["failed_count"],
        "steps_definition": steps_definition or [],
        "has_evidence": bool(summary["step"] or summary["failed_count"] or summary["console"]),
    }


async def assemble_evidence(db, *, batch_id: int, case_id: Optional[int] = None) -> dict:
    """从批次收集失败证据；``case_id`` 限定单个用例，None 聚合批次内全部失败用例。"""
    from sqlalchemy import select

    from app import db_models

    runs_q = select(db_models.TestRun).where(
        db_models.TestRun.batch_id == batch_id,
        db_models.TestRun.status == "failed",
    )
    if case_id is not None:
        runs_q = runs_q.where(db_models.TestRun.case_id == case_id)
    runs = (await db.execute(runs_q)).scalars().all()

    cases = []
    for run in runs[:10]:
        logs = (
            await db.execute(
                select(db_models.RunLog).where(db_models.RunLog.run_id == run.id).order_by(db_models.RunLog.id)
            )
        ).scalars().all()
        log_dicts = [
            {"level": lg.level, "message": lg.message, "screenshot_path": lg.screenshot_path}
            for lg in logs
        ]
        case_title = f"用例 #{run.case_id}"
        definition: list[dict] = []
        try:
            tc = await db.get(db_models.TestCase, run.case_id)
            if tc is not None:
                case_title = tc.name or case_title
                steps = (await db.execute(
                    select(db_models.TestStep)
                    .where(db_models.TestStep.case_id == run.case_id)
                    .order_by(db_models.TestStep.step_order)
                )).scalars().all()
                definition = [
                    {"description": s.description or "", "expected": s.parsed_result or ""}
                    for s in steps
                ]
        except Exception:
            logger.debug("load case definition failed case=%s", run.case_id, exc_info=True)
        cases.append(build_case_evidence(
            case_id=run.case_id, case_title=case_title, logs=log_dicts, steps_definition=definition,
        ))
    return {
        "batch_id": batch_id,
        "case_id": case_id,
        "cases": cases,
        "has_evidence": any(c["has_evidence"] for c in cases),
    }


# ----------------------------
# 固定话术 / 解读解析
# ----------------------------

def insufficient_reply(batch_id: int) -> str:
    return (
        f"批次 #{batch_id} 中未发现失败记录或缺少可用证据（失败步骤/console 均为空）："
        "证据不足，无法归因。请确认批次失败后重试，或补充失败现场。"
    )


def parse_explanation(raw: str) -> tuple[Optional[dict], str]:
    """解析归因输出：{analysis, grounded_on[], suggestions[]}。"""
    obj = contracts.extract_json_object(raw or "")
    if obj is None:
        return None, "未能解析归因 JSON"
    analysis = str(obj.get("analysis") or "").strip()
    if not analysis:
        return None, "归因分析为空"
    grounded = [str(g).strip() for g in (obj.get("grounded_on") or []) if str(g).strip()][:10]
    suggestions = []
    for s in (obj.get("suggestions") or [])[:5]:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        prompt = str(s.get("apply_prompt") or "").strip()
        if not title or not prompt:
            continue
        suggestions.append({
            "title": title[:200],
            "reason": str(s.get("reason") or "").strip()[:500],
            "apply_prompt": prompt[:2000],
        })
    return {"analysis": analysis[:4000], "grounded_on": grounded, "suggestions": suggestions}, ""


def _evidence_prompt(ev: dict) -> list[dict]:
    payload = {
        "batch_id": ev["batch_id"],
        "cases": [
            {
                "case_id": c["case_id"],
                "case_title": c["case_title"],
                "failed_step": c["step"],
                "console": c["console"],
                "current_steps": c["steps_definition"],
            }
            for c in ev["cases"]
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是测试失败归因助手。只能依据给定证据分析失败原因，禁止臆测；"
                "引用证据时逐条列出依据。对可修复的失败给出修改建议（apply_prompt 是用户可直接发送给"
                "对话助手的修改指令原文，必须包含用例 id 与具体步骤表述）。"
                "只输出 JSON：{\"analysis\": \"…\", \"grounded_on\": [\"证据点…\"], "
                "\"suggestions\": [{\"title\": \"…\", \"reason\": \"…\", \"apply_prompt\": \"…\"}]}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:15000]},
    ]


async def explain(
    db,
    *,
    conversation,
    batch_id: int,
    case_id: Optional[int] = None,
    llm_call: Optional[Callable[[list], Awaitable[str]]] = None,
) -> tuple[Optional[dict], str]:
    """生成失败归因卡；返回 (card, error)。"""
    ev = await assemble_evidence(db, batch_id=batch_id, case_id=case_id)
    base = {
        "type": "evidence",
        "batch_id": batch_id,
        "case_id": case_id,
        "evidence": {"batch_id": batch_id, "cases": ev["cases"]},
    }
    if not ev["has_evidence"]:
        return {
            **base,
            "status": "insufficient",
            "analysis": insufficient_reply(batch_id),
            "grounded_on": [],
            "suggestions": [],
        }, ""

    if llm_call is None:
        from app.gen.model_client import call_model

        agent_id = getattr(conversation, "assistant_agent_id", None)
        llm_call = lambda messages: call_model(messages, agent_type="assistant", agent_id=agent_id)  # noqa: E731

    try:
        raw = await llm_call(_evidence_prompt(ev))
        parsed, perr = parse_explanation(raw)
    except Exception as exc:
        logger.warning("evidence llm call failed: %s", exc)
        parsed, perr = None, str(exc)
    if parsed is None:
        return {
            **base,
            "status": "ready",
            "analysis": f"（模型解读失败：{str(perr)[:120]}）已收集到以下失败证据，请人工核对。",
            "grounded_on": [],
            "suggestions": [],
        }, ""
    return {**base, "status": "ready", **parsed}, ""


# ----------------------------
# 动作处理器
# ----------------------------

@register_action_handler("explain")
async def explain_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    card, err = await explain(
        db, conversation=conversation,
        batch_id=action["batch_id"], case_id=action.get("case_id"),
        llm_call=ctx.get("llm"),
    )
    if card is None:
        return ActionOutcome(ok=False, note=f"（归因失败：{err}）")
    if card.get("status") == "insufficient":
        return ActionOutcome(card=card, note=card["analysis"])
    return ActionOutcome(card=card, note="失败归因已生成（基于实际执行证据）。")
