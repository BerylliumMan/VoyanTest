"""034 US6：引用机制 —— 校验（存在性/权限）/去重合并/失效剔除（T049）。"""
import pytest

from app.services import chat_refs
from tests.unit.chat.conftest import FakeConversation


class FakeUser:
    def __init__(self, role="admin", project_ids=None):
        self.id = 7
        self.role = role
        self.project_ids = project_ids


def test_merge_refs_dedupe_and_remove():
    current = [{"type": "case", "id": 1, "label": "A"}, {"type": "batch", "id": 9, "label": "B"}]
    merged = chat_refs.merge_refs(
        current,
        add=[{"type": "case", "id": 2, "label": "C"}, {"type": "case", "id": 1, "label": "A2"}],
        remove=[{"type": "batch", "id": 9}],
    )
    assert [r["id"] for r in merged] == [1, 2]
    assert merged[0]["label"] == "A"  # 已存在不覆盖
    assert chat_refs.merge_refs([], [], []) == []


@pytest.mark.asyncio
async def test_validate_refs_admin_accepts_existing(monkeypatch):
    from app import crud

    class Case:
        project_id = 30

    async def get_tc(db, i):
        return Case() if i == 662 else None

    monkeypatch.setattr(crud, "get_test_case", get_tc)
    valid, problems = await chat_refs.validate_refs(
        None, [{"type": "case", "id": 662, "label": "T"}], FakeUser(role="admin"),
    )
    assert problems == [] and valid[0]["project_id"] == 30

    valid2, problems2 = await chat_refs.validate_refs(
        None, [{"type": "case", "id": 999, "label": "X"}], FakeUser(role="admin"),
    )
    assert valid2 == [] and "不存在" in problems2[0]


@pytest.mark.asyncio
async def test_validate_refs_rejects_foreign_project(monkeypatch):
    from app import crud
    from app.auth import get_user_project_filter as _real

    class Case:
        project_id = 99

    async def get_tc(db, i):
        return Case()

    monkeypatch.setattr(crud, "get_test_case", get_tc)
    # 普通用户项目过滤：仅项目 30
    monkeypatch.setattr(
        "app.auth.get_user_project_filter",
        lambda user: [30],
    )
    valid, problems = await chat_refs.validate_refs(
        None, [{"type": "case", "id": 662, "label": "T"}], FakeUser(role="user"),
    )
    assert valid == [] and "无权引用" in problems[0]


@pytest.mark.asyncio
async def test_set_refs_handler_persists(monkeypatch):
    from app import db_models
    from app.crud import chat as chat_crud

    conv = FakeConversation(project_id=30)
    updates = {}

    class User:
        id = 7

    async def db_get(model, i):
        return User()

    class FakeDB:
        async def get(self, model, i):
            return User()

    async def update_conv(db, conversation, **fields):
        updates.update(fields)
        conversation.refs = fields.get("refs")
        return conversation

    async def validate(db, refs, user):
        return [{"type": "case", "id": 662, "label": "T", "project_id": 30}], []

    monkeypatch.setattr(chat_refs, "validate_refs", validate)
    monkeypatch.setattr(chat_crud, "update_conversation", update_conv)

    outcome = await chat_refs.set_refs_handler(
        FakeDB(), conv,
        {"type": "set_refs", "add": [{"type": "case", "id": 662, "label": "T"}], "remove": []},
        {"user_id": 7},
    )
    assert outcome.ok and "引用已更新" in outcome.note
    assert updates["refs"][0]["id"] == 662


@pytest.mark.asyncio
async def test_set_refs_handler_rejects_all_invalid(monkeypatch):
    conv = FakeConversation(project_id=30)

    class User:
        id = 7

    class FakeDB:
        async def get(self, model, i):
            return User()

    async def validate(db, refs, user):
        return [], ["无权引用用例 #999（项目 99）"]

    monkeypatch.setattr(chat_refs, "validate_refs", validate)
    outcome = await chat_refs.set_refs_handler(
        FakeDB(), conv,
        {"type": "set_refs", "add": [{"type": "case", "id": 999, "label": "X"}], "remove": []},
        {"user_id": 7},
    )
    assert not outcome.ok and "未生效" in outcome.note


@pytest.mark.asyncio
async def test_prune_invalid_refs(monkeypatch):
    from app import crud

    class Case:
        project_id = 30

    class Batch:
        project_id = 30

    async def get_tc(db, i):
        return Case() if i == 1 else None

    async def get_batch(db, i):
        return Batch() if i == 9 else None

    monkeypatch.setattr(crud, "get_test_case", get_tc)
    monkeypatch.setattr(crud, "get_run_batch", get_batch)
    valid, dropped = await chat_refs.prune_invalid_refs(None, [
        {"type": "case", "id": 1}, {"type": "case", "id": 2}, {"type": "batch", "id": 9},
    ])
    assert [r["id"] for r in valid] == [1, 9] and dropped == ["case#2"]
