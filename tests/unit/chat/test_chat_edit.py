"""034 US3：改写补丁 / 对照渲染 / 基线双检 / 应用与放弃（T028，先红）。"""
import pytest

from app.services import chat_edit
from core.chat.contracts import apply_case_patch, validate_case_patch
from tests.unit.chat.conftest import FakeConversation, FakeMessage


def _steps():
    return [
        {"description": "打开登录页", "expected": ""},
        {"description": "输入用户名", "expected": "回显用户名"},
        {"description": "点击 Login", "expected": "进入商品页"},
    ]


def test_validate_case_patch_ops():
    patch, err = validate_case_patch({
        "steps": [
            {"op": "update", "index": 2, "fields": {"description": "点击【Login】等待跳转", "expected": "进入商品页"}},
            {"op": "add", "after_index": 2, "step": {"description": "断言页面包含 Products", "expected": ""}},
            {"op": "remove", "index": 1},
        ],
        "case_fields": {"priority": "high"},
    })
    assert err == "" and len(patch["steps"]) == 3
    assert patch["case_fields"]["priority"] == "high"


def test_validate_case_patch_rejects_bad():
    assert validate_case_patch({})[0] is None
    assert validate_case_patch({"steps": [{"op": "update", "index": 0, "fields": {"description": "  "}}]})[0] is None
    assert validate_case_patch({"steps": [{"op": "update", "index": 0, "fields": {"nope": 1}}]})[0] is None
    assert validate_case_patch({"steps": [{"op": "frobnicate"}]})[0] is None


def test_apply_case_patch_original_index_semantics():
    steps = _steps()
    patch, _ = validate_case_patch({"steps": [
        {"op": "remove", "index": 1},
        {"op": "update", "index": 2, "fields": {"expected": "进入商品列表页"}},
        {"op": "add", "after_index": 0, "step": {"description": "断言标题 Swag Labs"}},
    ]})
    new, changes, err = apply_case_patch(steps, patch)
    assert err == ""
    assert [s["description"] for s in new] == ["打开登录页", "断言标题 Swag Labs", "点击 Login"]
    assert new[2]["expected"] == "进入商品列表页"
    kinds = [c["kind"] for c in changes]
    assert kinds.count("remove") == 1 and kinds.count("update") == 1 and kinds.count("add") == 1


def test_apply_case_patch_out_of_range_atomic():
    steps = _steps()
    patch, _ = validate_case_patch({"steps": [{"op": "update", "index": 9, "fields": {"description": "x"}}]})
    new, changes, err = apply_case_patch(steps, patch)
    assert new is None and "超出范围" in err and changes == []


class FakeStep:
    def __init__(self, order, desc, expected=""):
        self.step_order = order
        self.description = desc
        self.parsed_result = expected


class FakeCase:
    def __init__(self):
        from datetime import datetime, timezone

        self.id = 100
        self.name = "登录用例"
        self.project_id = 30
        self.module_id = None
        self.description = None
        self.priority = "medium"
        self.updated_at = datetime.now(timezone.utc)
        self.steps = [
            FakeStep(1, "打开登录页"),
            FakeStep(2, "输入用户名", "回显用户名"),
            FakeStep(3, "点击 Login", "进入商品页"),
        ]


def _case_fixture(monkeypatch):
    case = FakeCase()

    async def load(db, cid):
        return case if cid == case.id else None

    monkeypatch.setattr(chat_edit, "_load_case_with_steps", load)
    return case


def _update_action():
    return {
        "type": "edit_case",
        "case_id": 100,
        "patch": {"steps": [{"op": "update", "index": 2, "fields": {"description": "点击【Login】等待跳转"}}]},
        "summary": "第 3 步改成等待跳转",
    }


def _wire_message(monkeypatch, conv, card):
    msg = FakeMessage(conversation_id=conv.id, role="assistant", cards=[card])

    async def get_message(db, cid, mid):
        return msg if mid == msg.id else None

    async def update_cards(db, m, cards):
        m.cards = cards
        return m

    async def noop_emit(cid, ev):
        return None

    monkeypatch.setattr(chat_edit.chat_crud, "get_message", get_message)
    monkeypatch.setattr(chat_edit.chat_crud, "update_message_cards", update_cards)
    monkeypatch.setattr(chat_edit, "emit", noop_emit)
    return msg


@pytest.mark.asyncio
async def test_edit_case_handler_builds_diff_card(monkeypatch):
    _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    outcome = await chat_edit.edit_case_handler(None, conv, _update_action(), {})
    assert outcome.ok and outcome.card["type"] == "diff" and outcome.card["status"] == "pending"
    assert outcome.card["base_hash"] and outcome.card["base_updated_at"]
    ch = outcome.card["changes"][0]
    assert ch["kind"] == "update" and ch["before"] == "点击 Login" and ch["after"] == "点击【Login】等待跳转"


