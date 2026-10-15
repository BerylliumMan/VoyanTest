"""T026 填充：调用治理单测。

覆盖：
- 并行上限（read 工具 gather 数量受 chat_tool_parallel_max 约束）
- 串行隔离（mutate/execute 串行且不与只读交叉）
- 预算终止（轮次/时长上限 + 终止说明）
- 防循环（同工具同参数重复检测 ≥3 提示 / ≥5 终止）
- 截断标注（超 chat_tool_result_max_bytes 的显式标注）
- 审计行（tool.<name> + 参数打码）
- 权限拒绝（越权错误不泄漏、不中断整轮）
"""

import asyncio
import json
import types

import pytest

from app.gen.model_client import ModelReply, ModelToolCall
from core.tools import loop as tool_loop
from core.tools.registry import ToolContext
from core.tools.result import ToolResult, error_result, result_bytes


class FakeConversation:
    id = 1


def _reply(content="", calls=None):
    return ModelReply(content=content, tool_calls=[
        ModelToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", "{}"))
        for c in (calls or [])
    ])


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, messages, *, tools, stream_callback=None, reasoning_callback=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.replies:
            return _reply("（脚本耗尽）")
        return self.replies.pop(0)


def _fake_settings(**over):
    base = types.SimpleNamespace(
        chat_tool_parallel_max=4,
        chat_tool_max_rounds=8,
        chat_tool_timeout_seconds=30,
        chat_tool_result_max_bytes=16384,
    )
    for k, v in over.items():
        setattr(base, k, v)
    return base


async def _run(registry, llm, *, ctx=None, messages=None, max_rounds=8, timeout_s=30):
    return await tool_loop.run_tool_loop(
        db=None,
        conversation=FakeConversation(),
        ctx=ctx or ToolContext(),
        messages=messages or [{"role": "user", "content": "hi"}],
        llm=llm,
        registry=registry,
        max_rounds=max_rounds,
        timeout_s=timeout_s,
    )


# ----------------------------
# 并行上限 / 串行隔离
# ----------------------------

@pytest.mark.asyncio
async def test_parallel_cap_respected(registry, make_tool, monkeypatch):
    monkeypatch.setattr(tool_loop, "get_settings", lambda: _fake_settings(chat_tool_parallel_max=2))
    active = {"n": 0, "max": 0}

    async def h(ctx, **a):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.05)
        active["n"] -= 1
        return ToolResult(ok=True, summary="ok")

    for i in range(4):
        registry.register(make_tool(name=f"r{i}", handler=h))
    llm = ScriptedLLM([
        _reply(calls=[{"id": f"c{i}", "name": f"r{i}"} for i in range(4)]),
        _reply(content="done"),
    ])
    await _run(registry, llm)
    assert active["max"] == 2  # 上限生效（4 个只读并发被压到 2）


@pytest.mark.asyncio
async def test_read_does_not_interleave_with_mutate(registry, make_tool):
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
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}, {"id": "c2", "name": "m1"}]),
        _reply(content="done"),
    ])
    await _run(registry, llm)
    assert order == ["r-start", "r-end", "m-start", "m-end"]


# ----------------------------
# 防循环（同工具同参数重复）
# ----------------------------

@pytest.mark.asyncio
async def test_duplicate_hint_at_three(registry, make_tool, make_handler):
    handler = make_handler(returns=ToolResult(ok=True, summary="同款结果"))
    registry.register(make_tool(name="r1", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1", "arguments": '{"a": 1}'}]),
        _reply(calls=[{"id": "c2", "name": "r1", "arguments": '{"a": 1}'}]),
        _reply(calls=[{"id": "c3", "name": "r1", "arguments": '{"a": 1}'}]),
        _reply(content="好的，我换一种方式"),
    ])
    outcome = await _run(registry, llm)
    # 第 3 次的 tool 消息应含治理提示；循环继续并正常收尾
    third_round_msgs = llm.calls[3]["messages"]
    tool_msgs = [m for m in third_round_msgs if m["role"] == "tool"]
    assert any("重复" in m["content"] for m in tool_msgs)
    assert outcome.reply == "好的，我换一种方式"


@pytest.mark.asyncio
async def test_duplicate_terminate_at_five(registry, make_tool, make_handler):
    handler = make_handler(returns=ToolResult(ok=True, summary="同款结果"))
    registry.register(make_tool(name="r1", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": f"c{i}", "name": "r1", "arguments": '{"a": 1}'}])
        for i in range(1, 7)
    ])
    outcome = await _run(registry, llm)
    assert outcome.rounds == 5  # 第 5 次重复后终止，不再进第 6 轮
    assert any("重复" in n for n in outcome.notes)
    assert "重复" in outcome.reply


@pytest.mark.asyncio
async def test_different_args_not_treated_as_duplicate(registry, make_tool, make_handler):
    handler = make_handler(returns=ToolResult(ok=True, summary="ok"))
    registry.register(make_tool(name="r1", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": f"c{i}", "name": "r1", "arguments": json.dumps({"a": i})}])
        for i in range(5)
    ] + [_reply(content="done")])
    outcome = await _run(registry, llm)
    assert outcome.rounds == 6  # 参数不同不算重复，循环未被提前终止


# ----------------------------
# 截断强制标注
# ----------------------------

@pytest.mark.asyncio
async def test_result_over_threshold_gets_truncated_annotation(registry, make_tool, make_handler, monkeypatch):
    monkeypatch.setattr(tool_loop, "get_settings", lambda: _fake_settings(chat_tool_result_max_bytes=120))
    handler = make_handler(returns=ToolResult(
        ok=True, summary="大结果", data={"items": ["x" * 500]}, truncated=False,
    ))
    registry.register(make_tool(name="big", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "big"}]),
        _reply(content="done"),
    ])
    outcome = await _run(registry, llm)
    tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "结果已截断" in tool_msg["content"]
    assert outcome.actions_log[0]["truncated"] is True


