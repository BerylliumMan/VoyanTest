# app/routers/chat/execute.py
# 034 对话内执行端点（contracts C1.2）：发起执行 / 取消。
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.config import get_settings
from app.crud import chat as chat_crud
from app.database import get_async_db

router = APIRouter(prefix="/api/chat", tags=["对话工作台"])


def _ensure_enabled() -> None:
    if not get_settings().chat_enabled:
        raise HTTPException(403, "对话功能未启用")


class RunCasesRequest(BaseModel):
    case_ids: list[int]
    mode: str = "server"
    agent_id: Optional[int] = None


@router.post("/conversations/{conversation_id}/run")
async def run_cases_endpoint(
    conversation_id: int,
    payload: RunCasesRequest,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")
    allowed = get_user_project_filter(user)
    if conv.project_id is not None and allowed is not None and conv.project_id not in allowed:
        raise HTTPException(403, "无权访问该项目")
    if payload.mode not in ("server", "client"):
        raise HTTPException(400, "mode 必须为 server|client")

    from app.services import chat_run

    card, err, _task_id = await chat_run.start_run(
        db, conversation=conv, case_ids=payload.case_ids,
        mode=payload.mode, agent_id=payload.agent_id, user_id=user.id,
    )
    if card is None:
        raise HTTPException(400, err)

    # 卡片需要落进对话消息流（watcher 依据消息更新状态；前端经 WS 实时刷新）
    from core.chat.events import emit
    from core.chat.orchestrator import serialize_message

    mode_label = "客户端" if payload.mode == "client" else "服务端"
    titles = "、".join(card.get("case_titles") or [])
    msg = await chat_crud.create_message(
        db, conversation_id=conv.id, role="assistant",
        content=f"已发起执行（{mode_label}）：{titles}",
        cards=[card], task_id=str(card["batch_id"]),
    )
    await emit(conv.id, {"type": "message.created", "message": serialize_message(msg)})
    return {"card": card, "message": serialize_message(msg)}


@router.post("/conversations/{conversation_id}/run/{batch_id}/cancel")
async def cancel_run_endpoint(
    conversation_id: int,
    batch_id: int,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")

    from app.services import chat_run

    result, err = await chat_run.cancel_run(
        db, conversation=conv, batch_id=batch_id, user_id=user.id,
    )
    if result is None:
        raise HTTPException(400, err)
    return result
