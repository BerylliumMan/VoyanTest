# core/chat/turns.py
"""034 迭代二：对话「在途轮次」注册表 —— 发送/编辑/重生成进行中可停止。

语义（2026-10-14 实测反馈）：
- 提交一条消息后，发送按钮变「停止」；点击 → 取消该对话正在生成的这轮回复；
- 每个会话同时至多一轮在途（发消息端点先检查 has_turn，冲突返回 409）；
- 取消只针对「这一轮 LLM 决策」；后台任务（浏览器探索 / 用例生成 / 执行批次）
  各自有卡片上的停止按钮，不在本注册表范围。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_TURNS: dict[int, asyncio.Task] = {}


def register_turn(conversation_id: int, task: asyncio.Task) -> None:
    """登记在途轮次（同会话覆盖旧值：旧任务已结束时才会走到这里）。"""
    _TURNS[int(conversation_id)] = task


def unregister_turn(conversation_id: int, task: asyncio.Task) -> None:
    """轮次结束（正常/异常/取消）时移除；仅在仍是自己时移除，避免误删后来者。"""
    if _TURNS.get(int(conversation_id)) is task:
        _TURNS.pop(int(conversation_id), None)


def get_turn(conversation_id: int) -> asyncio.Task | None:
    task = _TURNS.get(int(conversation_id))
    if task is not None and task.done():
        return None
    return task


def has_turn(conversation_id: int) -> bool:
    return get_turn(conversation_id) is not None


def cancel_turn(conversation_id: int) -> bool:
    """取消在途轮次；返回是否真正发出了取消（没有在途轮次 → False）。"""
    task = get_turn(conversation_id)
    if task is None:
        return False
    task.cancel()
    logger.info("chat turn cancelled: conversation=%s", conversation_id)
    return True