# ----------------------------
# 审计（tool.<name> + 参数打码）
# ----------------------------

@pytest.mark.asyncio
async def test_audit_written_per_call_with_redaction(registry, make_tool, make_handler, monkeypatch):
    captured: list[tuple] = []

    async def fake_log_audit(db, user_id, action, details=None, ip_address=None):
        captured.append((db, user_id, action, details))

    import app.auth as auth_mod

    monkeypatch.setattr(auth_mod, "log_audit", fake_log_audit)

    handler = make_handler(returns=ToolResult(ok=True, summary="ok"))
    registry.register(make_tool(name="list_projects", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "list_projects",
                       "arguments": '{"password": "pw", "project_id": 24}'}]),
        _reply(content="done"),
    ])
    db_obj = object()
    ctx = ToolContext(db=db_obj, user_id=7)
    await _run(registry, llm, ctx=ctx)

    assert len(captured) == 1
    db_arg, user_id, action, details = captured[0]
    assert db_arg is db_obj
    assert user_id == 7
    assert action == "tool.list_projects"
    assert details["status"] == "ok"
    blob = json.dumps(details, ensure_ascii=False)
    assert "******" in blob and "pw" not in blob
    assert details["args"]["project_id"] == 24


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_loop(registry, make_tool, make_handler, monkeypatch):
    async def boom(db, user_id, action, details=None, ip_address=None):
        raise RuntimeError("audit down")

    import app.auth as auth_mod

    monkeypatch.setattr(auth_mod, "log_audit", boom)

    registry.register(make_tool(name="r1", handler=make_handler()))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}]),
        _reply(content="照常完成"),
    ])
    outcome = await _run(registry, llm, ctx=ToolContext(db=object(), user_id=7))
    assert outcome.reply == "照常完成"


# ----------------------------
# 权限拒绝（错误不泄漏、不中断整轮）
# ----------------------------

@pytest.mark.asyncio
async def test_permission_error_flows_without_breaking(registry, make_tool, make_handler):
    handler = make_handler(returns=error_result("无权访问项目 #9"))
    registry.register(make_tool(name="search_cases", handler=handler))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "search_cases", "arguments": '{"project_id": 9}'}]),
        _reply(content="该项目无权访问，我换一个项目试试"),
    ])
    outcome = await _run(registry, llm)
    assert outcome.actions_log[0]["status"] == "failed"
    tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "无权访问项目 #9" in tool_msg["content"]
    assert "Traceback" not in tool_msg["content"]  # 不泄漏内部细节


# ----------------------------
# 时长预算
# ----------------------------

@pytest.mark.asyncio
async def test_time_budget_termination(registry, make_tool, make_handler, monkeypatch):
    registry.register(make_tool(name="r1", handler=make_handler(delay=0.05)))
    llm = ScriptedLLM([
        _reply(calls=[{"id": f"c{i}", "name": "r1", "arguments": json.dumps({"i": i})}])
        for i in range(10)
    ])

    # 让 deadline 快速过期：第 2 轮后 time.monotonic 前进 100s
    real = tool_loop.time.monotonic
    state = {"n": 0}

    def fake_monotonic():
        state["n"] += 1
        return real() + (100 if state["n"] > 6 else 0)

    monkeypatch.setattr(tool_loop.time, "monotonic", fake_monotonic)
    outcome = await _run(registry, llm, timeout_s=10)
    assert any("超出预算" in n for n in outcome.notes)
    assert "超出预算" in outcome.reply


@pytest.mark.asyncio
async def test_parallel_reads_get_isolated_sessions(registry, make_tool, monkeypatch):
    """并行只读各自独立 session（AsyncSession 不支持并发共享）。"""
    monkeypatch.setattr(tool_loop, "get_settings", lambda: _fake_settings())
    seen_dbs = []

    class FakeSession:
        def __init__(self, tag):
            self.tag = tag

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    counter = {"n": 0}

    class FakeSessionLocal:
        def __call__(self):
            counter["n"] += 1
            return FakeSession(counter["n"])

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())

    async def h(ctx, **a):
        seen_dbs.append(ctx.db)
        await asyncio.sleep(0.02)
        return ToolResult(ok=True, summary="ok")

    registry.register(make_tool(name="r1", handler=h))
    registry.register(make_tool(name="r2", handler=h))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}, {"id": "c2", "name": "r2"}]),
        _reply(content="done"),
    ])
    request_db = object()
    await _run(registry, llm, ctx=ToolContext(db=request_db, user_id=7))
    assert len(seen_dbs) == 2
    assert seen_dbs[0] is not seen_dbs[1]
    assert all(db is not request_db for db in seen_dbs)


@pytest.mark.asyncio
async def test_single_read_keeps_request_session(registry, make_tool, monkeypatch):
    monkeypatch.setattr(tool_loop, "get_settings", lambda: _fake_settings())
    seen_dbs = []

    async def h(ctx, **a):
        seen_dbs.append(ctx.db)
        return ToolResult(ok=True, summary="ok")

    registry.register(make_tool(name="r1", handler=h))
    llm = ScriptedLLM([
        _reply(calls=[{"id": "c1", "name": "r1"}]),
        _reply(content="done"),
    ])
    request_db = object()
    await _run(registry, llm, ctx=ToolContext(db=request_db, user_id=7))
    assert seen_dbs == [request_db]
