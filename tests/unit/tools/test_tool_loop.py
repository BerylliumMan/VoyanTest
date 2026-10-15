"""T010：工具循环单测（红→绿）。"""

import asyncio
import json

import pytest

from app.gen.model_client import ModelReply, ModelToolCall
from core.tools import loop as tool_loop
from core.tools.registry import ToolContext
from core.tools.result import ToolResult


class FakeConversation:
    id = 1


def _reply(content="", calls=None):
    return ModelReply(content=content, tool_calls=[
        ModelToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", "{}"))
        for c in (calls or [])
    ])


class ScriptedLLM:
    """按脚本依次返回 ModelReply；记录每次调用。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, messages, *, tools, stream_callback=None, reasoning_callback=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.replies:
            return _reply("（脚本耗尽）")
        return self.replies.pop(0)


async def _run(registry, llm, *, messages=None, max_rounds=8, timeout_s=30):
    return await tool_loop.run_tool_loop(
        db=None,
        conversation=FakeConversation(),
        ctx=ToolContext(),
        messages=messages or [{"role": "user", "content": "hi"}],
        llm=llm,
        registry=registry,
        max_rounds=max_rounds,
        timeout_s=timeout_s,
    )


@pytest.mark.asyncio
async def test_multi_round_and_final_reply(registry, make_tool, make_handler):
    handler = make_handler(returns=ToolResult(ok=True, summary="5 个项目", data={"items": []}))
    registry.register(make_tool(name="list_projects", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "list_projects", "arguments": "{}"}]),
        _reply(content="共有 5 个项目"),
    ])
    outcome = await _run(registry, llm)
    assert outcome.reply == "共有 5 个项目"
    assert outcome.rounds == 2
    assert outcome.used_tools is True
    assert handler.calls == [{}]
    # 第二轮消息序列：assistant(tool_calls) + tool(result)
    second = llm.calls[1]["messages"]
    assert second[1]["role"] == "assistant"
    assert second[1]["tool_calls"][0]["function"]["name"] == "list_projects"
    assert second[2]["role"] == "tool"
    assert second[2]["tool_call_id"] == "c1"
    assert "5 个项目" in second[2]["content"]


@pytest.mark.asyncio
async def test_no_tool_calls_returns_text(registry, make_tool):
    registry.register(make_tool(name="r1"))
    llm = ScriptedLLM([_reply(content="纯文本回答")])
    outcome = await _run(registry, llm)
    assert outcome.reply == "纯文本回答"
    assert outcome.used_tools is False
    assert outcome.rounds == 1


@pytest.mark.asyncio
async def test_str_reply_from_llm_treated_as_final(registry, make_tool):
    """兼容桥：注入的 llm 返回 str（未走 tools 协议）→ 视为最终回复（混合输出交编排层）。"""
    registry.register(make_tool(name="r1"))

    async def llm(messages, *, tools, stream_callback=None, reasoning_callback=None):
        return '{"thinking": "t", "reply": "你好！", "actions": []}'

    outcome = await _run(registry, llm)
    assert outcome.reply == '{"thinking": "t", "reply": "你好！", "actions": []}'
    assert outcome.used_tools is False


@pytest.mark.asyncio
async def test_read_tools_run_in_parallel(registry, make_tool):
    active = {"n": 0, "max": 0}

    async def h(ctx, **a):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.05)
        active["n"] -= 1
        return ToolResult(ok=True, summary="ok")

    registry.register(make_tool(name="r1", handler=h))
    registry.register(make_tool(name="r2", handler=h))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}, {"id": "c2", "name": "r2"}]),
        _reply(content="done"),
    ])
    outcome = await _run(registry, llm)
    assert active["max"] >= 2  # 两个只读工具并发执行
    assert outcome.used_tools


@pytest.mark.asyncio
async def test_mutate_serial_and_not_interleaved(registry, make_tool):
    order: list[str] = []

    async def read_h(ctx, **a):
        order.append("r-start")
        await asyncio.sleep(0.03)
        order.append("r-end")
        return ToolResult(ok=True, summary="r")

    async def mut_h(ctx, **a):
        order.append("m-start")
        await asyncio.sleep(0.01)
        order.append("m-end")
        return ToolResult(ok=True, summary="m")

    registry.register(make_tool(name="r1", handler=read_h))
    registry.register(make_tool(name="m1", side_effect="mutate", handler=mut_h))
    registry.register(make_tool(name="m2", side_effect="mutate", handler=mut_h))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}, {"id": "c2", "name": "m1"}, {"id": "c3", "name": "m2"}]),
        _reply(content="done"),
    ])
    await _run(registry, llm)
    assert order == ["r-start", "r-end", "m-start", "m-end", "m-start", "m-end"]


@pytest.mark.asyncio
async def test_tool_failure_does_not_break_loop(registry, make_tool, make_handler):
    boom = make_handler(exc=RuntimeError("内部错误"))
    registry.register(make_tool(name="bad", handler=boom))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "bad"}, {"id": "c2", "name": "nope"}]),
        _reply(content="已处理失败"),
    ])
    outcome = await _run(registry, llm)
    assert outcome.reply == "已处理失败"
    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert "工具执行失败" in tool_msgs[0]["content"]
    assert "未知工具" in tool_msgs[1]["content"]
    statuses = [a["status"] for a in outcome.actions_log]
    assert statuses == ["failed", "failed"]


@pytest.mark.asyncio
async def test_round_budget_exhausted(registry, make_tool):
    registry.register(make_tool(name="r1"))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}]),
        _reply(calls=[{"id": "c2", "name": "r1"}]),
    ])
    outcome = await _run(registry, llm, max_rounds=2)
    assert outcome.rounds == 2
    assert "轮次上限" in outcome.reply
    assert outcome.used_tools


@pytest.mark.asyncio
async def test_cards_and_task_id_extracted_not_fed_to_model(registry, make_tool, make_handler):
    card = {"type": "draft_set", "items": [{"draft_id": 1}], "status": "draft"}
    handler = make_handler(returns=ToolResult(
        ok=True, summary="已生成 1 条草稿",
        data={"card": card, "task_id": "sess-1", "card_type": "draft_set"},
    ))
    registry.register(make_tool(name="generate_cases", side_effect="mutate", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "generate_cases"}]),
        _reply(content="done"),
    ])
    outcome = await _run(registry, llm)
    assert outcome.cards == [card]
    assert outcome.task_id == "sess-1"
    tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
    assert '"card"' not in tool_msg["content"]  # 大对象不回喂模型
    assert "card_type" in tool_msg["content"]   # 轻量元信息保留


@pytest.mark.asyncio
async def test_truncation_guidance_appended(registry, make_tool, make_handler):
    handler = make_handler(returns=ToolResult(
        ok=True, summary="大结果", data={"items": [{"x": "y" * 100}]}, truncated=True,
    ))
    registry.register(make_tool(name="big", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "big"}]),
        _reply(content="done"),
    ])
    await _run(registry, llm)
    tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "结果已截断" in tool_msg["content"]


@pytest.mark.asyncio
async def test_events_emitted(registry, make_tool, make_handler, monkeypatch):
    captured: list[dict] = []

    async def fake_emit(conv_id, event):
        captured.append(event)

    monkeypatch.setattr(tool_loop, "emit", fake_emit)
    handler = make_handler(returns=ToolResult(ok=True, summary="ok"))
    registry.register(make_tool(name="list_projects", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "list_projects", "arguments": '{"password": "pw", "project_id": 24}'}]),
        _reply(content="done"),
    ])
    await _run(registry, llm)
    types_ = [e["type"] for e in captured]
    assert types_ == ["tool.call.start", "tool.call.end"]
    start, end = captured
    assert start["tool"] == "list_projects"
    assert start["call_id"] == "c1"
    assert start["round"] == 1
    assert "******" in start["args_digest"] and "pw" not in start["args_digest"]
    assert end["ok"] is True
    assert end["duration_ms"] >= 0
    assert end["truncated"] is False
