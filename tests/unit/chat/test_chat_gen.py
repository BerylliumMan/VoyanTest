"""034 US2：生成服务辅助逻辑 / 串行守卫 / 导入错误路径（T021，先红）。"""
import os

import pytest

from app.services import chat_gen
from tests.unit.chat.conftest import FakeConversation, FakeMessage


def test_resolve_chat_file_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data/chat_uploads/x", exist_ok=True)
    with open("data/chat_uploads/x/a.md", "w") as fh:
        fh.write("hi")
    assert chat_gen._resolve_chat_file("data/chat_uploads/x/a.md") is not None
    assert chat_gen._resolve_chat_file("data/chat_uploads/../secret.md") is None  # 路径穿越
    assert chat_gen._resolve_chat_file("/etc/passwd") is None
    assert chat_gen._resolve_chat_file("data/chat_uploads/x/missing.md") is None
    assert chat_gen._resolve_chat_file("") is None


@pytest.mark.asyncio
async def test_has_active_generation_guard(monkeypatch):
    conv = FakeConversation()
    running = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "s1", "status": "generating", "items": []},
    ])
    done = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "s0", "status": "draft", "items": []},
    ])

    async def list_after(db, cid, **kw):
        return [done]

    monkeypatch.setattr(chat_gen.chat_crud, "list_messages_after", list_after)
    assert await chat_gen._has_active_generation(None, conv.id) is False

    async def list_after2(db, cid, **kw):
        return [done, running]

    monkeypatch.setattr(chat_gen.chat_crud, "list_messages_after", list_after2)
    assert await chat_gen._has_active_generation(None, conv.id) is True


@pytest.mark.asyncio
async def test_generate_handler_rejects_when_active(monkeypatch):
    conv = FakeConversation()

    async def has_active(db, cid):
        return True

    monkeypatch.setattr(chat_gen, "_has_active_generation", has_active)
    outcome = await chat_gen.generate_handler(
        None, conv,
        {"type": "generate", "kind": "ui", "source": "text", "payload": {"description": "x"}},
        {"user_id": 7},
    )
    assert not outcome.ok and "已有生成任务进行中" in outcome.note


@pytest.mark.asyncio
async def test_import_drafts_error_paths(monkeypatch):
    conv = FakeConversation()

    async def find_card_none(db, cid, sid):
        return None, None

    monkeypatch.setattr(chat_gen, "_find_card_message", find_card_none)
    result, err = await chat_gen.import_drafts(None, conversation=conv, gen_session_id="s")
    assert result is None and "草稿集不存在" in err

    msg = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "s1", "status": "draft", "items": []},
    ])

    async def find_card_ok(db, cid, sid):
        return msg, msg.cards[0]

    monkeypatch.setattr(chat_gen, "_find_card_message", find_card_ok)

    class Rec:
        status = "analyzing"
        case_kind = "ui"
        imported_count = 0

    async def get_session(db, sid):
        return Rec()

    monkeypatch.setattr(chat_gen.crud, "get_gen_session", get_session)
    result, err = await chat_gen.import_drafts(None, conversation=conv, gen_session_id="s1")
    assert result is None and "尚未完成" in err


# ----------------------------
# watcher（035 回归：卡片消息滞后落库）
# ----------------------------

@pytest.mark.asyncio
async def test_watch_generation_tolerates_late_card_message(monkeypatch):
    """035 回归：工具循环下卡片消息滞后落库时，生成 watcher 应等待而非早退。"""
    conv_id = 3
    session_id = "s-late"
    msg = FakeMessage(conversation_id=conv_id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": session_id, "status": "generating", "items": []},
    ])
    writes = []
    state = {"i": 0}

    class Record:
        status = "completed"
        progress = 100
        progress_message = "分析完成"
        error_message = None

    async def fake_sleep(sec):
        state["i"] += 1

    async def get_gen_session(db, sid):
        return Record()

    async def find_card(db, cid, sid):
        if state["i"] < 3:
            return None, None
        return msg, msg.cards[0]

    async def list_gen_test_cases(db, sid):
        class TC:
            id = 1
            title = "用例A"
            test_steps = "打开页面\n点击登录"
            expected_result = "跳转仪表盘"
            priority = "P0"
            module = "登录"
        return [TC()]

    async def write_card(db, m, card):
        writes.append(card)
        m.cards = [card]
        return m

    async def noop_emit(cid, ev):
        return None

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession()

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())
    monkeypatch.setattr(chat_gen.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chat_gen.crud, "get_gen_session", get_gen_session)
    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_gen_test_cases)
    monkeypatch.setattr(chat_gen, "_find_card_message", find_card)
    monkeypatch.setattr(chat_gen, "_write_card", write_card)
    monkeypatch.setattr(chat_gen, "emit", noop_emit)

    await chat_gen._watch_generation(conv_id, session_id, max_iterations=10)
    assert len(writes) == 1
    assert writes[0]["status"] == "draft"
    assert writes[0]["items"][0]["title"] == "用例A"


@pytest.mark.asyncio
async def test_watch_generation_gives_up_after_grace(monkeypatch):
    conv_id = 3
    session_id = "s-never"
    state = {"finds": 0}

    class Record:
        status = "analyzing"
        progress = 10
        progress_message = ""
        error_message = None

    async def fake_sleep(sec):
        return None

    async def get_gen_session(db, sid):
        return Record()

    async def find_card(db, cid, sid):
        state["finds"] += 1
        return None, None

    async def noop_emit(cid, ev):
        return None

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession()

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())
    monkeypatch.setattr(chat_gen.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chat_gen.crud, "get_gen_session", get_gen_session)
    monkeypatch.setattr(chat_gen, "_find_card_message", find_card)
    monkeypatch.setattr(chat_gen, "emit", noop_emit)

    await chat_gen._watch_generation(conv_id, session_id, max_iterations=100)
    assert state["finds"] == 60
