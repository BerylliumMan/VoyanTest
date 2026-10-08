"""AgentBridge 宏动作单元契约 — 模型输出的 unit 必须透传，DOM 探针必须可用。

本轮修复回归（线上 run #334）：
  1. PlaywrightMCPToolCall 支持 unit（省略 action）后，_llm_decide 必须把 unit
     原样返回给主循环的 `action.get("unit")` 分叉；否则 unit 会被包成普通动作丢弃。
  2. _execute_action_unit 内引用未定义的 `cfg`（NameError 被 except 吞掉）曾导致
     DOM 探针在客户端链路上被静默禁用。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core import agent_bridge as bridge_mod
from core.llm_wrapper import PlaywrightMCPToolCall


def _make_bridge():
    return bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )


def _patch_settings(monkeypatch, *, probe=True):
    from app.config import Settings

    settings = Settings(action_unit_probe=probe)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    async def _noop_message(*args, **kwargs):
        return None

    # 成功路径的审计消息写库与探针无关；用 no-op 替换避免 AsyncMock 噪声。
    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_message", _noop_message)


async def _decide(bridge, **overrides):
    kwargs = dict(
        llm_client=None,
        goal="g",
        snapshot="",
        page_url="",
        context_messages=[],
        turn=1,
    )
    kwargs.update(overrides)
    return await bridge._llm_decide(**kwargs)


# ── 1. _llm_decide：unit 透传 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_decide_passes_batch_unit_through(monkeypatch):
    async def fake_resolve(*args, **kwargs):
        return ""

    async def fake_generate(**kwargs):
        return PlaywrightMCPToolCall.model_validate({
            "thinking": "登录机械三段",
            "unit": [
                {"action": "fill", "selector": "e11", "value": "standard_user"},
                {"action": "fill", "selector": "e13", "value": "secret_sauce"},
                {"action": "click", "selector": "e15"},
            ],
        })

    monkeypatch.setattr(bridge_mod, "resolve_prompt_for_agent", fake_resolve)
    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)

    action = await _decide(_make_bridge())
    assert isinstance(action.get("unit"), list)
    assert [u["action"] for u in action["unit"]] == ["fill", "fill", "click"]


@pytest.mark.asyncio
async def test_llm_decide_single_action_has_no_unit_key(monkeypatch):
    async def fake_resolve(*args, **kwargs):
        return ""

    async def fake_generate(**kwargs):
        return PlaywrightMCPToolCall(action="click", selector="text=Login")

    monkeypatch.setattr(bridge_mod, "resolve_prompt_for_agent", fake_resolve)
    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)

    action = await _decide(_make_bridge())
    assert "unit" not in action
    assert action.get("name") == "click"


# ── 2. _execute_action_unit：DOM 探针启用（cfg NameError 回归）────────────


def _capture_run_unit(monkeypatch):
    captured = {}

    async def fake_run_unit(steps, execute, *, on_step_ok=None, probe=None, **kw):
        from core.action_unit import UnitOutcome

        captured["probe"] = probe
        return UnitOutcome(success=True)

    monkeypatch.setattr("core.action_unit.run_unit", fake_run_unit)
    return captured


@pytest.mark.asyncio
async def test_action_unit_enables_dom_probe(monkeypatch):
    _patch_settings(monkeypatch, probe=True)
    captured = _capture_run_unit(monkeypatch)
    bridge = _make_bridge()

    out = await bridge._execute_action_unit(
        SimpleNamespace(id=1), 1, "agent-1", "run-1",
        [{"action": "click", "selector": "#login"}],
        "", [],
    )
    assert out.get("success") is True
    assert captured["probe"] is not None


@pytest.mark.asyncio
async def test_action_unit_probe_can_be_disabled(monkeypatch):
    _patch_settings(monkeypatch, probe=False)
    captured = _capture_run_unit(monkeypatch)
    bridge = _make_bridge()

    await bridge._execute_action_unit(
        SimpleNamespace(id=1), 1, "agent-1", "run-1",
        [{"action": "click", "selector": "#login"}],
        "", [],
    )
    assert captured["probe"] is None


@pytest.mark.asyncio
async def test_action_unit_probe_wires_to_browser_probe(monkeypatch):
    _patch_settings(monkeypatch, probe=True)
    captured = _capture_run_unit(monkeypatch)
    bridge = _make_bridge()

    async def fake_send_act(agent_id, run_id, tool_call):
        assert tool_call.get("action") == "evaluate"
        return {
            "success": True,
            "text": (
                '{"url":"https://x","title":"t","count":1,"elements":['
                '{"name":"Login","selector":"#login","attributes":{"id":"login"}}]}'
            ),
        }

    bridge.agent_manager.send_act = fake_send_act

    await bridge._execute_action_unit(
        SimpleNamespace(id=1), 1, "agent-1", "run-1",
        [{"action": "click", "selector": "#login"}],
        "", [],
    )
    locators, evidence = await captured["probe"]("Login")
    assert locators == ["#login", "text=Login"]
    assert evidence["matched"] == 1


# ── 3. _execute_action_unit：逐子动作落 agent_tool_calls（步骤级证据）────────


def _capture_tool_calls(monkeypatch):
    calls = []

    async def fake_create_tool_call(db, run_id, turn, name, args, success, error=""):
        calls.append(
            {"run": run_id, "turn": turn, "name": name, "args": args,
             "success": success, "error": error}
        )

    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_tool_call", fake_create_tool_call)
    return calls


@pytest.mark.asyncio
async def test_action_unit_records_tool_call_per_subaction(monkeypatch):
    _patch_settings(monkeypatch, probe=False)
    calls = _capture_tool_calls(monkeypatch)
    bridge = _make_bridge()

    async def fake_send_act(agent_id, run_id, tool_call):
        return {"success": True}

    bridge.agent_manager.send_act = fake_send_act

    out = await bridge._execute_action_unit(
        SimpleNamespace(id=7), 3, "agent-1", "run-1",
        [
            {"action": "fill", "selector": "#user", "value": "standard_user"},
            {"action": "click", "selector": "#login-btn"},
        ],
        "", [],
    )
    assert out.get("success") is True
    assert out.get("last_action") == "click"
    assert [(c["name"], c["success"]) for c in calls] == [("fill", True), ("click", True)]
    assert calls[0]["args"].get("value") == "standard_user"
    assert calls[1]["args"].get("selector") == "#login-btn"


@pytest.mark.asyncio
async def test_action_unit_appends_history_for_model(monkeypatch):
    """unit 子动作结果必须进逐轮上下文 HISTORY（否则模型看不见成功 → 重复执行）。"""
    _patch_settings(monkeypatch, probe=False)
    _capture_tool_calls(monkeypatch)
    bridge = _make_bridge()

    async def fake_send_act(agent_id, run_id, tool_call):
        return {"success": True}

    bridge.agent_manager.send_act = fake_send_act
    ctx: list[dict] = []
    out = await bridge._execute_action_unit(
        SimpleNamespace(id=7), 3, "agent-1", "run-1",
        [
            {"action": "fill", "selector": "#user", "value": "u"},
            {"action": "click", "selector": "#login"},
        ],
        "", ctx,
    )
    assert out["success"] is True
    assert [m["role"] for m in ctx] == ["assistant", "tool", "assistant", "tool"]
    assert "fill" in ctx[0]["content"] and "u" in ctx[0]["content"]
    assert ctx[1]["content"]


def test_collect_journal_entry_maps_unit_style_assert():
    """unit 子动作（顶层 selector/value）必须能映射 checklist 覆盖（case 551 回归）。"""
    bridge = _make_bridge()
    bridge._run_steps = [
        {"step_order": 6, "description": "断言【Remove】可见"},
        {"step_order": 7, "description": "断言页面包含【1】"},
    ]
    bridge._journal_covered = set()
    bridge._journal = []
    bridge._collect_journal_entry(
        {"action": "assert_text", "selector": "e126", "value": "Remove"}, 3,
    )
    assert 6 in bridge._journal_covered
    assert bridge._journal[-1]["value"] == "Remove"


def test_digit_assert_text_maps_when_bracketed_exact():
    """数字型期望文本（断言【1】）应映射覆盖；无括号的秒数守卫不受影响。"""
    from core.agent_bridge import pick_checklist_index

    steps = [{"step_order": 7, "description": "断言页面包含【1】"}]
    e = {"turn": 2, "success": True, "status": "ok",
         "action": "assert_text", "selector": None, "value": "1"}
    assert pick_checklist_index(steps, set(), e) == 7

    # 值不在括号里全等 → 仍按秒数守卫拒绝
    steps2 = [{"step_order": 8, "description": "断言页面包含【Products】"}]
    e2 = {"turn": 2, "success": True, "status": "ok",
          "action": "assert_text", "selector": None, "value": "2"}
    assert pick_checklist_index(steps2, set(), e2) is None


@pytest.mark.asyncio
async def test_maybe_engine_done_completes_when_covered_and_assert_last(monkeypatch):
    bridge = _make_bridge()
    bridge._run_steps = [{"step_order": 1}, {"step_order": 2}]
    bridge._journal_covered = {1, 2}

    completed: list[tuple] = []

    async def fake_complete(run_id, turn):
        completed.append((run_id, turn))

    async def _noop_message(*args, **kwargs):
        return None

    monkeypatch.setattr(bridge, "_complete", fake_complete)
    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_message", _noop_message)

    ok = await bridge._maybe_engine_done(
        SimpleNamespace(id=99), 4, ["打开", "断言"],
        {"success": True, "last_action": "assert_text"},
    )
    assert ok is True
    assert completed == [(99, 4)]


@pytest.mark.asyncio
async def test_maybe_engine_done_negative_cases(monkeypatch):
    bridge = _make_bridge()
    bridge._run_steps = [{"step_order": 1}, {"step_order": 2}]
    bridge._journal_covered = {1}  # 未覆盖全

    async def _noop_message(*args, **kwargs):
        return None

    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_message", _noop_message)
    ran = SimpleNamespace(id=99)

    # 覆盖不全 → 不完成
    assert not await bridge._maybe_engine_done(
        ran, 4, ["a", "b"], {"success": True, "last_action": "assert_text"},
    )
    bridge._journal_covered = {1, 2}
    # 末步不是断言 → 不完成
    assert not await bridge._maybe_engine_done(
        ran, 4, ["a", "b"], {"success": True, "last_action": "click"},
    )
    # 无清单 → 不完成
    assert not await bridge._maybe_engine_done(
        ran, 4, None, {"success": True, "last_action": "assert_text"},
    )


@pytest.mark.asyncio
async def test_action_unit_records_failed_subaction(monkeypatch):
    _patch_settings(monkeypatch, probe=False)
    calls = _capture_tool_calls(monkeypatch)
    bridge = _make_bridge()

    async def fake_send_act(agent_id, run_id, tool_call):
        if tool_call.get("selector") == "#user":
            return {"success": True}
        return {"success": False, "error": "strict mode violation"}

    bridge.agent_manager.send_act = fake_send_act

    out = await bridge._execute_action_unit(
        SimpleNamespace(id=7), 3, "agent-1", "run-1",
        [
            {"action": "fill", "selector": "#user", "value": "u"},
            {"action": "click", "selector": "#nope"},
        ],
        "", [],
    )
    assert out.get("success") is False
    assert [(c["name"], c["success"]) for c in calls] == [("fill", True), ("click", False)]
    assert "strict mode violation" in calls[1]["error"]
