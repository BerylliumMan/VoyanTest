# app/routers/chat/evidence.py
# 034 US5/US7：失败归因端点 + 对话导出（Markdown）。
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.crud import chat as chat_crud
from app.database import get_async_db
from core.chat.events import emit
from core.chat.orchestrator import serialize_message

router = APIRouter(prefix="/api/chat", tags=["对话工作台"])


def _ensure_enabled() -> None:
    if not get_settings().chat_enabled:
        raise HTTPException(403, "对话功能未启用")


class EvidenceRequest(BaseModel):
    batch_id: int
    case_id: Optional[int] = None


@router.post("/conversations/{conversation_id}/evidence")
async def evidence_endpoint(
    conversation_id: int,
    payload: EvidenceRequest,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")

    from app.services import chat_evidence

    card, err = await chat_evidence.explain(
        db, conversation=conv, batch_id=payload.batch_id, case_id=payload.case_id,
    )
    if card is None:
        raise HTTPException(400, err)
    title = f"批次 #{payload.batch_id}" + (f" · 用例 #{payload.case_id}" if payload.case_id else "")
    content = card["analysis"] if card.get("status") == "insufficient" else (
        "已完成失败归因分析（基于实际执行证据）。"
    )
    msg = await chat_crud.create_message(
        db, conversation_id=conv.id, role="assistant",
        content=f"{content}\n（{title}）", cards=[card],
    )
    await emit(conv.id, {"type": "message.created", "message": serialize_message(msg)})
    return {"card": card, "message": serialize_message(msg)}


# ----------------------------
# US7：对话导出（Markdown）
# ----------------------------

_CARD_LABELS = {
    "draft_set": "草稿集",
    "diff": "修改对照",
    "run": "执行批次",
    "evidence": "失败归因",
}


def conversation_to_markdown(conversation, messages) -> str:
    """对话 → Markdown（消息/时间/产物摘要；与实际记录一致）。"""
    lines = [f"# 对话记录：{conversation.title}", ""]
    lines.append(f"- 对话 ID：{conversation.id}")
    lines.append(f"- 项目 ID：{conversation.project_id}")
    lines.append(f"- 创建时间：{conversation.created_at.isoformat() if conversation.created_at else ''}")
    lines.append(f"- 导出时间：{__import__('app.tz', fromlist=['now']).now().isoformat()}")
    lines.append("")
    for m in messages:
        who = "用户" if m.role == "user" else "助手"
        ts = m.created_at.isoformat() if m.created_at else ""
        lines.append(f"## {who} · {ts}")
        lines.append("")
        if m.content:
            lines.append(m.content)
            lines.append("")
        for r in m.refs or []:
            if isinstance(r, dict) and r.get("type") != "file":
                lines.append(f"> 引用：{r.get('type')}#{r.get('id')} {r.get('label') or ''}")
        for c in m.cards or []:
            if not isinstance(c, dict):
                continue
            label = _CARD_LABELS.get(c.get("type"), c.get("type"))
            if c.get("type") == "draft_set":
                lines.append(f"> [{label}] {c.get('case_kind')} · {len(c.get('items') or [])} 条 · 状态 {c.get('status')}")
                for it in (c.get("items") or [])[:30]:
                    lines.append(f">   - {it.get('title')}")
            elif c.get("type") == "diff":
                lines.append(f"> [{label}] 用例 #{c.get('case_id')} · 状态 {c.get('status')} · {len(c.get('changes') or [])} 处变更")
            elif c.get("type") == "run":
                res = c.get("result") or {}
                lines.append(f"> [{label}] #{c.get('batch_id')} · 状态 {c.get('status')} · 通过 {res.get('passed', '-')} 失败 {res.get('failed', '-')}")
            elif c.get("type") == "evidence":
                lines.append(f"> [{label}] 批次 #{c.get('batch_id')} · 状态 {c.get('status')}")
        lines.append("")
    return "\n".join(lines)


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
):
    _ensure_enabled()
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")
    messages = await chat_crud.list_messages_after(db, conversation_id, limit=1000)
    md = conversation_to_markdown(conv, messages)
    from urllib.parse import quote

    filename = quote(f"chat-{conversation_id}.md")
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
