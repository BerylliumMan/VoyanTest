"""US5 单测：坐标兜底的元素回解（先 RED）。

覆盖：
- build_element_probe_js / parse_element_probe_result（含 ### Result 包装与垃圾输入）
- probe_via_evaluate_act（客户端 act 通道适配；失败静默）
- replay_resolve：click_xy + dom_attrs.active → 语义策略（click_role/click_text）
- bridge._resolve_coordinate_target：click_xy 成功后 _resolved 入 action.args
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_build_element_probe_js():
    from core.coordinate_probe import build_element_probe_js

    js = build_element_probe_js(120, 240)
    assert "elementFromPoint" in js
    assert "120" in js and "240" in js


def test_parse_element_probe_result_variants():
    from core.coordinate_probe import parse_element_probe_result

    payload = {"name": "下载", "role": "button", "selector": "#dl", "tag": "button"}
    wrapped = "### Result\n" + json.dumps(payload, ensure_ascii=False)
    assert parse_element_probe_result(wrapped) == payload
    assert parse_element_probe_result(json.dumps(payload)) == payload
    # playwright-mcp 实际格式：Result 区块内是"双编码字符串" + 代码回显
    double = (
        '### Result\n"{\\"name\\":\\"Login\\",\\"role\\":\\"button\\",\\"selector\\":\\"#login-button\\"}"\n'
        "### Ran Playwright code\n```js\nawait page.evaluate('() => { const el = document.querySelector(\\'#login-button\\'); }');\n```"
    )
    out = parse_element_probe_result(double)
    assert out is not None and out.get("name") == "Login" and out.get("selector") == "#login-button"
    assert parse_element_probe_result("not json at all") is None
    assert parse_element_probe_result("") is None
    assert parse_element_probe_result(None) is None
    assert parse_element_probe_result("{}") is None  # 空元素视为未命中


@pytest.mark.asyncio
async def test_probe_via_evaluate_act():
    from core.coordinate_probe import probe_via_evaluate_act

    calls = []

    async def fake_send_act(action):
        calls.append(action)
        return {
            "success": True,
            "text": '### Result\n{"name":"Login","role":"button","selector":"#login-button"}',
        }

    out = await probe_via_evaluate_act(fake_send_act, 10, 20)
    assert out["name"] == "Login"
    assert calls and calls[0]["name"] == "evaluate"
    assert "elementFromPoint" in calls[0]["args"]["value"]

    async def fail_send_act(action):
        return {"success": False, "error": "boom"}

    assert await probe_via_evaluate_act(fail_send_act, 10, 20) is None

    async def raising(action):
        raise RuntimeError("ws down")

    assert await probe_via_evaluate_act(raising, 10, 20) is None


def test_replay_click_xy_uses_resolved_semantics():
    from core.replay_resolve import build_replay_from_step

    step = {"description": "点击【下载】", "structured_step": {"action": "click", "target_name": "下载"}}
    replay = build_replay_from_step(
        step, action="click_xy", selector=None,
        dom_attrs={"active": {"name": "下载", "role": "button"}},
    )
    assert replay.get("strategy") in ("click_role", "click_text")
    assert replay.get("name") == "下载" or replay.get("exact_text") == "下载"

    replay2 = build_replay_from_step(
        step, action="click_xy", selector=None,
        dom_attrs={"active": {"name": "下载报告"}},
    )
    assert replay2.get("strategy") == "click_text"
    assert replay2.get("exact_text") == "下载报告"

    # 无 resolved 时保持既有回退（不崩溃）
    replay3 = build_replay_from_step(step, action="click_xy", selector=None)
    assert isinstance(replay3, dict)


@pytest.mark.asyncio
async def test_bridge_resolve_coordinate_target(monkeypatch):
    from core import agent_bridge as bridge_mod

    bridge = bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )
    sent = []

    async def fake_send_act(agent_id, run_id, tool_call):
        sent.append(tool_call)
        if tool_call.get("name") == "evaluate":
            return {
                "success": True,
                "text": '### Result\n{"name":"登录","role":"button","selector":"#login"}',
            }
        return {"success": True}

    bridge.agent_manager.send_act = fake_send_act

    action = {"name": "click_xy", "args": {"value": "100,200"}}
    result = {"success": True}
    await bridge._resolve_coordinate_target("a1", "r1", action, result)

    assert action["args"].get("_resolved", {}).get("name") == "登录"
    assert result.get("_resolved", {}).get("name") == "登录"
    assert sent and sent[0]["name"] == "evaluate"

    # 非坐标动作：不回解
    action2 = {"name": "click", "args": {"selector": "e1"}}
    await bridge._resolve_coordinate_target("a1", "r1", action2, {"success": True})
    assert "_resolved" not in action2["args"]
