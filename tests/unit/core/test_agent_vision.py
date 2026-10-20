"""037 T006/T007：执行链视觉（bridge 压缩/降级 + runner 采集策略）。

覆盖：_llm_decide 截图压缩为 JPEG data URL；网关拒绝图片→纯文本重试一次并关闭本 run 视觉；
无图失败不重试；_vision_on 门控（开关 + 运行时降级）；runner observe 采集策略。
"""
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core import agent_bridge as bridge_mod
from core.llm_wrapper import PlaywrightMCPToolCall


def _png_b64(w=120, h=60) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _make_bridge():
    return bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )


def _patch_settings(monkeypatch, **overrides):
    from app.config import Settings

    settings = Settings(**overrides)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    return settings


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


def _patch_prompt(monkeypatch):
    async def fake_resolve(*args, **kwargs):
        return ""

    monkeypatch.setattr(bridge_mod, "resolve_prompt_for_agent", fake_resolve)


# ── T006: bridge 压缩与降级 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_decide_compresses_screenshot_to_jpeg_data_url(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_prompt(monkeypatch)
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return PlaywrightMCPToolCall.model_validate(
            {"thinking": "t", "action": "click", "selector": "e1"}
        )

    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)
    bridge = _make_bridge()
    await _decide(bridge, screenshot_b64=_png_b64())

    shot = captured.get("screenshot_b64") or ""
    assert shot.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(shot.split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"  # JPEG 头
    # 提示词声明附图
    assert "SCREENSHOT is attached" in captured["step_description"]


@pytest.mark.asyncio
async def test_llm_decide_vision_failure_retries_text_only_and_disables(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_prompt(monkeypatch)
    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs.get("screenshot_b64"))
        if kwargs.get("screenshot_b64"):
            raise RuntimeError("model does not support images")
        return PlaywrightMCPToolCall.model_validate(
            {"thinking": "t", "action": "done", "value": "ok"}
        )

    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)
    bridge = _make_bridge()
    tc = await _decide(bridge, screenshot_b64=_png_b64())

    assert tc is not None
    assert len(calls) == 2
    assert calls[0] and calls[1] is None  # 第二次无图
    assert bridge._vision_off is True
    assert bridge._vision_on() is False  # 本 run 剩余轮次纯文本


@pytest.mark.asyncio
async def test_llm_decide_no_screenshot_no_retry(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_prompt(monkeypatch)
    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs.get("screenshot_b64"))
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)
    bridge = _make_bridge()
    tc = await _decide(bridge)

    assert tc is None and len(calls) == 1
    assert bridge._vision_off is False


def test_vision_on_gate(monkeypatch):
    bridge = _make_bridge()
    _patch_settings(monkeypatch, exec_vision=False)
    assert bridge._vision_on() is False
    _patch_settings(monkeypatch, exec_vision=True)
    assert bridge._vision_on() is True
    bridge._vision_off = True
    assert bridge._vision_on() is False


# ── T007: runner 采集策略 ────────────────────────────────────────────────


def _make_runner(monkeypatch):
    from core.agent_runner import runner as runner_mod

    mcp = AsyncMock()
    mcp.get_dom_snapshot = AsyncMock(return_value='- button "OK" [ref=e1]')
    mcp.call_tool = AsyncMock(return_value={"success": True, "text": "http://x/"})
    r = runner_mod.AgentRunner(mcp, "goal", AsyncMock(), model="m")
    return runner_mod, r


@pytest.mark.asyncio
async def test_runner_observe_vision_every_round(monkeypatch):
    runner_mod, r = _make_runner(monkeypatch)
    _patch_settings(monkeypatch, exec_vision=True)
    # 困难页启发式返回 False → 证明 exec_vision 门控在起作用
    monkeypatch.setattr(runner_mod, "should_capture_screenshot", lambda *a, **k: False)

    async def fake_capture(mgr):
        return _png_b64()

    monkeypatch.setattr(runner_mod, "capture_screenshot_b64", fake_capture)
    obs = await r.observe()
    shot = obs["screenshot_b64"]
    assert shot and shot.startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_runner_observe_no_capture_when_vision_disabled(monkeypatch):
    runner_mod, r = _make_runner(monkeypatch)
    _patch_settings(monkeypatch, exec_vision=False)
    monkeypatch.setattr(runner_mod, "should_capture_screenshot", lambda *a, **k: False)
    called = {"n": 0}

    async def fake_capture(mgr):
        called["n"] += 1
        return _png_b64()

    monkeypatch.setattr(runner_mod, "capture_screenshot_b64", fake_capture)
    obs = await r.observe()
    assert obs["screenshot_b64"] is None and called["n"] == 0


