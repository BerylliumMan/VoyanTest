# app/routers/chat/actions.py
# 034 对话动作端点（contracts C1.2）：确认导入（US2）等。
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


class ImportDraftsRequest(BaseModel):
    gen_session_id: str
    draft_ids: Optional[list] = None


class DiffActionRequest(BaseModel):
    message_id: int
    card_index: int


@router.post("/conversations/{conversation_id}/import-drafts")
async def import_drafts_endpoint(
    conversation_id: int,
    payload: ImportDraftsRequest,
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

    from app.services import chat_gen

    result, err = await chat_gen.import_drafts(
        db, conversation=conv, gen_session_id=payload.gen_session_id,
        draft_ids=payload.draft_ids,
    )
    if result is None:
        raise HTTPException(400, err)
    return result


@router.post("/conversations/{conversation_id}/apply-diff")
async def apply_diff_endpoint(
    conversation_id: int,
    payload: DiffActionRequest,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")

    from app.services import chat_edit

    result, err = await chat_edit.apply_diff(
        db, conversation=conv, message_id=payload.message_id,
        card_index=payload.card_index, user_id=user.id,
    )
    if result is None:
        raise HTTPException(400, err)
    if result.get("stale"):
        # 基线已变化（FR-017）：409 + 最新对照
        raise HTTPException(409, detail={
            "stale": True,
            "card": result["card"],
            "message": "用例已被修改，请查看最新对照后再次确认",
        })
    return result


@router.post("/conversations/{conversation_id}/discard-diff")
async def discard_diff_endpoint(
    conversation_id: int,
    payload: DiffActionRequest,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")

    from app.services import chat_edit

    result, err = await chat_edit.discard_diff(
        db, conversation=conv, message_id=payload.message_id, card_index=payload.card_index,
    )
    if result is None:
        raise HTTPException(400, err)
    return result
