"""037 T013/T014：执行链验证码动作（solve_captcha）单元测试。

覆盖：开关关闭拒绝；成功/失败结果口径；ClientBrowser/ServerBrowser 适配参数；
识别器调用参数（input_ref/refresh 透传）；tool_args 无明文（位数口径）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core import agent_bridge as bridge_mod
from core.agent_runner import runner as runner_mod


def _patch_settings(monkeypatch, **overrides):
    from app.config import Settings

    settings = Settings(**overrides)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    return settings


def _make_bridge():
    return bridge_mod.AgentBridge(
        agent_manager=AsyncMock(),
        db=AsyncMock(),
        agent_def=SimpleNamespace(name="测试执行引擎", id=1),
    )


@pytest.mark.asyncio
async def test_bridge_captcha_disabled_rejects(monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_enabled=False)
    bridge = _make_bridge()
    res = await bridge._execute_captcha_action("agent-1", "run-1", {"action": "solve_captcha"})
    assert res["success"] is False and "已关闭" in res["error"]


@pytest.mark.asyncio
async def test_bridge_captcha_success_uses_client_browser(monkeypatch):
    _patch_settings(monkeypatch)
    captured = {}

    async def fake_solve(browser, *, input_ref=None, refresh=False, solve_fn=None, **kw):
        captured.update(input_ref=input_ref, refresh=refresh, browser=browser, solve_fn=solve_fn)
        return {"ok": True, "text_len": 4, "note": ""}

    monkeypatch.setattr("core.captcha.solve_captcha_via_browser", fake_solve)
    bridge = _make_bridge()
    res = await bridge._execute_captcha_action(
        "agent-1", "run-1", {"action": "solve_captcha", "selector": "e9", "refresh": True},
    )
    assert res["success"] and "已识别并填写（4 位）" in res["text"]
    assert captured["input_ref"] == "e9" and captured["refresh"] is True
    # ClientBrowser 适配：agent/run 标识正确传入；solve_fn 为模型读图识别器（可调用）
    assert captured["browser"]._agent == "agent-1" and captured["browser"]._run_id == "run-1"
    assert callable(captured["solve_fn"])


@pytest.mark.asyncio
async def test_bridge_captcha_failure_passthrough(monkeypatch):
    _patch_settings(monkeypatch)

    async def fake_solve(browser, **kw):
        return {"ok": False, "error": "模型未能从验证码图片中读出答案"}

    monkeypatch.setattr("core.captcha.solve_captcha_via_browser", fake_solve)
    bridge = _make_bridge()
    res = await bridge._execute_captcha_action("a", "r", {"action": "solve_captcha"})
    assert res["success"] is False and "读出答案" in res["error"]


@pytest.mark.asyncio
async def test_runner_captcha_success_uses_server_browser(monkeypatch):
    _patch_settings(monkeypatch)
    captured = {}

    async def fake_solve(browser, *, input_ref=None, refresh=False, solve_fn=None, **kw):
        captured.update(browser=browser)
        return {"ok": True, "text_len": 5, "note": "已自动补回账号密码"}

    monkeypatch.setattr("core.captcha.solve_captcha_via_browser", fake_solve)
    mcp = AsyncMock()
    r = runner_mod.AgentRunner(mcp, "goal", AsyncMock(), model="m")
    res = await r._execute_captcha_action({"action": "solve_captcha", "selector": "e3"})
    assert res["success"] and "5 位" in res["text"] and "补回" in res["text"]
    assert captured["browser"].mode == "server"


@pytest.mark.asyncio
async def test_runner_captcha_disabled_rejects(monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_enabled=False)
    mcp = AsyncMock()
    r = runner_mod.AgentRunner(mcp, "goal", AsyncMock(), model="m")
    res = await r._execute_captcha_action({"action": "solve_captcha"})
    assert res["success"] is False and "已关闭" in res["error"]


@pytest.mark.asyncio
async def test_captcha_result_records_len_not_content(monkeypatch):
    """结果文本只含位数（内容不落库/不回显）。"""
    _patch_settings(monkeypatch)

    async def fake_solve(browser, **kw):
        return {"ok": True, "text_len": 6, "note": "识别成功"}

    monkeypatch.setattr("core.captcha.solve_captcha_via_browser", fake_solve)
    bridge = _make_bridge()
    res = await bridge._execute_captcha_action("a", "r", {"action": "solve_captcha"})
    assert "6 位" in res["text"] and "识别成功" in res["text"]
