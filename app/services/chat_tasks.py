# app/services/chat_tasks.py
# 034 迭代：对话内「停止」——浏览器探索 / 用例生成任务的统一停止入口。
#   - browse：取消进行中的探索 asyncio 任务（chat_browse 注册表）并回写卡片 stopped；
#   - generate：复用生成管线既有取消机制（request_cancel_gen：协作检查 + 直接取消任务），
#     并把 draft_set 卡片标记为 cancelled（区别于生成失败）。
from __future__ import annotations

import logging
from typing import Optional

from app.crud import chat as chat_crud
from core.chat.events import emit
from core.chat.orchestrator import serialize_message

logger = logging.getLogger(__name__)


async def stop_conversation_tasks(
    db,
    *,
    conversation,
    user_id: int,
    kind: Optional[str] = None,
    key: Optional[str] = None,
) -> dict:
    """停止对话内进行中的任务；``kind`` 缺省=全部。返回 ``{"browse": [...], "generate": [...]}``。"""
    stopped: dict[str, list[str]] = {"browse": [], "generate": []}

    if kind in (None, "browse"):
        try:
            from app.services import chat_browse

            stopped["browse"] = await chat_browse.stop_browse_tasks(
                conversation.id, key if kind == "browse" else None
            )
        except Exception:
            logger.exception("stop browse tasks failed")

    if kind in (None, "generate"):
        try:
            from app.routers.gen.state import request_cancel_gen

            msgs = await chat_crud.list_messages_after(db, conversation.id, after_id=0, limit=1000)
            for m in msgs:
                cards = list(m.cards or [])
                changed = False
                for idx, c in enumerate(cards):
                    if not isinstance(c, dict) or c.get("type") != "draft_set":
                        continue
                    if c.get("status") not in ("generating", "queued"):
                        continue
                    sid = c.get("gen_session_id")
                    if not sid:
                        continue
                    if kind == "generate" and key and sid != key:
                        continue
                    await request_cancel_gen(sid)
                    # 必须写"新字典"：原地改后整列按值比较检测不到变化，UPDATE 不会执行
                    nc = dict(c)
                    nc["status"] = "cancelled"
                    nc["error"] = None
                    cards[idx] = nc
                    changed = True
                    stopped["generate"].append(sid)
                if changed:
                    updated = await chat_crud.update_message_cards(db, m, cards)
                    await emit(conversation.id, {
                        "type": "message.updated",
                        "message": serialize_message(updated),
                    })
        except Exception:
            logger.exception("stop generate tasks failed")

    return stopped
