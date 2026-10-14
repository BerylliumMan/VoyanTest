"""034 迭代二：停止在途回复 + 编辑消息重生成（2026-10-14 实测反馈）。

- core/chat/turns.py：在途轮次注册表（发送/编辑/重生成共用，可停止）；
- orchestrator.edit_message：更新内容 → 删除其后消息 → 以新内容重跑本轮。
"""
import asyncio
import json

import pytest

from core.chat import orchestrator as orch
from core.chat import turns as chat_turns
from tests.unit.chat.conftest import FakeConversation


# ----------------------------
# 在途轮次注册表
# ----------------------------

@pytest.mark.asyncio
async def test_registry_cancel_and_unregister():
    async def _work():
        await asyncio.sleep(30)

    task = asyncio.create_task(_work())
    await asyncio.sleep(0)
    chat_turns.register_turn(9, task)
    assert chat_turns.has_turn(9) and chat_turns.get_turn(9) is task
    assert chat_turns.cancel_turn(9) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not chat_turns.has_turn(9)
    # 已取消/已完成的任务：取消返回 False，不误报
    assert chat_turns.cancel_turn(9) is False
    chat_turns.unregister_turn(9, task)  # 幂等


@pytest.mark.asyncio
async def test_registry_unregister_only_own_task():
    async def _work():
        await asyncio.sleep(30)

    t1 = asyncio.create_task(_work())
    t2 = asyncio.create_task(_work())
    await asyncio.sleep(0)
    chat_turns.register_turn(11, t1)
    chat_turns.register_turn(11, t2)  # 后来者覆盖
    chat_turns.unregister_turn(11, t1)  # 旧任务结束 → 不能误删新任务
    assert chat_turns.get_turn(11) is t2
    chat_turns.unregister_turn(11, t2)
    assert not chat_turns.has_turn(11)
    t1.cancel()
    t2.cancel()
    for t in (t1, t2):
        with pytest.raises(asyncio.CancelledError):
            await t


# ----------------------------
# edit_message：编辑并重新生成
# ----------------------------

async def _turn(conv, llm_payload, content):
    return await orch.handle_user_message(
        None, conversation=conv, user_id=7, content=content,
        llm_call=await _fake_llm(llm_payload),
    )


async def _fake_llm(payload):
    async def _llm(messages):
        return json.dumps(payload, ensure_ascii=False)

    return _llm


@pytest.mark.asyncio
async def test_edit_message_truncates_later_and_regenerates(chat_store):
    conv = FakeConversation()
    u1, a1 = await _turn(conv, {"reply": "R1", "actions": []}, "第一版")
    await _turn(conv, {"reply": "R2", "actions": []}, "第二轮")

    updated, assistant, err = await orch.edit_message(
        None, conversation=conv, message_id=u1.id, content="第一版（改）", user_id=7,
        llm_call=await _fake_llm({"reply": "R1-new", "actions": []}),
    )
    assert err == "" and updated.id == u1.id and updated.content == "第一版（改）"
    assert assistant.content == "R1-new"
    # 其后消息（a1/第二轮的 user+assistant）全部清除
    got = [(m.role, m.content) for m in chat_store["messages"]]
    assert got == [("user", "第一版（改）"), ("assistant", "R1-new")]


@pytest.mark.asyncio
async def test_edit_message_rejects_assistant_empty_and_missing(chat_store):
    conv = FakeConversation()
    u1, a1 = await _turn(conv, {"reply": "R1", "actions": []}, "内容")

    _, _, err = await orch.edit_message(
        None, conversation=conv, message_id=a1.id, content="x", user_id=7,
    )
    assert "用户消息" in err

    _, _, err2 = await orch.edit_message(
        None, conversation=conv, message_id=u1.id, content="   ", user_id=7,
    )
    assert "不能为空" in err2

    _, _, err3 = await orch.edit_message(
        None, conversation=conv, message_id=99999, content="x", user_id=7,
    )
    assert "不存在" in err3


@pytest.mark.asyncio
async def test_edit_message_emits_deleted_updated_created(chat_store, monkeypatch):
    from core.chat import events as chat_events

    events: list[tuple[str, dict]] = []

    async def _hook(conversation_id, ev):
        events.append((ev.get("type"), ev))

    monkeypatch.setattr(chat_events, "_push_hooks", [_hook])
    conv = FakeConversation()
    u1, _a1 = await _turn(conv, {"reply": "R1", "actions": []}, "第一版")
    u2, _a2 = await _turn(conv, {"reply": "R2", "actions": []}, "第二轮")
    events.clear()

    await orch.edit_message(
        None, conversation=conv, message_id=u1.id, content="改", user_id=7,
        llm_call=await _fake_llm({"reply": "R-new", "actions": []}),
    )
    kinds = [k for k, _ in events]
    # 先销毁其后消息（a1/u2/a2 共 3 条），再更新被编辑消息，最后落助手回复
    assert kinds.count("message.deleted") == 3
    assert kinds.index("message.updated") > kinds.index("message.deleted")
    assert kinds[-1] == "message.created"
    updated_ev = next(ev for k, ev in events if k == "message.updated")
    assert updated_ev["message"]["content"] == "改"
    # 被编辑消息的 id 保持原值（不新建用户消息）
    assert updated_ev["message"]["id"] == u1.id


@pytest.mark.asyncio
async def test_edit_message_llm_failure_keeps_readable_error(chat_store):
    conv = FakeConversation()
    u1, _a1 = await _turn(conv, {"reply": "R1", "actions": []}, "第一版")

    async def _boom(messages):
        raise RuntimeError("gateway down")

    updated, assistant, err = await orch.edit_message(
        None, conversation=conv, message_id=u1.id, content="改后", user_id=7, llm_call=_boom,
    )
    assert err == "" and updated.content == "改后"
    assert "重试" in assistant.content  # 助手错误消息落库（handle_user_message 兜底路径）
    got = [(m.role, m.content) for m in chat_store["messages"]]
    assert got[0] == ("user", "改后") and got[-1][0] == "assistant"
