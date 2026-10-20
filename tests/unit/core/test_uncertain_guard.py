"""US1 单测：副作用动作的 uncertain 归类与豁免（先 RED）。

覆盖：
- is_uncertain_failure 分类矩阵（副作用集合 × transport 级模式）
- 明确可操作性错误 / 非副作用动作 / 断言失败 → 非 uncertain
- unit 原地停止（不重试、不试 fallbacks）、outcome.uncertain
- bridge：annotate_uncertain / uncertain_note / uncertain_context_hint
- bridge._execute_action_unit：uncertain 单次执行 + 失败步 _uncertain 入账 + 上下文提示
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.mcp_args import SIDE_EFFECT_ACTIONS, is_uncertain_failure


# ── T005：分类矩阵 ──────────────────────────────────────────────────────────


def test_side_effect_actions_cover_core_mutations():
    for name in ("click", "double_click", "right_click", "fill", "press_key",
                 "select", "check", "drag", "click_xy", "drag_xy", "upload",
                 "dialog", "goto", "hover"):
        assert name in SIDE_EFFECT_ACTIONS, name


def test_side_effect_transport_failures_are_uncertain():
    assert is_uncertain_failure("click", "Act timeout (120s)")
    assert is_uncertain_failure({"action": "fill"}, "Connection closed")
    assert is_uncertain_failure({"name": "press_key"}, "client disconnected")
    assert is_uncertain_failure("browser_click", "连接中断")


def test_non_side_effect_actions_not_uncertain():
    assert not is_uncertain_failure("snapshot", "Act timeout (120s)")
    assert not is_uncertain_failure("wait", "Act timeout (120s)")
    assert not is_uncertain_failure("assert_text", "connection closed")
    assert not is_uncertain_failure("screenshot", "Act timeout (120s)")


def test_actionability_errors_not_uncertain():
    """派发前错误（元素未就绪/滚动失败等）保持瞬时重试语义，不标 uncertain。"""
    assert not is_uncertain_failure("click", "Timeout 30000ms exceeded waiting for element e12")
    assert not is_uncertain_failure("click", "element not found")
    assert not is_uncertain_failure("fill", "not visible")
    assert not is_uncertain_failure("click", "元素不存在")


def test_empty_error_not_uncertain():
    assert not is_uncertain_failure("click", None)
    assert not is_uncertain_failure("click", "")


# ── T006：豁免、标注、提示 ──────────────────────────────────────────────────


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_run_unit_stops_on_uncertain_without_retry_or_fallback():
    from core.action_unit import normalize_unit, run_unit

    calls = []

    async def _exec(action):
        calls.append(dict(action))
        return {"success": False, "error": "Act timeout (120s)"}

    steps, problems = normalize_unit(
        [{"action": "click", "selector": "#login", "fallbacks": ["text=登录"]}],
        max_steps=6,
        is_valid_ref=lambda ref: True,
    )
    assert steps and not problems

    outcome = await run_unit(steps, _exec, retry_delays=(0.0, 0.0), sleep=_no_sleep)
    assert outcome.success is False
    assert outcome.uncertain is True
    assert len(calls) == 1, "uncertain 不得重试、不得尝试 fallbacks"


@pytest.mark.asyncio
async def test_run_unit_transient_still_retries():
    """回归：普通瞬时错误（元素未就绪）仍走既有本地重试。"""
    from core.action_unit import normalize_unit, run_unit

    calls = []

    async def _exec(action):
        calls.append(dict(action))
        if len(calls) == 1:
            return {"success": False, "error": "element not found"}
        return {"success": True}

    steps, _ = normalize_unit(
        [{"action": "click", "selector": "#login"}],
        max_steps=6,
        is_valid_ref=lambda ref: True,
    )
    outcome = await run_unit(steps, _exec, retry_delays=(0.0, 0.0), sleep=_no_sleep)
    assert outcome.success is True
    assert len(calls) == 2
    assert outcome.uncertain is False


def test_annotate_uncertain_marks_result():
    from core.agent_bridge import annotate_uncertain

    r = {"success": False, "error": "Act timeout (120s)"}
    assert annotate_uncertain({"name": "click"}, r) is True
    assert r.get("_uncertain") is True

    r2 = {"success": False, "error": "element not found"}
    assert annotate_uncertain({"action": "click"}, r2) is False
    assert "_uncertain" not in r2

    ok = {"success": True}
    assert annotate_uncertain({"action": "click"}, ok) is False

    r3 = {"success": False, "error": "Act timeout (120s)", "_uncertain": True}
    assert annotate_uncertain({"action": "click"}, r3) is True


def test_uncertain_note_and_hint_helpers():
    from core.agent_bridge import uncertain_context_hint
    from core.mcp_args import uncertain_note

    assert uncertain_note({"_uncertain": True})
    assert uncertain_note({}) == ""
    assert uncertain_note(None) == ""

    hint = uncertain_context_hint("click")
    assert "可能已生效" in hint
    assert "click" in hint


@pytest.mark.asyncio
async def test_bridge_unit_uncertain_single_shot_and_records(monkeypatch):
    """bridge 链路：unit 子动作 uncertain → 单次执行、失败步带 _uncertain、上下文有提示。"""
    from app.config import Settings
    from core import agent_bridge as bridge_mod

    settings = Settings(action_unit_probe=False)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    calls = []

    async def fake_create_tool_call(db, run_id, turn, name, args, success, error=""):
        calls.append({"name": name, "args": args, "success": success, "error": error})

    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_tool_call", fake_create_tool_call)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(bridge_mod.crud_agent_run, "create_message", _noop)

    bridge = bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )
    send_calls = []

    async def fake_send_act(agent_id, run_id, tool_call):
        send_calls.append(tool_call)
        return {"success": False, "error": "Act timeout (120s)"}

    bridge.agent_manager.send_act = fake_send_act

    context: list = []
    out = await bridge._execute_action_unit(
        SimpleNamespace(id=7), 3, "agent-1", "run-1",
        [{"action": "click", "selector": "#login", "fallbacks": ["text=登录"]}],
        "", context,
    )

    assert out.get("success") is False
    assert out.get("uncertain") is True
    assert len(send_calls) == 1, "uncertain 不得重试/回退"

    failed = [c for c in calls if c["success"] is False]
    assert failed, "失败步必须落库"
    assert failed[-1]["args"].get("_uncertain") is True

    assert any(
        isinstance(m, dict) and "可能已生效" in str(m.get("content") or "")
        for m in context
    ), "上下文需注入重观察提示"


# ── 032 附加：WS 迟到响应（串扰）防护 ───────────────────────────────────────


def _pending_entry(fut, matcher):
    return (fut, matcher)


def test_resolve_pending_drops_late_act_result_for_observe():
    """迟到的 act 成功结果不得解析 observe 的等待（迟到/串扰丢弃）。"""
    import asyncio

    from agent.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._pending = {}
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        mgr._pending["r1"] = _pending_entry(
            fut, lambda r: ("snapshot" in r) or (r.get("success") is False)
        )
        # 迟到的 act 成功结果 → 丢弃，不消费 future
        mgr.resolve_pending("r1", {"success": True, "text": "late act result"})
        assert not fut.done()

        # 真正的 observe 结果 → 正常解析
        mgr.resolve_pending("r1", {"success": True, "snapshot": "page", "page_url": "u"})
        assert fut.done()
        assert fut.result().get("snapshot") == "page"
    finally:
        loop.close()


def test_resolve_pending_observe_failure_still_resolves():
    """observe 失败结果（无 snapshot、success=False）照常解析，避免等待直至超时。"""
    import asyncio

    from agent.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._pending = {}
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        mgr._pending["r2"] = _pending_entry(
            fut, lambda r: ("snapshot" in r) or (r.get("success") is False)
        )
        mgr.resolve_pending("r2", {"success": False, "error": "observe failed"})
        assert fut.done()
        assert fut.result()["success"] is False
    finally:
        loop.close()


def test_resolve_pending_act_rejects_late_observe_result():
    import asyncio

    from agent.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._pending = {}
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        mgr._pending["r3"] = _pending_entry(fut, lambda r: "snapshot" not in r)
        mgr.resolve_pending("r3", {"success": True, "snapshot": "late observe"})
        assert not fut.done()
        mgr.resolve_pending("r3", {"success": True, "text": "act ok"})
        assert fut.done()
        assert fut.result().get("text") == "act ok"
    finally:
        loop.close()


def test_resolve_pending_act_rejects_run_start_ready_ack():
    """037 冷启动：RUN_START 的 ready ACK（snapshot_result 形状、带 ready 键）不得
    被当作随后的 act 结果——否则首个冷启动运行会被误判「导航失败 unknown」。"""
    import asyncio

    from agent.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._pending = {}
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        mgr._pending["r5"] = _pending_entry(
            fut, lambda r: "snapshot" not in r and "ready" not in r
        )
        mgr.resolve_pending(
            "r5", {"text": "(browser ready)", "ready": True, "error": None}
        )
        assert not fut.done()
        mgr.resolve_pending("r5", {"success": True, "error": "", "screenshot_b64": ""})
        assert fut.done()
        assert fut.result().get("success") is True
    finally:
        loop.close()


def test_resolve_pending_legacy_structure_still_works():
    import asyncio

    from agent.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._pending = {}
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        mgr._pending["r4"] = fut  # 旧结构（裸 future）
        mgr.resolve_pending("r4", {"success": True})
        assert fut.done()
    finally:
        loop.close()
