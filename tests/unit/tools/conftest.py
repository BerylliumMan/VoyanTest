"""035 工具层测试夹具。

- 隔离注册表实例（不污染全局 REGISTRY）
- ToolSpec 工厂（fake tool handler 工厂：记录调用 + 可编排返回/异常/时延）
- 截断断言助手：assert_truncated
"""

import asyncio

import pytest

from core.tools.registry import ToolRegistry, ToolSpec
from core.tools.result import ToolResult, result_bytes


@pytest.fixture
def registry():
    """隔离的注册表实例。"""
    return ToolRegistry()


@pytest.fixture
def make_tool():
    """ToolSpec 工厂。"""

    def _make(name="demo_tool", *, side_effect="read", requires_confirmation=False,
              description="演示工具", parameters=None, handler=None, result_shaper=None):
        async def _default_handler(ctx, **args):
            return ToolResult(ok=True, summary="ok")

        return ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            side_effect=side_effect,
            requires_confirmation=requires_confirmation,
            handler=handler or _default_handler,
            result_shaper=result_shaper,
        )

    return _make


@pytest.fixture
def make_handler():
    """fake tool handler 工厂：记录调用参数 + 可编排返回/异常/时延。"""

    def _make(returns=None, exc=None, delay=0.0):
        calls: list[dict] = []

        async def handler(ctx, **args):
            calls.append(args)
            if delay:
                await asyncio.sleep(delay)
            if exc is not None:
                raise exc
            if returns is not None:
                return returns
            return ToolResult(ok=True, summary="ok", data={"echo": args})

        handler.calls = calls
        return handler

    return _make


def assert_truncated(result: ToolResult, *, max_bytes: int = 16384):
    """截断断言助手：truncated=True 且 data 仍在预算内。"""
    assert result.truncated is True
    if result.data is not None:
        assert result_bytes(result.data) <= max_bytes
