"""034 US2：草稿补丁契约与修订（T020，先红）。"""
import pytest

from app.services import chat_gen
from core.chat.contracts import apply_draft_patch, validate_draft_patch
from tests.unit.chat.conftest import FakeConversation, FakeMessage


def _cases():
    return [
        {"draft_id": 1, "title": "登录成功", "steps": ["打开登录页", "输入账号"], "expected": "进入首页"},
        {"draft_id": 2, "title": "登录失败", "steps": ["打开登录页"], "expected": "提示错误"},
    ]


def test_validate_draft_patch_ops():
    patch, err = validate_draft_patch([
        {"op": "add", "after_index": 1, "case": {"title": "新用例", "steps": ["a", "b"], "expected": "e"}},
        {"op": "update", "draft_id": 1, "fields": {"expected": "进入首页(修正)"}},
        {"op": "remove", "draft_id": 2},
    ])
    assert err == "" and len(patch) == 3
    assert patch[0]["case"]["steps"] == ["a", "b"]


def test_validate_draft_patch_rejects_bad():
    assert validate_draft_patch([])[0] is None
    assert validate_draft_patch([{"op": "add", "case": {}}])[0] is None          # 缺 title
    assert validate_draft_patch([{"op": "update", "draft_id": "x", "fields": {"a": 1}}])[0] is None
    assert validate_draft_patch([{"op": "remove"}])[0] is None
    assert validate_draft_patch([{"op": "frobnicate"}])[0] is None


def test_apply_draft_patch_and_changes():
    cases = _cases()
    patch, _ = validate_draft_patch([
        {"op": "update", "draft_id": 1, "fields": {"expected": "E2"}},
        {"op": "remove", "draft_id": 2},
        {"op": "add", "after_index": 0, "case": {"title": "T3", "steps": ["s"], "expected": "e"}},
    ])
    new, changes, err = apply_draft_patch(cases, patch)
    assert err == ""
    assert [c.get("title") for c in new] == ["登录成功", "T3"]
    assert new[0]["expected"] == "E2"
    assert {c["kind"] for c in changes} == {"update", "remove", "add"}
    # 原始列表未被就地修改
    assert cases[0]["expected"] == "进入首页" and len(cases) == 2


def test_apply_draft_patch_missing_id_atomic():
    cases = _cases()
    patch, _ = validate_draft_patch([
        {"op": "update", "draft_id": 999, "fields": {"expected": "x"}},
        {"op": "remove", "draft_id": 1},
    ])
    new, changes, err = apply_draft_patch(cases, patch)
    assert new is None and "不存在" in err and changes == []


def test_steps_to_text_and_structured_recompute():
    assert chat_gen._steps_to_text(["打开登录页", "输入账号"]) == "1. 打开登录页\n2. 输入账号"
    structured = chat_gen._structured_from_steps(["点击【登录】"])
    assert isinstance(structured, list) and structured
    assert chat_gen._structured_from_steps(["完全无法解析的一句话？？？"]) is None  # 解析不出 → 不伪造


def test_draft_item_splits_steps():
    class Row:
        id = 1
        title = "T"
        test_steps = "1. a\n2. b"
        expected_result = "e"
        priority = "中"
        module = ""

    item = chat_gen._draft_item(Row())
    assert item["draft_id"] == 1 and item["steps"] == ["1. a", "2. b"] and item["title"] == "T"


@pytest.mark.asyncio
async def test_revise_drafts_handler_persists(monkeypatch):
    from app.crud import gen as gen_crud

    class Row:
        def __init__(self, rid, title, steps, expected):
            self.id = rid
            self.title = title
            self.test_steps = steps
            self.expected_result = expected
            self.priority = "中"
            self.module = ""

    store = {"rows": [Row(1, "登录成功", "1. 打开登录页", "进入首页"), Row(2, "登录失败", "1. 打开登录页", "提示错误")]}

    async def list_tcs(db, sid):
        return list(store["rows"])

    async def del_raw(db, rid):
        store["rows"] = [r for r in store["rows"] if r.id != rid]
        return True

    async def upd_raw(db, rid, fields):
        for r in store["rows"]:
            if r.id == rid:
                for k, v in fields.items():
                    setattr(r, k, v)
        return None

    async def create_raw(db, sid, **kw):
        r = Row(99, kw.get("title"), kw.get("test_steps"), kw.get("expected_result"))
        store["rows"].append(r)
        return r

    monkeypatch.setattr(gen_crud, "list_gen_test_cases", list_tcs)
    monkeypatch.setattr(gen_crud, "delete_gen_test_case_raw", del_raw)
    monkeypatch.setattr(gen_crud, "update_gen_test_case_raw", upd_raw)
    monkeypatch.setattr(gen_crud, "create_gen_test_case_raw", create_raw)
    # chat_gen 通过 app.crud 包命名空间调用，需同步打补丁
    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_tcs)
    monkeypatch.setattr(chat_gen.crud, "delete_gen_test_case_raw", del_raw)
    monkeypatch.setattr(chat_gen.crud, "update_gen_test_case_raw", upd_raw)
    monkeypatch.setattr(chat_gen.crud, "create_gen_test_case_raw", create_raw)

    conv = FakeConversation()
    msg = FakeMessage(
        conversation_id=conv.id, role="assistant",
        cards=[{"type": "draft_set", "gen_session_id": "s1", "case_kind": "ui", "status": "draft", "items": []}],
    )

    async def find_card(db, cid, sid):
        return msg, msg.cards[0]

    async def write_card(db, message, card):
        message.cards = [card]
        return message

    monkeypatch.setattr(chat_gen, "_find_card_message", find_card)
    monkeypatch.setattr(chat_gen, "_write_card", write_card)

    action = {
        "type": "revise_drafts", "gen_session_id": "s1",
        "patch": [
            {"op": "update", "draft_id": 1, "fields": {"expected": "E2"}},
            {"op": "remove", "draft_id": 2},
            {"op": "add", "after_index": 0, "case": {"title": "新增异常分支", "steps": ["点一下"], "expected": "有提示"}},
        ],
    }
    outcome = await chat_gen.revise_drafts_handler(None, conv, action, {})
    assert outcome.ok and "已更新" in outcome.note
    assert store["rows"][0].expected_result == "E2"
    assert len(store["rows"]) == 2  # 2 删除、新增 1 条
    assert any(r.title == "新增异常分支" for r in store["rows"])
    assert msg.cards[0]["last_changes"]


@pytest.mark.asyncio
async def test_revise_drafts_handler_missing_card(monkeypatch):
    conv = FakeConversation()

    async def find_card(db, cid, sid):
        return None, None

    monkeypatch.setattr(chat_gen, "_find_card_message", find_card)
    outcome = await chat_gen.revise_drafts_handler(
        None, conv, {"type": "revise_drafts", "gen_session_id": "nope", "patch": [{"op": "remove", "draft_id": 1}]}, {},
    )
    assert not outcome.ok and "找不到草稿集" in outcome.note