@pytest.mark.asyncio
async def test_edit_case_handler_corrects_bad_index_via_llm(monkeypatch):
    """索引抖动：模型给出越界 index → 带步骤清单自动修正一次。"""
    import json as _json

    _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    bad = {
        "type": "edit_case", "case_id": 100,
        "patch": {"steps": [{"op": "update", "index": 3, "fields": {"description": "X"}}]},
        "summary": "s",
    }

    async def llm(messages):
        return _json.dumps({"steps": [{"op": "update", "index": 2, "fields": {"description": "点击【Login】等待跳转"}}]})

    outcome = await chat_edit.edit_case_handler(None, conv, bad, {"llm": llm, "user_id": 7})
    assert outcome.ok and outcome.card["type"] == "diff"
    assert outcome.card["changes"][0]["after"] == "点击【Login】等待跳转"


@pytest.mark.asyncio
async def test_edit_case_handler_bad_index_without_llm_fails(monkeypatch):
    _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    bad = {
        "type": "edit_case", "case_id": 100,
        "patch": {"steps": [{"op": "update", "index": 9, "fields": {"description": "X"}}]},
    }
    outcome = await chat_edit.edit_case_handler(None, conv, bad, {})
    assert not outcome.ok and "超出范围" in outcome.note


@pytest.mark.asyncio
async def test_edit_case_handler_rejects_other_project(monkeypatch):
    _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=99)
    outcome = await chat_edit.edit_case_handler(None, conv, _update_action(), {})
    assert not outcome.ok and "不属于当前对话" in outcome.note


@pytest.mark.asyncio
async def test_edit_case_handler_no_change(monkeypatch):
    _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    action = {
        "type": "edit_case", "case_id": 100,
        "patch": {"steps": [{"op": "update", "index": 2, "fields": {"description": "点击 Login"}}]},
    }
    outcome = await chat_edit.edit_case_handler(None, conv, action, {})
    assert not outcome.ok and "未产生任何变化" in outcome.note


@pytest.mark.asyncio
async def test_apply_diff_fresh_applies_with_revision(monkeypatch):
    case = _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    outcome = await chat_edit.edit_case_handler(None, conv, _update_action(), {})
    msg = _wire_message(monkeypatch, conv, outcome.card)

    calls = {}

    async def fake_update(db, case_id, body, **kw):
        calls["kw"] = kw
        calls["body"] = body
        return case

    async def fake_list_rev(db, case_id, limit=1):
        return [type("R", (), {"id": 7})()]

    monkeypatch.setattr(chat_edit.crud, "update_test_case", fake_update)
    monkeypatch.setattr(chat_edit.chat_crud, "list_case_revisions", fake_list_rev)

    result, err = await chat_edit.apply_diff(None, conversation=conv, message_id=msg.id, card_index=0, user_id=7)
    assert err == "" and result["card"]["status"] == "applied" and result["revision_id"] == 7
    assert calls["kw"]["revision_source"] == "chat" and calls["kw"]["revision_conversation_id"] == conv.id
    assert calls["kw"]["revision_user_id"] == 7
    assert calls["body"].steps[2].description == "点击【Login】等待跳转"
    assert msg.cards[0]["status"] == "applied"


@pytest.mark.asyncio
async def test_apply_diff_stale_when_case_changed(monkeypatch):
    case = _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    outcome = await chat_edit.edit_case_handler(None, conv, _update_action(), {})
    msg = _wire_message(monkeypatch, conv, outcome.card)

    calls = {}

    async def fake_update(db, case_id, body, **kw):
        calls["kw"] = kw
        return case

    monkeypatch.setattr(chat_edit.crud, "update_test_case", fake_update)

    # 对照生成后，用例被页面/他人修改
    case.steps[2].description = "点击 Login 并等待"

    result, err = await chat_edit.apply_diff(None, conversation=conv, message_id=msg.id, card_index=0, user_id=7)
    assert err == "" and result["stale"] is True
    assert result["card"]["status"] == "pending" and result["card"]["stale"] is True
    assert "kw" not in calls  # 未写回
    assert msg.cards[0]["stale"] is True


@pytest.mark.asyncio
async def test_discard_diff(monkeypatch):
    _case_fixture(monkeypatch)
    conv = FakeConversation(project_id=30)
    outcome = await chat_edit.edit_case_handler(None, conv, _update_action(), {})
    msg = _wire_message(monkeypatch, conv, outcome.card)

    result, err = await chat_edit.discard_diff(None, conversation=conv, message_id=msg.id, card_index=0)
    assert err == "" and result["card"]["status"] == "discarded"
    # 已处理 → 再次放弃报错
    result2, err2 = await chat_edit.discard_diff(None, conversation=conv, message_id=msg.id, card_index=0)
    assert result2 is None and "已处理" in err2
