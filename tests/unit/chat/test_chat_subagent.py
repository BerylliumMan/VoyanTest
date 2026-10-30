"""044 US5：只读隔离子代理（先红）。"""

from types import SimpleNamespace

import pytest

from core.chat.subagent import run_subagent
from core.tools.registry import ToolRegistry, ToolSpec
from core.tools.result import ToolResult


class _Call:
    def __init__(self, id_, name, arguments="{}"):
        self.id = id_
        self.name = name
        self.arguments = arguments


class _Reply:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _registry():
    reg = ToolRegistry()

    async def _read(ctx, **a):
        return ToolResult(ok=True, summary="hit", data={"items": [1]})

    async def _write(ctx, **a):
        return ToolResult(ok=True, summary="written", data={})

    reg.register(ToolSpec(
        name="get_case", description="读", parameters={"type": "object", "properties": {}},
        side_effect="read", requires_confirmation=False, handler=_read,
    ))
    reg.register(ToolSpec(
        name="run_case", description="写", parameters={"type": "object", "properties": {}},
        side_effect="execute", requires_confirmation=False, handler=_write,
    ))
    return reg


def _ctx():
    return SimpleNamespace(
        db=None,
        conversation=SimpleNamespace(id=1, refs=None),
        user_id=1,
        llm=None,
        source="chat",
    )


@pytest.mark.asyncio
async def test_subagent_uses_allowlist_and_returns_text():
    reg = _registry()
    calls = {"n": 0}

    async def fake_llm(messages, *, tools, stream_callback=None, reasoning_callback=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Reply("", [_Call("c1", "get_case")])
        return _Reply("查到用例 1")

    result = await run_subagent(
        ctx=_ctx(), prompt="查一下用例 1", agent_type="explore",
        tool_allowlist=["get_case"], llm=fake_llm, registry=reg,
    )
    assert result.status == "completed"
    assert "get_case" in result.content or "用例 1" in result.content
    assert result.tool_uses >= 1


@pytest.mark.asyncio
async def test_subagent_respects_max_rounds():
    reg = _registry()

    async def looping_llm(messages, *, tools, stream_callback=None, reasoning_callback=None, **kw):
        return _Reply("", [_Call("c1", "get_case")])

    result = await run_subagent(
        ctx=_ctx(), prompt="x", max_rounds=2, llm=looping_llm, registry=reg,
    )
    assert result.status in {"completed", "max_rounds"}


@pytest.mark.asyncio
async def test_subagent_rejects_write_tools():
    result = await run_subagent(
        ctx=_ctx(), prompt="x", tool_allowlist=["run_case"], registry=_registry(),
    )
    assert result.status == "rejected"
    assert "只读" in result.content
