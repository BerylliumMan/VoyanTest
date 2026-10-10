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
