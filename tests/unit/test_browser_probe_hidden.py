"""US6/US7 单测：browser_find 能力探测 + 隐藏元素诊断（先 RED）。

US6：
- TOOL_MAP/build_mcp_args find 映射（text / regex 二选一）
- manager caps 缓存与 getter；bridge find 辅助（仅断言/等待失败 + 能力可用）
US7：
- collect_elements(include_hidden) 采集模式
- diagnose_missing 三态：hidden / not_in_dom / visible_only / unknown
- action_unit probe_note 含诊断
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


# ── US6：映射与参数 ────────────────────────────────────────────────────────


def test_find_mapping_and_args():
    from core.mcp_args import TOOL_MAP, build_mcp_args

    assert TOOL_MAP.get("find") == "browser_find"
    assert build_mcp_args("find", selector="", value="Products") == {"text": "Products"}
    assert build_mcp_args("find", selector="", value="/prod.*/") == {"regex": "/prod.*/"}


def test_manager_caps_cache():
    from agent.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._agent_caps = {}
    assert mgr.get_agent_caps("a1") == set()
    mgr._agent_caps["a1"] = {"browser_find", "browser_click"}
    assert "browser_find" in mgr.get_agent_caps("a1")
    # 返回副本，外部修改不影响缓存
    s = mgr.get_agent_caps("a1")
    s.add("x")
    assert "x" not in mgr._agent_caps["a1"]


@pytest.mark.asyncio
async def test_bridge_find_assist(monkeypatch):
    from app.config import Settings
    from core import agent_bridge as bridge_mod

    settings = Settings(exec_find_probe=True)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    bridge = bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )
    bridge.agent_manager.get_agent_caps = lambda agent_id: {"browser_find"}

    sent = []

    async def fake_send_act(agent_id, run_id, tool_call):
        sent.append(tool_call)
        if tool_call.get("name") == "find":
            return {"success": True, "text": "found: Products heading at line 12"}
        return {"success": True}

    bridge.agent_manager.send_act = fake_send_act

    result = {"success": False, "error": "Timeout 5000ms exceeded waiting for text"}
    await bridge._enrich_failure_evidence(
        "a1", "r1", {"name": "assert_text", "args": {"value": "Products"}}, result,
    )
    assert "find:" in result["error"]
    assert any(c.get("name") == "find" for c in sent)


@pytest.mark.asyncio
async def test_bridge_find_assist_skipped_without_caps(monkeypatch):
    from app.config import Settings
    from core import agent_bridge as bridge_mod

    settings = Settings(exec_find_probe=True)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    bridge = bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )
    bridge.agent_manager.get_agent_caps = lambda agent_id: set()
    sent = []

    async def fake_send_act(agent_id, run_id, tool_call):
        sent.append(tool_call)
        return {"success": True}

    bridge.agent_manager.send_act = fake_send_act

    result = {"success": False, "error": "Timeout"}
    await bridge._enrich_failure_evidence(
        "a1", "r1", {"name": "assert_text", "args": {"value": "Products"}}, result,
    )
    assert "find:" not in result.get("error", "")
    assert not sent


# ── US7：采集与诊断 ────────────────────────────────────────────────────────


class _FakeCaller:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        fn = str(args.get("function") or "")
        if "elementFromPoint" in fn and self.payload.get("fail_probe"):
            return {"success": False, "text": ""}
        return {"success": True, "text": json.dumps(self.payload)}


def _elem(name, visible=True, selector="#x"):
    return {"name": name, "text": name, "selector": selector, "visible": visible}


@pytest.mark.asyncio
async def test_collect_elements_include_hidden_uses_unfiltered_fn():
    from core.browser_probe import COLLECT_FN, collect_elements

    caller = _FakeCaller({"url": "u", "title": "t", "count": 2, "elements": [_elem("A", True), _elem("B", False)]})
    await collect_elements(caller)
    assert ".filter((e) => e.visible)" in str(caller.calls[-1][1]["function"])
    await collect_elements(caller, include_hidden=True)
    assert ".filter((e) => true)" in str(caller.calls[-1][1]["function"])
    assert ".filter((e) => e.visible)" not in str(caller.calls[-1][1]["function"])
    assert ".filter((e) => e.visible)" in COLLECT_FN  # 常量未被破坏


@pytest.mark.asyncio
async def test_diagnose_missing_states():
    from core.browser_probe import diagnose_missing

    hidden = _FakeCaller({
        "url": "u", "title": "t", "count": 2,
        "elements": [_elem("Login", True), _elem("Submit", False, "#hidden-btn")],
    })
    out = await diagnose_missing(hidden, "Submit")
    assert out["state"] == "hidden"
    assert "不可见" in out["note"]

    absent = _FakeCaller({"url": "u", "title": "t", "count": 1, "elements": [_elem("Login", True)]})
    out2 = await diagnose_missing(absent, "下载报告")
    assert out2["state"] == "not_in_dom"

    visible = _FakeCaller({"url": "u", "title": "t", "count": 1, "elements": [_elem("Login", True)]})
    out3 = await diagnose_missing(visible, "Login")
    assert out3["state"] == "visible_only"

    failing = _FakeCaller({"fail_probe": True})
    out4 = await diagnose_missing(failing, "Login")
    assert out4["state"] == "unknown"
    assert out4["note"] == ""


@pytest.mark.asyncio
async def test_action_unit_probe_note_includes_diagnosis():
    from core.action_unit import normalize_unit, run_unit

    async def _exec(action):
        return {"success": False, "error": "element not found"}

    async def _probe(target):
        return [], {"elements": 3, "matched": 0, "diagnosis": "目标存在于 DOM 但不可见（hidden）：#x"}

    async def _nosleep(_d):
        return None

    steps, _ = normalize_unit(
        [{"action": "click", "selector": "#x", "element_desc": "登录按钮"}],
        max_steps=6, is_valid_ref=lambda r: True,
    )
    outcome = await run_unit(steps, _exec, retry_delays=(0.0,), sleep=_nosleep, probe=_probe)
    assert outcome.success is False
    assert "hidden" in str(outcome.error)


def test_extract_json_real_double_encoded_format():
    """回归：playwright-mcp 0.0.80 实测格式（Result 块内双编码字符串 + 代码回显）。"""
    from core.browser_probe import _extract_json

    raw = (
        '### Result\n"{\\"url\\":\\"https://www.saucedemo.com/\\",\\"title\\":\\"Swag Labs\\",'
        '\\"count\\":4,\\"elements\\":[{\\"tag\\":\\"div\\",\\"visible\\":true}]}"\n'
        "### Ran Playwright code\n```js\nawait page.evaluate('() => { /* ... */ }');\n```\n"
        "### Page\n- Page URL: https://www.saucedemo.com/\n"
    )
    data = _extract_json(raw)
    assert data.get("count") == 4
    assert data.get("elements") and data["elements"][0].get("tag") == "div"
