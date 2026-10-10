# core/chat/events.py
# 034 对话事件总线（进程内）：编排内核 → WS 推送的解耦点。
#
# WS 层（app/routers/chat/ws.py）在导入时注册推送钩子；编排/任务代码只依赖本模块。
from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_push_hooks: list[Callable[[int, dict], Awaitable[None]]] = []


def register_push_hook(fn: Callable[[int, dict], Awaitable[None]]) -> None:
    if fn not in _push_hooks:
        _push_hooks.append(fn)


async def emit(conversation_id: int, event: dict) -> None:
    """广播一条对话事件（尽力而为：推送失败不影响业务）。"""
    for fn in list(_push_hooks):
        try:
            await fn(conversation_id, event)
        except Exception:
            logger.debug("chat push hook failed", exc_info=True)
