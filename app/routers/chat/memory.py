# app/routers/chat/memory.py
# 044 US4：项目记忆管理（设置页消费）。
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_async_db
from app.routers.chat.conversations import _ensure_project_access
from app.services import chat_memory

router = APIRouter(prefix="/api/chat", tags=["对话工作台"])


def _ensure_memory_enabled() -> None:
    if not get_settings().chat_enabled:
        raise HTTPException(403, "对话功能未启用")
    if not get_settings().chat_memory_enabled:
        raise HTTPException(403, "记忆功能未启用")


@router.get("/memory")
async def list_memory(
    project_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_memory_enabled()
    _ensure_project_access(user, project_id)
    items = chat_memory.list_for_project(int(project_id or 0))
    return {"items": items, "total": len(items)}


@router.delete("/memory/{name}")
async def delete_memory(
    name: str,
    project_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_memory_enabled()
    _ensure_project_access(user, project_id)
    if not chat_memory.delete_for_project(int(project_id or 0), name):
        raise HTTPException(404, "记忆不存在")
    return {"deleted": True, "name": name}
