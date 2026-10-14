"""对话任务停止（2026-10-14）：探索任务取消 + 生成任务取消（卡片状态回写）。

覆盖：stop_browse_tasks（按对话/精确 key 过滤、卡片先写、任务取消与注册表清理）、
stop_conversation_tasks（生成取消复用 request_cancel_gen、草稿卡标记 cancelled、WS 事件）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services import chat_browse, chat_tasks


@pytest.mark.asyncio
async def test_stop_browse_tasks_cancels_and_writes_card():
    written = []

    async def writer(cid, bid):
        written.append((cid, bid))

    async def sleeper():
        await asyncio.sleep(60)

    t1 = asyncio.create_task(sleeper())
    t2 = asyncio.create_task(sleeper())
    chat_browse._TASK_INDEX["b1"] = (7, t1)
    chat_browse._TASK_INDEX["b2"] = (8, t2)
    chat_browse._TASKS.update({t1, t2})
    try:
        stopped = await chat_browse.stop_browse_tasks(7, card_writer=writer)
        assert stopped == ["b1"]
        assert written == [(7, "b1")]
        assert t1.cancelled() or t1.done()
        assert not t2.done() and (8, t2) == chat_browse._TASK_INDEX["b2"]  # 其他对话不受影响
        assert "b1" not in chat_browse._TASK_INDEX
    finally:
        t2.cancel()
        chat_browse._TASK_INDEX.pop("b2", None)
        chat_browse._TASKS.discard(t2)


@pytest.mark.asyncio
async def test_stop_browse_tasks_precise_key():
    async def writer(cid, bid):
        pass

    async def sleeper():
        await asyncio.sleep(60)

    t1 = asyncio.create_task(sleeper())
    t2 = asyncio.create_task(sleeper())
    chat_browse._TASK_INDEX["b1"] = (7, t1)
    chat_browse._TASK_INDEX["b2"] = (7, t2)
    chat_browse._TASKS.update({t1, t2})
    try:
        stopped = await chat_browse.stop_browse_tasks(7, "b2", card_writer=writer)
        assert stopped == ["b2"] and not t1.done()
    finally:
        t1.cancel()
        chat_browse._TASK_INDEX.pop("b1", None)
        chat_browse._TASKS.discard(t1)


@pytest.mark.asyncio
async def test_stop_conversation_tasks_generate(monkeypatch):
    cancelled = []

    async def fake_cancel(sid):
        cancelled.append(sid)

    monkeypatch.setattr("app.routers.gen.state.request_cancel_gen", fake_cancel)

    class Msg:
        def __init__(self):
            self.cards = [
                {"type": "draft_set", "status": "generating", "gen_session_id": "s1", "items": []},
                {"type": "draft_set", "status": "draft", "gen_session_id": "s0", "items": []},
            ]

    async def fake_list(db, cid, after_id=0, limit=1000):
        return [Msg()]

    updated = {}

    async def fake_update(db, m, cards):
        updated["cards"] = cards
        m.cards = cards
        return m

    events = []

    async def fake_emit(cid, evt):
        events.append(evt)

    async def fake_stop_browse(cid, bid=None, **kw):
        return ["b1"]

    monkeypatch.setattr(chat_tasks.chat_crud, "list_messages_after", fake_list)
    monkeypatch.setattr(chat_tasks.chat_crud, "update_message_cards", fake_update)
    monkeypatch.setattr(chat_tasks, "emit", fake_emit)
    monkeypatch.setattr(chat_tasks, "serialize_message", lambda m: {"id": 1})
    monkeypatch.setattr(chat_browse, "stop_browse_tasks", fake_stop_browse)

    conv = SimpleNamespace(id=3)
    stopped = await chat_tasks.stop_conversation_tasks(None, conversation=conv, user_id=1)

    assert stopped["browse"] == ["b1"] and stopped["generate"] == ["s1"]
    assert cancelled == ["s1"]
    assert updated["cards"][0]["status"] == "cancelled"
    assert updated["cards"][1]["status"] == "draft"  # 非进行中的卡片不动
    assert events and events[0]["type"] == "message.updated"


@pytest.mark.asyncio
async def test_stop_persists_cancelled_card(chat_db, monkeypatch):
    """真实 SQLAlchemy 会话断言取消已落库（防「原地改 dict 导致 JSON 列变更检测失效」回归）。"""
    from app.crud import chat as crud
    from app.services import chat_tasks as ct

    c = await crud.create_conversation(chat_db, user_id=1)
    m = await crud.create_message(
        chat_db, conversation_id=c.id, role="assistant", content="gen",
        cards=[{"type": "draft_set", "status": "generating", "gen_session_id": "s1", "items": []}],
    )

    async def fake_cancel(sid):
        pass

    monkeypatch.setattr("app.routers.gen.state.request_cancel_gen", fake_cancel)
    events = []

    async def fake_emit(cid, evt):
        events.append(evt)

    monkeypatch.setattr(ct, "emit", fake_emit)

    conv = await crud.get_conversation(chat_db, c.id)
    stopped = await ct.stop_conversation_tasks(chat_db, conversation=conv, user_id=1, kind="generate")
    assert stopped["generate"] == ["s1"]
    assert events and events[0]["type"] == "message.updated"

    fresh = await crud.get_message(chat_db, c.id, m.id)
    assert fresh.cards[0]["status"] == "cancelled"  # 落库生效（原地改动不会触发 UPDATE）


@pytest.mark.asyncio
async def test_stop_conversation_tasks_kind_generate_only(monkeypatch):
    called = {"browse": False}

    async def fake_stop_browse(cid, bid=None, **kw):
        called["browse"] = True
        return []

    async def fake_list(db, cid, after_id=0, limit=1000):
        return []

    monkeypatch.setattr(chat_browse, "stop_browse_tasks", fake_stop_browse)
    monkeypatch.setattr(chat_tasks.chat_crud, "list_messages_after", fake_list)

    stopped = await chat_tasks.stop_conversation_tasks(
        None, conversation=SimpleNamespace(id=3), user_id=1, kind="generate",
    )
    assert stopped == {"browse": [], "generate": []}
    assert called["browse"] is False  # kind=generate 时不动探索任务
