# tests/unit/chat/conftest.py
# 034 对话工作台测试夹具：内存消息存储 + 可编程 fake LLM + sqlite 会话（仅 chat 表）。
import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from core.chat import orchestrator as orch


class FakeConversation:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.title = kw.get("title", "新对话")
        self.user_id = kw.get("user_id", 7)
        self.project_id = kw.get("project_id", None)
        self.assistant_agent_id = kw.get("assistant_agent_id", None)
        self.status = kw.get("status", "active")
        self.refs = kw.get("refs")
        self.summary = kw.get("summary")
        self.summarized_until_message_id = kw.get("summarized_until_message_id")


class FakeMessage:
    _seq = 0

    def __init__(self, conversation_id=1, role="user", content="", cards=None, refs=None,
                 actions=None, task_id=None):
        FakeMessage._seq += 1
        self.id = FakeMessage._seq
        self.conversation_id = conversation_id
        self.role = role
        self.content = content
        self.cards = cards
        self.refs = refs
        self.actions = actions
        self.task_id = task_id
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at


@pytest.fixture
def fake_conversation():
    return FakeConversation()


@pytest.fixture
def fake_llm_factory():
    """返回工厂：fake_llm_factory({"reply": "...", "actions": [...]}) 或字符串。"""

    def make(payload, *, raise_exc: Exception | None = None):
        calls = []

        async def _llm(messages):
            calls.append(messages)
            if raise_exc is not None:
                raise raise_exc
            if isinstance(payload, str):
                return payload
            return json.dumps(payload, ensure_ascii=False)

        _llm.calls = calls
        return _llm

    return make


@pytest.fixture
def chat_store(monkeypatch):
    """把 orchestrator 依赖的 chat_crud 换成内存实现（消息/会话更新记录）。"""
    store = {"messages": [], "conversation_updates": [], "created": []}

    async def list_messages_after(db, conversation_id, *, after_id=0, limit=500):
        return [m for m in store["messages"] if m.conversation_id == conversation_id and m.id > after_id]

    async def create_message(db, *, conversation_id, role, content=None, cards=None, refs=None,
                             actions=None, task_id=None):
        m = FakeMessage(conversation_id=conversation_id, role=role, content=content, cards=cards,
                        refs=refs, actions=actions, task_id=task_id)
        store["messages"].append(m)
        store["created"].append(m)
        return m

    async def update_conversation(db, conversation, **fields):
        for k, v in fields.items():
            setattr(conversation, k, v)
        store["conversation_updates"].append(fields)
        return conversation

    async def get_message(db, conversation_id, message_id):
        for m in store["messages"]:
            if m.conversation_id == conversation_id and m.id == message_id:
                return m
        return None

    async def delete_message(db, message):
        store["messages"].remove(message)

    async def update_message(db, message, *, content=None, cards=None, actions=None):
        if content is not None:
            message.content = content
        if cards is not None:
            message.cards = cards
        if actions is not None:
            message.actions = actions
        return message

    monkeypatch.setattr(orch.chat_crud, "list_messages_after", list_messages_after)
    monkeypatch.setattr(orch.chat_crud, "create_message", create_message)
    monkeypatch.setattr(orch.chat_crud, "update_conversation", update_conversation)
    monkeypatch.setattr(orch.chat_crud, "get_message", get_message)
    monkeypatch.setattr(orch.chat_crud, "delete_message", delete_message)
    monkeypatch.setattr(orch.chat_crud, "update_message", update_message)
    monkeypatch.setattr(orch, "resolve_system_prompt", _fake_resolve_system_prompt)
    return store


async def _fake_resolve_system_prompt(db, conversation, *, agent_id=None):
    return "SYSTEM"


@pytest_asyncio.fixture
async def chat_db():
    """仅建 chat 三表的 sqlite 内存库（CRUD 单测用）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.chat import CaseRevision, ChatConversation, ChatMessage

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: ChatConversation.__table__.create(sync_conn, checkfirst=True)
        )
        await conn.run_sync(
            lambda sync_conn: ChatMessage.__table__.create(sync_conn, checkfirst=True)
        )
        await conn.run_sync(
            lambda sync_conn: CaseRevision.__table__.create(sync_conn, checkfirst=True)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
