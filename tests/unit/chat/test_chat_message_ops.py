"""034 US1：多轮编排 / 消息操作（T012，先红）。"""
import pytest
import json

from core.chat import orchestrator as orch
from tests.unit.chat.conftest import FakeConversation


@pytest.mark.asyncio
async def test_handle_user_message_happy_path(chat_store, fake_llm_factory):
    conv = FakeConversation()
    llm = fake_llm_factory({
        "thinking": "用户在打招呼，我应介绍职责范围并给出可操作的引导；不需要执行动作。",
        "reply": "你好！我可以帮你生成、修改与执行用例。",
        "actions": [],
        "suggestions": ["生成一条 UI 用例"],
    })
    user_msg, assistant = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="你好", llm_call=llm,
    )
    assert user_msg.role == "user" and user_msg.content == "你好"
    assert assistant.role == "assistant" and "用例" in assistant.content
    assert "介绍职责" in assistant.thinking  # 思考过程随助手消息落库
    assert orch.serialize_message(assistant)["thinking"] == assistant.thinking  # API 输出含 thinking
    assert len(chat_store["messages"]) == 2


@pytest.mark.asyncio
async def test_multi_turn_context_carries_history(chat_store, fake_llm_factory):
    conv = FakeConversation()
    await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="第一轮内容",
        llm_call=fake_llm_factory({"reply": "R1", "actions": []}),
    )
    llm2 = fake_llm_factory({"reply": "R2", "actions": []})
    await orch.handle_user_message(None, conversation=conv, user_id=7, content="继续", llm_call=llm2)
    sent = llm2.calls[0]
    contents = [m["content"] for m in sent]
    assert any("第一轮内容" in c for c in contents) and any("R1" in c for c in contents)


@pytest.mark.asyncio
async def test_llm_failure_creates_readable_error(chat_store, fake_llm_factory):
    conv = FakeConversation()
    llm = fake_llm_factory({}, raise_exc=RuntimeError("boom"))
    _, assistant = await orch.handle_user_message(None, conversation=conv, user_id=7, content="hi", llm_call=llm)
    assert "重试" in assistant.content
    assert assistant.actions[0]["status"] == "error"


@pytest.mark.asyncio
async def test_parse_failure_creates_readable_error(chat_store, fake_llm_factory):
    conv = FakeConversation()
    _, assistant = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="hi", llm_call=fake_llm_factory('{"broken": '),
    )
    assert "未能理解" in assistant.content


@pytest.mark.asyncio
async def test_plain_text_reply_falls_back_to_content(chat_store, fake_llm_factory):
    conv = FakeConversation()
    reply = "批次 #460 并未失败，因此没有可归因的失败原因——直接对未失败的批次做分析会是无依据的臆测。"
    _, assistant = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="为什么失败", llm_call=fake_llm_factory(reply),
    )
    assert assistant.content == reply
    assert assistant.actions[0]["type"] == "text_fallback"


def test_text_fallback_guards():
    from core.chat.contracts import _text_fallback

    assert _text_fallback("普通自然语言回复") == "普通自然语言回复"
    assert _text_fallback("```\n普通回复\n```") == "普通回复"
    assert _text_fallback("") == ""
    assert _text_fallback('{"reply": "x"') == ""          # JSON 片段残留
    assert _text_fallback('{"actions": []}') == ""
    assert _text_fallback("x" * 5000) == ""


@pytest.mark.asyncio
async def test_rejected_and_unimplemented_actions_are_notes(chat_store, fake_llm_factory, monkeypatch):
    conv = FakeConversation()
    # 暂时摘掉 explain 处理器，模拟「能力尚未开放」分支（其余动作均已实现）
    saved = dict(orch.ACTION_HANDLERS)
    orch.ACTION_HANDLERS.pop("explain", None)
    try:
        llm = fake_llm_factory({
            "reply": "好的",
            "actions": [
                {"type": "nonsense"},
                {"type": "explain", "batch_id": 1},
            ],
        })
        _, assistant = await orch.handle_user_message(None, conversation=conv, user_id=7, content="跑一下", llm_call=llm)
    finally:
        orch.ACTION_HANDLERS.update(saved)
    assert "已忽略" in assistant.content
    assert "尚未开放" in assistant.content
    assert [a["status"] for a in assistant.actions] == ["rejected", "unimplemented"]