# ── T026: US3 失败现场视觉自诊断 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_llm_decide_uses_pending_failure_shot(monkeypatch):
    """无新帧时回喂失败现场截图（一次性消费；提示词声明附图）。"""
    _patch_settings(monkeypatch)
    _patch_prompt(monkeypatch)
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return PlaywrightMCPToolCall.model_validate(
            {"thinking": "t", "action": "done", "value": "ok"}
        )

    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)
    bridge = _make_bridge()
    bridge._pending_failure_shot_b64 = _png_b64()

    await _decide(bridge)  # 无新帧
    shot = captured.get("screenshot_b64") or ""
    assert shot.startswith("data:image/jpeg;base64,")  # 压缩后回喂
    assert "SCREENSHOT is attached" in captured["step_description"]
    assert bridge._pending_failure_shot_b64 is None  # 一次性消费

    # 已消费 → 再决策不带帧
    captured.clear()
    await _decide(bridge)
    assert captured.get("screenshot_b64") is None


@pytest.mark.asyncio
async def test_bridge_llm_decide_fresh_shot_discards_pending(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_prompt(monkeypatch)
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return PlaywrightMCPToolCall.model_validate(
            {"thinking": "t", "action": "done", "value": "ok"}
        )

    monkeypatch.setattr(bridge_mod, "generate_tool_call", fake_generate)
    bridge = _make_bridge()
    bridge._pending_failure_shot_b64 = _png_b64()
    await _decide(bridge, screenshot_b64=_png_b64())  # 有新鲜帧
    assert bridge._pending_failure_shot_b64 is None  # 陈旧失败帧作废


@pytest.mark.asyncio
async def test_bridge_capture_failure_screenshot_stashes_b64(monkeypatch):
    _patch_settings(monkeypatch)
    bridge = _make_bridge()

    async def fake_observe(*a, **k):
        return {"success": True, "screenshot_b64": _png_b64()}

    bridge.agent_manager.send_observe = fake_observe
    monkeypatch.setattr(bridge, "_save_screenshot", lambda b64, name: f"reports/x/{name}.png")
    path = await bridge._capture_failure_screenshot("a", "1", name="fail_turn_02")
    assert path and bridge._pending_failure_shot_b64  # b64 已缓存


@pytest.mark.asyncio
async def test_runner_think_uses_pending_failure_shot(monkeypatch):
    runner_mod, r = _make_runner(monkeypatch)
    _patch_settings(monkeypatch)
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        from core.llm_wrapper import PlaywrightMCPToolCall

        return PlaywrightMCPToolCall.model_validate(
            {"thinking": "t", "action": "done", "value": "ok"}
        )

    monkeypatch.setattr("core.llm_wrapper.generate_tool_call", fake_generate)
    r._pending_failure_shot_b64 = "data:image/jpeg;base64," + _png_b64()

    async def fake_observe(**kwargs):
        return {"snapshot": "- button [ref=e1]", "url": "http://x/", "screenshot_b64": None,
                "snapshot_version": "v1", "candidates": ()}

    r.observe = fake_observe
    obs = await r.observe()
    await r.think(obs)
    assert captured.get("screenshot_b64", "").startswith("data:image/jpeg;base64,")
    assert "SCREENSHOT is attached" in captured["step_description"]
    assert r._pending_failure_shot_b64 is None


@pytest.mark.asyncio
async def test_runner_act_failure_stashes_shot(monkeypatch):
    runner_mod, r = _make_runner(monkeypatch)
    _patch_settings(monkeypatch)

    async def fail_once(action):
        return {"success": False, "error": "boom not transient at all"}

    monkeypatch.setattr(r, "_execute_action_once", fail_once)
    monkeypatch.setattr("core.mcp_args.is_transient_action_error", lambda *a, **k: False)

    async def fake_capture(mgr):
        return _png_b64()

    monkeypatch.setattr(runner_mod, "capture_screenshot_b64", fake_capture)
    res = await r.act({"action": "click", "selector": "e1"})
    assert res["success"] is False
    assert (r._pending_failure_shot_b64 or "").startswith("data:image/jpeg;base64,")
