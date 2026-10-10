# app/services/chat_edit.py
# 034 对话内修改已有用例（US3）：
#   - edit_case：补丁 → 服务端渲染步骤级对照（diff 卡片，pending）
#   - apply_diff：基线双检（内容哈希 + updated_at）→ 复用 PUT 语义写回 → CaseRevision
#   - discard_diff：放弃（无变化）
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from app import crud
from app.crud import chat as chat_crud
from core.chat import contracts
from core.chat.events import emit
from core.chat.orchestrator import ActionOutcome, register_action_handler, serialize_message

logger = logging.getLogger(__name__)


# ----------------------------
# 工具
# ----------------------------

def _steps_to_dicts(steps) -> list[dict]:
    return [
        {"description": s.description or "", "expected": s.parsed_result or ""}
        for s in sorted(steps or [], key=lambda x: x.step_order)
    ]


def case_content_hash(case, steps) -> str:
    payload = {
        "name": case.name,
        "priority": getattr(case, "priority", None),
        "module_id": getattr(case, "module_id", None),
        "steps": _steps_to_dicts(steps),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


async def _load_case_with_steps(db, case_id: int):
    from sqlalchemy import select

    from app import db_models

    row = await db.execute(select(db_models.TestCase).where(db_models.TestCase.id == case_id))
    return row.scalar_one_or_none()


async def _write_card(db, message, card: dict) -> None:
    cards = list(message.cards or [])
    replaced = False
    for i, c in enumerate(cards):
        if isinstance(c, dict) and c.get("type") == "diff" and c.get("case_id") == card.get("case_id"):
            if c.get("status") == "pending" or c is card:
                cards[i] = card
                replaced = True
                break
    if not replaced:
        cards.append(card)
    updated = await chat_crud.update_message_cards(db, message, cards)
    await emit(updated.conversation_id, {"type": "message.updated", "message": serialize_message(updated)})


async def _correct_patch_via_llm(llm, case_title: str, steps: list[dict], patch: dict, error: str):
    """索引类补丁错误 → 带步骤清单请模型修正一次（常见 0/1-based 抖动）。

    返回修正后的补丁（已通过校验）或 None。
    """
    if llm is None:
        return None
    listing = "\n".join(
        f"index={i}: {str(s.get('description') or '')[:120]}" for i, s in enumerate(steps)
    )
    prompt = [
        {
            "role": "system",
            "content": (
                "你是用例补丁修正器。上一个补丁索引错误，请输出修正后的补丁 JSON，"
                "格式：{\"steps\": [{\"op\": \"update|remove\", \"index\": <0-based>…} "
                "| {\"op\": \"add\", \"after_index\": <0-based 或省略表示末尾>, \"step\": {…}}]}。"
                "只输出 JSON，index 必须在有效范围内。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"case_title": case_title, "error": error, "steps": listing, "bad_patch": patch},
                ensure_ascii=False,
            )[:8000],
        },
    ]
    try:
        raw = await llm(prompt)
        obj = contracts.extract_json_object(raw or "")
        if isinstance(obj, dict):
            validated, verr = contracts.validate_case_patch(obj)
            if validated is not None and verr == "":
                return validated
    except Exception:
        logger.debug("patch correction llm failed", exc_info=True)
    return None


# ----------------------------
# edit_case 动作处理器
# ----------------------------

@register_action_handler("edit_case")
async def edit_case_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    case = await _load_case_with_steps(db, action["case_id"])
    if case is None:
        return ActionOutcome(ok=False, note=f"（用例 #{action['case_id']} 不存在）")
    conv_project = getattr(conversation, "project_id", None)
    if conv_project is None:
        return ActionOutcome(ok=False, note="（对话未绑定项目，无法修改用例）")
    if getattr(case, "project_id", None) != conv_project:
        return ActionOutcome(ok=False, note="（该用例不属于当前对话绑定的项目）")

    steps = _steps_to_dicts(case.steps or [])
    new_steps, changes, err = contracts.apply_case_patch(steps, action["patch"])
    if err:
        # 034 实测：模型索引抖动（第 2 步写成 index=3）会让修改直接失败；
        # 带步骤清单请模型修正一次（仅错误路径触发，正常路径零额外调用）
        corrected = await _correct_patch_via_llm(
            (ctx or {}).get("llm"), case.name, steps, action["patch"], err,
        )
        if corrected is not None:
            action = {**action, "patch": corrected}
            new_steps, changes, err = contracts.apply_case_patch(steps, corrected)
    if err:
        return ActionOutcome(ok=False, note=f"（修改未生成对照：{err}）")
    if not changes:
        return ActionOutcome(ok=False, note="（修改指令未产生任何变化）")

    card = {
        "type": "diff",
        "case_id": case.id,
        "case_title": case.name,
        "base_hash": case_content_hash(case, case.steps or []),
        "base_updated_at": case.updated_at.isoformat() if case.updated_at else None,
        "changes": changes,
        "patch": action["patch"],
        "summary": action.get("summary") or "",
        "status": "pending",
    }
    return ActionOutcome(
        card=card,
        note="修改对照已生成，请在卡片上「应用修改」或「放弃」。",
    )


# ----------------------------
# 确认 / 放弃
# ----------------------------

async def _find_diff_card(db, conversation, message_id: int, card_index: int):
    msg = await chat_crud.get_message(db, conversation.id, message_id)
    if msg is None:
        return None, None, "消息不存在"
    cards = list(msg.cards or [])
    if card_index < 0 or card_index >= len(cards):
        return None, None, "卡片不存在"
    card = cards[card_index]
    if not isinstance(card, dict) or card.get("type") != "diff":
        return None, None, "该卡片不是修改对照"
    return msg, card, ""


async def apply_diff(
    db,
    *,
    conversation,
    message_id: int,
    card_index: int,
    user_id: int,
) -> tuple[Optional[dict], str]:
    """应用修改对照；返回 (result, error)。

    result 含 ``stale=True`` 表示基线已变化（已重建最新对照，需再次确认）。
    """
    msg, card, err = await _find_diff_card(db, conversation, message_id, card_index)
    if msg is None:
        return None, err
    if card.get("status") != "pending":
        return None, "该对照已处理"

    case = await _load_case_with_steps(db, card.get("case_id"))
    if case is None:
        return None, "用例不存在"
    if getattr(case, "project_id", None) != getattr(conversation, "project_id", None):
        return None, "用例不属于当前对话绑定的项目"

    current_hash = case_content_hash(case, case.steps or [])
    current_updated = case.updated_at.isoformat() if case.updated_at else None
    patch = card.get("patch") or {}

    # 基线双检（FR-017）：不一致 → 基于最新用例重建对照
    if current_hash != card.get("base_hash") or current_updated != card.get("base_updated_at"):
        steps = _steps_to_dicts(case.steps or [])
        _new, changes, perr = contracts.apply_case_patch(steps, patch)
        if perr:
            return None, f"用例已变化且补丁不再适用：{perr}"
        fresh = {
            **card,
            "base_hash": current_hash,
            "base_updated_at": current_updated,
            "changes": changes,
            "status": "pending",
            "stale": True,
        }
        await _write_card(db, msg, fresh)
        return {"stale": True, "card": fresh}, ""

    steps = _steps_to_dicts(case.steps or [])
    new_steps, changes, err2 = contracts.apply_case_patch(steps, patch)
    if err2:
        return None, err2

    # 复用 PUT 语义（派生结构化 + 固化脚本失效）+ 变更留痕（source=chat）
    from app.models.schemas import TestCaseUpdate, TestStepUpdate

    step_payload = [
        TestStepUpdate(
            step_order=i + 1,
            description=(s.get("description") or " ").strip() or " ",
            parsed_result=(s.get("expected") or None),
        )
        for i, s in enumerate(new_steps)
    ]
    body = TestCaseUpdate(
        name=case.name,
        description=case.description,
        module_id=case.module_id,
        steps=step_payload,
    )
    updated = await crud.update_test_case(
        db, case.id, body,
        revision_user_id=user_id,
        revision_source="chat",
        revision_conversation_id=conversation.id,
        revision_summary=card.get("summary") or None,
    )
    if updated is None:
        return None, "写回失败"

    revision_id = None
    try:
        rows = await chat_crud.list_case_revisions(db, case.id, limit=1)
        if rows:
            revision_id = rows[0].id
    except Exception:
        logger.debug("load revision id failed", exc_info=True)

    applied = {**card, "status": "applied", "revision_id": revision_id}
    await _write_card(db, msg, applied)
    return {"card": applied, "revision_id": revision_id}, ""


async def discard_diff(
    db,
    *,
    conversation,
    message_id: int,
    card_index: int,
) -> tuple[Optional[dict], str]:
    msg, card, err = await _find_diff_card(db, conversation, message_id, card_index)
    if msg is None:
        return None, err
    if card.get("status") != "pending":
        return None, "该对照已处理"
    discarded = {**card, "status": "discarded"}
    await _write_card(db, msg, discarded)
    return {"card": discarded}, ""
