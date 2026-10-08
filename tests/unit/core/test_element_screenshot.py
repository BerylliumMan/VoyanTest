"""US3 单测：元素级失败截图（先 RED）。

覆盖：
- screenshot 参数构造（element/target 与 fullPage 互斥）+ highlight 映射
- bridge._capture_failure_screenshot：target → observe 携带 screenshot_target/highlight；开关关闭 → 不携带
- bridge act 派发注入 _element_screenshot（unit 路径）
- client 回退逻辑：元素截图失败 → 整页（light 对象直测）
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


# ── T021：参数构造 ─────────────────────────────────────────────────────────


def test_screenshot_element_mode_args():
    from core.mcp_args import TOOL_MAP, build_mcp_args

    assert TOOL_MAP.get("highlight") == "browser_highlight"
    assert TOOL_MAP.get("hide_highlight") == "browser_hide_highlight"

    args = build_mcp_args(
        "screenshot", selector="e12", element_desc="登录按钮", value="shot.png",
    )
    assert args["element"] == "登录按钮"
    assert args["target"] == "e12"
    assert args["filename"] == "shot.png"
    assert "fullPage" not in args

    full = build_mcp_args("screenshot", selector="", value="shot.png")
    assert full.get("fullPage") is True
    assert "target" not in full


def test_highlight_args():
    from core.mcp_args import build_mcp_args

    assert build_mcp_args("highlight", selector="e12", element_desc="登录") == {
        "element": "登录", "target": "e12",
    }
    assert build_mcp_args("hide_highlight", selector="", value="") == {}


# ── T021：bridge 失败截图带 target ─────────────────────────────────────────


def _make_bridge(monkeypatch, *, element_ss=True):
    from app.config import Settings
    from core import agent_bridge as bridge_mod

    settings = Settings(exec_element_screenshot=element_ss)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    return bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )


@pytest.mark.asyncio
async def test_capture_failure_screenshot_element_mode(monkeypatch):
    bridge = _make_bridge(monkeypatch, element_ss=True)
    calls = {}

    async def fake_send_observe(agent_id, run_id, **kwargs):
        calls.update(kwargs)
        return {"screenshot_b64": "x"}

    bridge.agent_manager.send_observe = fake_send_observe
    monkeypatch.setattr(bridge, "_save_screenshot", lambda b64, name: f"p/{name}.png")

    out = await bridge._capture_failure_screenshot("a1", "r1", target="e12")
    assert out == "p/failure_final.png"
    assert calls.get("want_screenshot") is True
    assert calls.get("screenshot_target") == "e12"
    assert calls.get("screenshot_highlight") is True


@pytest.mark.asyncio
async def test_capture_failure_screenshot_switch_off(monkeypatch):
    bridge = _make_bridge(monkeypatch, element_ss=False)
    calls = {}

    async def fake_send_observe(agent_id, run_id, **kwargs):
        calls.update(kwargs)
        return {"screenshot_b64": "x"}

    bridge.agent_manager.send_observe = fake_send_observe
    monkeypatch.setattr(bridge, "_save_screenshot", lambda b64, name: f"p/{name}.png")

    await bridge._capture_failure_screenshot("a1", "r1", target="e12")
    assert calls.get("screenshot_target") is None
    assert calls.get("screenshot_highlight") is False


@pytest.mark.asyncio
async def test_unit_dispatch_injects_element_screenshot_flag(monkeypatch):
    from core import agent_bridge as bridge_mod

    bridge = _make_bridge(monkeypatch, element_ss=True)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_message", _noop)
    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_tool_call", _noop)

    sent = []

    async def fake_send_act(agent_id, run_id, tool_call):
        sent.append(tool_call)
        return {"success": True}

    bridge.agent_manager.send_act = fake_send_act

    out = await bridge._execute_action_unit(
        SimpleNamespace(id=7), 3, "agent-1", "run-1",
        [{"action": "click", "selector": "#login"}],
        "", [],
    )
    assert out.get("success") is True
    assert sent and sent[0].get("_element_screenshot") is True


# ── T021：client 回退（元素失败 → 整页）───────────────────────────────────


@pytest.mark.asyncio
async def test_client_screenshot_falls_back_to_fullpage():
    from agent.client_core import AgentClient

    client = object.__new__(AgentClient)
    calls = []

    async def fake_call(action, selector, value, **kwargs):
        calls.append((action, selector))
        if action == "screenshot" and selector == "e12":
            return {"success": False, "error": "no such element", "_content": []}
        if action == "screenshot" and selector == "":
            return {"success": True, "_content": [{"type": "image", "data": "A" * 120}]}
        return {"success": True, "_content": []}

    client._mcp_call_tool = fake_call

    b64 = await client._mcp_screenshot_base64(element_target="e12", highlight=True)
    assert b64 == "A" * 120
    assert ("highlight", "e12") in calls
    assert ("screenshot", "e12") in calls
    assert ("screenshot", "") in calls  # 回退整页


@pytest.mark.asyncio
async def test_client_screenshot_element_success_no_fallback():
    from agent.client_core import AgentClient

    client = object.__new__(AgentClient)
    calls = []

    async def fake_call(action, selector, value, **kwargs):
        calls.append((action, selector))
        if action == "screenshot":
            return {"success": True, "_content": [{"type": "image", "data": "B" * 120}]}
        return {"success": True, "_content": []}

    client._mcp_call_tool = fake_call

    b64 = await client._mcp_screenshot_base64(element_target="e12", highlight=False)
    assert b64 == "B" * 120
    assert ("screenshot", "e12") in calls
    assert ("screenshot", "") not in calls  # 未回退