@pytest.mark.asyncio
async def test_regenerate_replaces_in_place_and_keeps_later(chat_store, fake_llm_factory):
    conv = FakeConversation()
    _, a1 = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="hi",
        llm_call=fake_llm_factory({"thinking": "第一版思考", "reply": "第一版", "actions": []}),
    )
    assert a1.thinking == "第一版思考"
    await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="继续",
        llm_call=fake_llm_factory({"reply": "后续", "actions": []}),
    )
    llm3 = fake_llm_factory({"thinking": "第二版思考", "reply": "第二版", "actions": []})
    updated, err = await orch.regenerate_message(
        None, conversation=conv, message_id=a1.id, user_id=7, llm_call=llm3,
    )
    assert err == "" and updated.content == "第二版" and updated.id == a1.id
    assert updated.thinking == "第二版思考"  # 重新生成时思考同步替换
    # 后续消息不受影响
    assert any(m.content == "后续" for m in chat_store["messages"])
    # 重新生成使用该条之前的历史（含第一轮用户消息）
    assert any("hi" in m["content"] for m in llm3.calls[0])


@pytest.mark.asyncio
async def test_regenerate_rejects_user_message(chat_store, fake_llm_factory):
    conv = FakeConversation()
    user_msg, _ = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="hi",
        llm_call=fake_llm_factory({"reply": "R", "actions": []}),
    )
    updated, err = await orch.regenerate_message(
        None, conversation=conv, message_id=user_msg.id, user_id=7,
        llm_call=fake_llm_factory({"reply": "X", "actions": []}),
    )
    assert updated is None and "助手消息" in err


@pytest.mark.asyncio
async def test_delete_message_removes_from_context(chat_store, fake_llm_factory):
    conv = FakeConversation()
    _, a1 = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="hi",
        llm_call=fake_llm_factory({"reply": "R1-内容", "actions": []}),
    )
    ok, err = await orch.delete_message(None, conversation=conv, message_id=a1.id)
    assert ok and err == ""

    llm2 = fake_llm_factory({"reply": "R2", "actions": []})
    await orch.handle_user_message(None, conversation=conv, user_id=7, content="继续", llm_call=llm2)
    assistant_texts = [m["content"] for m in llm2.calls[0] if m["role"] == "assistant"]
    assert all("R1-内容" not in t for t in assistant_texts)


@pytest.mark.asyncio
async def test_text_fallback_retries_once_for_json(chat_store):
    """模型第一轮只回自然语言 → 自动纠错重试一次；第二轮合法 JSON 按合同执行。"""
    from core.chat import orchestrator as _orch

    calls = []

    async def _llm(messages):
        calls.append(messages)
        if len(calls) == 1:
            return "好的，已按当前对话默认设置提交执行。"
        return json.dumps({
            "thinking": "上一轮漏了 JSON，本轮补齐动作",
            "reply": "已处理",
            "actions": [],
        }, ensure_ascii=False)

    conv = FakeConversation()
    _, assistant = await _orch.handle_user_message(
        None, conversation=conv, user_id=7, content="执行用例集 冒烟", llm_call=_llm,
    )
    assert len(calls) == 2
    assert "只输出一个 JSON 对象" in calls[1][-1]["content"]
    assert not assistant.actions or assistant.actions[0].get("type") != "text_fallback"
    assert assistant.content == "已处理"


@pytest.mark.asyncio
async def test_text_fallback_keeps_second_failure_with_raw(chat_store):
    from core.chat import orchestrator as _orch

    async def _llm(messages):
        return "第一轮自然语言"

    conv = FakeConversation()
    _, assistant = await _orch.handle_user_message(
        None, conversation=conv, user_id=7, content="执行", llm_call=_llm,
    )
    assert assistant.actions and assistant.actions[0]["type"] == "text_fallback"
    assert "raw" in assistant.actions[0]
