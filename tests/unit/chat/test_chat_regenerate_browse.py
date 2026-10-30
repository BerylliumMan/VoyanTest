"""044 缺陷修复：重新生成/编辑前必须停止进行中的浏览器探索任务。

根因假设：browse 任务与对话 turn 是两套独立任务；regenerate 只重建消息，
不停止旧 browse 任务。两个探索同时驱动同一个浏览器会话（客户端 MCP 单进程），
第二个探索拿不到可用浏览器。
"""

import pytest

from core.chat import orchestrator as orch

from tests.unit.chat.conftest import FakeConversation  # noqa: F401


@pytest.fixture
def stop_spy(monkeypatch):
    calls: list[tuple[int, str | None]] = []

    async def _stop(conversation_id, browse_id=None, **kwargs):
        calls.append((conversation_id, browse_id))
        return []

    from app.services import chat_browse

    monkeypatch.setattr(chat_browse, "stop_browse_tasks", _stop)
    return calls


@pytest.mark.asyncio
async def test_regenerate_stops_running_browse_tasks(chat_store, fake_llm_factory, stop_spy):
    conv = FakeConversation()
    _, a1 = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="探索一下站点",
        llm_call=fake_llm_factory({"reply": "第一版", "actions": []}),
    )
    await orch.regenerate_message(
        None, conversation=conv, message_id=a1.id, user_id=7,
        llm_call=fake_llm_factory({"reply": "第二版", "actions": []}),
    )
    assert stop_spy, "重新生成前应停止进行中的探索任务"
    assert stop_spy[0][0] == conv.id


@pytest.mark.asyncio
async def test_edit_message_stops_running_browse_tasks(chat_store, fake_llm_factory, stop_spy):
    conv = FakeConversation()
    user_msg, _ = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="hi",
        llm_call=fake_llm_factory({"reply": "R", "actions": []}),
    )
    await orch.edit_message(
        None, conversation=conv, message_id=user_msg.id, content="hi2", user_id=7,
        llm_call=fake_llm_factory({"reply": "R2", "actions": []}),
    )
    assert stop_spy, "编辑重生成前应停止进行中的探索任务"
    assert stop_spy[0][0] == conv.id


@pytest.mark.asyncio
async def test_regenerate_uses_tool_loop_instead_of_decision_json(
    chat_store, fake_llm_factory, monkeypatch
):
    """回归：重新生成必须走原生工具循环；模型输出工具调用时不应报「未能解析 JSON 决策」。"""
    from app.gen import model_client as mc
    from core.tools import loop as tool_loop

    from tests.unit.chat.test_chat_tool_loop import _tool_registry_with

    monkeypatch.setattr(tool_loop, "REGISTRY", _tool_registry_with())

    calls: list = []

    async def fake_llm(messages, *, tools, stream_callback=None, reasoning_callback=None, **kw):
        calls.append(tools)
        if len(calls) == 1:
            return mc.ModelReply(content="", tool_calls=[
                mc.ModelToolCall(id="c1", name="list_projects", arguments="{}"),
            ])
        return mc.ModelReply(content="共有 0 个项目")

    monkeypatch.setattr(mc, "call_model", fake_llm)

    conv = FakeConversation()
    _, a1 = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="有哪些项目？",
        llm_call=fake_llm_factory({"reply": "旧回复", "actions": []}),
    )
    updated, err = await orch.regenerate_message(
        None, conversation=conv, message_id=a1.id, user_id=7,
    )
    assert err == "", err
    assert updated.content == "共有 0 个项目"
    assert updated.actions and updated.actions[0]["tool"] == "list_projects"
