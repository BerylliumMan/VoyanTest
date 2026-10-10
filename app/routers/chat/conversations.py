# app/routers/chat/conversations.py
# 034 对话工作台 REST（contracts C1.1）：会话 CRUD / 消息 / 重新生成 / 删除 / 附件。
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.config import get_settings
from app.crud import chat as chat_crud
from app.database import get_async_db
from core.chat import orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["对话工作台"])

_CHAT_UPLOAD_DIR = os.path.join("data", "chat_uploads")


def _ensure_enabled() -> None:
    if not get_settings().chat_enabled:
        raise HTTPException(403, "对话功能未启用")


@router.get("/status")
async def chat_status() -> dict:
    """轻量状态（不受 chat_enabled 拦截）：前端据此隐藏菜单/禁用入口。"""
    return {"enabled": bool(get_settings().chat_enabled)}


def _ensure_project_access(user, project_id: Optional[int]) -> None:
    if project_id is None:
        return
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(403, "无权访问该项目")


async def _get_owned(db: AsyncSession, conversation_id: int, user):
    conv = await chat_crud.get_conversation(db, conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(404, "对话不存在")
    return conv


class ConversationCreate(BaseModel):
    title: Optional[str] = None
    project_id: int
    assistant_agent_id: Optional[int] = None
    refs: Optional[list] = None


class ConversationPatch(BaseModel):
    title: Optional[str] = None
    project_id: Optional[int] = None
    status: Optional[str] = None
    assistant_agent_id: Optional[int] = None
    refs: Optional[list] = None


class MessageSend(BaseModel):
    content: str = ""
    refs: Optional[list] = None
    attachments: Optional[list] = None


def serialize_conversation(c) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "project_id": c.project_id,
        "assistant_agent_id": c.assistant_agent_id,
        "status": c.status,
        "refs": c.refs or [],
        "summary": c.summary,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ----------------------------
# 会话
# ----------------------------

@router.post("/conversations")
async def create_conversation(
    payload: ConversationCreate,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    _ensure_project_access(user, payload.project_id)

    # US6：初始引用校验（存在性 + 项目权限；全部无效则拒绝）
    refs = None
    if payload.refs:
        from app.services import chat_refs

        valid, problems = await chat_refs.validate_refs(db, payload.refs, user)
        if problems and not valid:
            raise HTTPException(403, "；".join(problems[:3]))
        refs = valid or None

    assistant_agent_id = payload.assistant_agent_id
    if assistant_agent_id is not None:
        from app.crud.agent_definition import get_agent_definition

        agent = await get_agent_definition(db, assistant_agent_id)
        if agent is None or agent.agent_type != "assistant":
            raise HTTPException(400, f"无效的对话助手 Agent id={assistant_agent_id}")
    else:
        from app.crud.agent_definition import get_active_by_type

        default_agent = await get_active_by_type(db, "assistant")
        assistant_agent_id = default_agent.id if default_agent else None

    conv = await chat_crud.create_conversation(
        db, user_id=user.id, project_id=payload.project_id,
        assistant_agent_id=assistant_agent_id,
        title=(payload.title or "新对话"),
        refs=refs,
    )
    return serialize_conversation(conv)


@router.get("/conversations")
async def list_conversations(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    result = await chat_crud.list_conversations(db, user.id, status=status, limit=limit, offset=offset)
    return {"items": [serialize_conversation(c) for c in result["items"]]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await _get_owned(db, conversation_id, user)
    return serialize_conversation(conv)


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: int,
    payload: ConversationPatch,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await _get_owned(db, conversation_id, user)
    fields: dict = {}
    if payload.title is not None:
        fields["title"] = payload.title
    if payload.project_id is not None:
        _ensure_project_access(user, payload.project_id)
        fields["project_id"] = payload.project_id
    if payload.status is not None:
        if payload.status not in ("active", "archived"):
            raise HTTPException(400, "status 必须为 active|archived")
        fields["status"] = payload.status
    if payload.assistant_agent_id is not None:
        from app.crud.agent_definition import get_agent_definition

        agent = await get_agent_definition(db, payload.assistant_agent_id)
        if agent is None or agent.agent_type != "assistant":
            raise HTTPException(400, "无效的对话助手 Agent")
        fields["assistant_agent_id"] = payload.assistant_agent_id
    if payload.refs is not None:
        from app.services import chat_refs

        valid, problems = await chat_refs.validate_refs(db, payload.refs, user)
        if problems and not valid:
            raise HTTPException(403, "；".join(problems[:3]))
        fields["refs"] = valid or None
    if fields:
        conv = await chat_crud.update_conversation(db, conv, **fields)
    return serialize_conversation(conv)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await _get_owned(db, conversation_id, user)
    await chat_crud.delete_conversation(db, conv)
    return {"ok": True}


# ----------------------------
# 消息
# ----------------------------

@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    await _get_owned(db, conversation_id, user)
    page = await chat_crud.list_messages(db, conversation_id, before_id=before_id, limit=limit)
    return {
        "items": [orchestrator.serialize_message(m) for m in page["items"]],
        "has_more": page["has_more"],
    }


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: int,
    payload: MessageSend,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await _get_owned(db, conversation_id, user)
    content = (payload.content or "").strip()
    if not content and not payload.attachments:
        raise HTTPException(400, "消息内容不能为空")

    # US6：消息级引用校验（存在性 + 项目权限；越权 403）
    refs = payload.refs
    if refs:
        from app.services import chat_refs

        valid, problems = await chat_refs.validate_refs(db, refs, user)
        if problems and not valid:
            raise HTTPException(403, "；".join(problems[:3]))
        refs = valid

    user_msg, assistant = await orchestrator.handle_user_message(
        db, conversation=conv, user_id=user.id, content=content,
        refs=refs, attachments=payload.attachments,
    )
    return {
        "user_message": orchestrator.serialize_message(user_msg),
        "assistant_message": orchestrator.serialize_message(assistant),
    }


@router.post("/conversations/{conversation_id}/messages/{message_id}/regenerate")
async def regenerate_message(
    conversation_id: int,
    message_id: int,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await _get_owned(db, conversation_id, user)
    updated, err = await orchestrator.regenerate_message(
        db, conversation=conv, message_id=message_id, user_id=user.id,
    )
    if updated is None:
        status = 404 if "不存在" in err else 400
        raise HTTPException(status, err)
    return {"assistant_message": orchestrator.serialize_message(updated)}


@router.delete("/conversations/{conversation_id}/messages/{message_id}")
async def delete_message(
    conversation_id: int,
    message_id: int,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    conv = await _get_owned(db, conversation_id, user)
    ok, err = await orchestrator.delete_message(db, conversation=conv, message_id=message_id)
    if not ok:
        raise HTTPException(404, err)
    return {"ok": True}


# ----------------------------
# 附件（需求文档上传 → 生成时引用）
# ----------------------------

@router.post("/conversations/{conversation_id}/attachments")
async def upload_attachment(
    conversation_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    _ensure_enabled()
    await _get_owned(db, conversation_id, user)
    from app.routers.gen.upload import _check_extension, _check_magic_bytes

    filename = file.filename or "upload.bin"
    ext = _check_extension(filename)
    content = await file.read()
    _check_magic_bytes(content, ext)

    ref = uuid.uuid4().hex
    dest_dir = os.path.join(_CHAT_UPLOAD_DIR, ref)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(filename))
    with open(dest_path, "wb") as fh:
        fh.write(content)
    file_ref = os.path.join(_CHAT_UPLOAD_DIR, ref, os.path.basename(filename)).replace("\\", "/")
    return {"file_ref": file_ref, "filename": filename, "size": len(content)}
