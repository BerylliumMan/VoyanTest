"""034 迭代（2026-10-10）：对话内执行用例集（run_case.suite_id）+ 上下文类修改不重排列表。"""
from types import SimpleNamespace

import pytest

from core.chat.contracts import validate_action
from tests.unit.chat.conftest import FakeConversation


# ----------------------------
# 契约校验
# ----------------------------

def test_validate_suite_id_only():
    norm, err = validate_action({"type": "run_case", "suite_id": 12})
    assert err == ""
    assert norm["suite_id"] == 12 and norm["case_ids"] == []


def test_validate_suite_and_cases_combined():
    norm, err = validate_action({"type": "run_case", "case_ids": [1], "suite_id": 12})
    assert err == ""
    assert norm["suite_id"] == 12 and norm["case_ids"] == [1]


def test_validate_requires_something():
    norm, err = validate_action({"type": "run_case"})
    assert norm is None and "case_ids" in err


def test_validate_suite_id_positive():
    norm, err = validate_action({"type": "run_case", "suite_id": -3})
    assert norm is None and "suite_id" in err
    norm, err = validate_action({"type": "run_case", "suite_id": "x"})
    assert norm is None and "suite_id" in err


# ----------------------------
# start_run：用例集展开
# ----------------------------

class FakeCase:
    def __init__(self, cid, project_id=30, case_kind="ui", name=None):
        self.id = cid
        self.project_id = project_id
        self.case_kind = case_kind
        self.name = name or f"用例{cid}"


@pytest.mark.asyncio
async def test_start_run_expands_suite(monkeypatch):
    from app.services import chat_run

    monkeypatch.setattr(chat_run, "_active_run_count", _afake(0))

    async def _get_suite(db, sid):
        assert sid == 12
        return SimpleNamespace(id=12, name="登录冒烟", project_id=30)

    async def _ordered(db, sid):
        return [675, 676]

    async def _get_case(db, cid):
        return FakeCase(cid)

    monkeypatch.setattr(chat_run.crud, "get_suite", _get_suite)
    monkeypatch.setattr(chat_run.crud, "get_suite_ordered_case_ids", _ordered)
    monkeypatch.setattr(chat_run.crud, "get_test_case", _get_case)
    monkeypatch.setattr(chat_run, "_resolve_environment", _a2fake(None, ""))
    monkeypatch.setattr(chat_run, "_load_user", _afake(SimpleNamespace(id=7)))
    monkeypatch.setattr(chat_run, "_invoke_batch", _afake(999))

    conv = FakeConversation(project_id=30, agent_id=None)
    card, err, task_id = await chat_run.start_run(
        None, conversation=conv, case_ids=[], mode="server", agent_id=None,
        user_id=7, suite_id=12,
    )
    assert err == "" and card is not None
    assert card["suite_id"] == 12 and card["suite_name"] == "登录冒烟"
    assert card["case_ids"] == [675, 676]
    assert task_id == "999"


@pytest.mark.asyncio
async def test_start_run_suite_project_mismatch(monkeypatch):
    from app.services import chat_run

    monkeypatch.setattr(chat_run, "_active_run_count", _afake(0))

    async def _get_suite(db, sid):
        return SimpleNamespace(id=12, name="别的项目", project_id=99)

    monkeypatch.setattr(chat_run.crud, "get_suite", _get_suite)
    conv = FakeConversation(project_id=30)
    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[], mode="server", agent_id=None,
        user_id=7, suite_id=12,
    )
    assert card is None and "不属于" in err


@pytest.mark.asyncio
async def test_start_run_suite_empty(monkeypatch):
    from app.services import chat_run

    monkeypatch.setattr(chat_run, "_active_run_count", _afake(0))

    async def _get_suite(db, sid):
        return SimpleNamespace(id=12, name="空集", project_id=30)

    async def _ordered(db, sid):
        return []

    monkeypatch.setattr(chat_run.crud, "get_suite", _get_suite)
    monkeypatch.setattr(chat_run.crud, "get_suite_ordered_case_ids", _ordered)
    conv = FakeConversation(project_id=30)
    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[], mode="server", agent_id=None,
        user_id=7, suite_id=12,
    )
    assert card is None and "没有用例" in err


# ----------------------------
# 上下文类修改不重排列表（preserve_updated_at；真实 sqlite ORM 验证 onupdate 语义）
# ----------------------------

@pytest.mark.asyncio
async def test_update_conversation_preserves_updated_at(chat_db):
    from datetime import datetime, timezone

    from app.crud.chat import create_conversation, update_conversation

    conv = await create_conversation(
        chat_db, user_id=7, project_id=30, title="t", case_kind="ui",
    )
    conv.updated_at = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)
    await chat_db.commit()
    old = conv.updated_at.replace(tzinfo=None)

    updated = await update_conversation(chat_db, conv, preserve_updated_at=True, agent_id=26)
    assert updated.agent_id == 26
    assert updated.updated_at.replace(tzinfo=None) == old  # 未被 onupdate 刷新

    # 非 preserve（标题类）→ onupdate 刷新时间戳
    updated2 = await update_conversation(chat_db, updated, title="t2")
    assert updated2.updated_at.replace(tzinfo=None) > old


# ----------------------------
# helpers
# ----------------------------

def _afake(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _a2fake(v1, v2):
    async def _fn(*a, **k):
        return v1, v2
    return _fn
