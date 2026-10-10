# app/routers/chat/ws.py
# 034 对话 WS 通道（contracts C2）：仅服务端推送；cookie 鉴权 + 会话所有权校验。
from __future__ import annotations

import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.websocket import LogWebSocketManager
from core.chat import events

logger = logging.getLogger(__name__)

# 复用日志管理器语义（conversation_id -> Set[WebSocket]）
chat_manager = LogWebSocketManager()


async def _push(conversation_id: int, event: dict) -> None:
    await chat_manager.send_message(conversation_id, event)


# 注册推送钩子（模块被 main.py 导入时生效）
events.register_push_hook(_push)


async def chat_conversation_ws(websocket: WebSocket, conversation_id: int):
    """对话实时通道：message.created/updated/deleted、task.updated、error。"""
    from app.auth import get_session
    from app.crud import chat as chat_crud
    from app.database import AsyncSessionLocal

    session_id = websocket.cookies.get("session_id")
    if not session_id:
        await websocket.close(code=4001, reason="missing session_id")
        return
    async with AsyncSessionLocal() as db:
        session = await get_session(db, session_id)
        if not session:
            await websocket.close(code=4003, reason="invalid session")
            return
        conv = await chat_crud.get_conversation(db, conversation_id, user_id=session.user_id)
        if conv is None:
            await websocket.close(code=4004, reason="conversation not found")
            return

    await chat_manager.connect(websocket, conversation_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await chat_manager.disconnect(websocket, conversation_id)
    except (RuntimeError, ConnectionError):
        logger.exception("chat ws error conversation_id=%s", conversation_id)
        await chat_manager.disconnect(websocket, conversation_id)
