"""036 US4：诊断脚本治理单测——门控（清单/提示词/MCP/调用拒绝）、审计细节、边界。

覆盖：
- chat_script_enabled=false：default_tool_filter 剔除；handler 调用拒绝；MCP 清单不含
- chat_script_enabled=true：清单包含；handler 正常执行（trivial 脚本）
- 审计 tool.run_script details：output_bytes / db_queries / http_calls / 失败 error
- 边界：12KB 节选截断标注；64KB 沙箱硬上限如实标注
"""
from __future__ import annotations

import pytest


class _FakeCtx:
    def __init__(self):
        self.db = object()
        self.user_id = 7
        self.conversation = None
        self.source = "chat"


@pytest.fixture()
def _gate(monkeypatch):
    """可切换的门控（默认关）。"""
    state = {"enabled": False}

    class _S:
        chat_script_enabled = False
        chat_script_timeout_ms = 30000

    settings = _S()

    import app.config as app_config

    monkeypatch.setattr(app_config, "get_settings", lambda: settings)

    def _set(v: bool):
        state["enabled"] = v
        settings.chat_script_enabled = v

    _set(False)
    return _set


@pytest.mark.asyncio
async def test_gate_off_handler_rejects(_gate):
    from core.tools.builtin.script import run_script_tool

    _gate(False)
    res = await run_script_tool(_FakeCtx(), script="print(1)")
    assert res.ok is False
    assert "未开启" in (res.error or "")


def test_gate_off_filter_excludes(_gate):
    from core.tools.loop import default_tool_filter

    _gate(False)
    assert default_tool_filter("run_script") is False
    assert default_tool_filter("count_cases") is True

    _gate(True)
    assert default_tool_filter("run_script") is True


def test_gate_off_prompt_and_mcp_exclude(_gate, monkeypatch):
    _gate(False)
    from core.chat.prompts import _tools_section

    section = _tools_section()
    # 门控：run_script 条目行不出现（其他工具描述里提及 run_script 属正常引导）
    assert not any(line.startswith("- `run_script`") for line in section.splitlines())
    assert "count_cases" in section

    from app.routers.mcp.server import _visible_specs

    names = {s.name for s in _visible_specs("write")}
    assert "run_script" not in names

    _gate(True)
    assert any(line.startswith("- `run_script`") for line in _tools_section().splitlines())
    names2 = {s.name for s in _visible_specs("write")}
    assert "run_script" in names2


@pytest.mark.asyncio
async def test_gate_on_trivial_script_runs(_gate, monkeypatch):
    from core.tools.builtin import script as script_mod

    _gate(True)
    monkeypatch.setattr(
        "app.services.script_sdk.build_sdk",
        lambda **kw: {},
    )
    res = await script_mod.run_script_tool(_FakeCtx(), script="a = 1 + 1\nprint(a)", title="冒烟")
    assert res.ok is True
    assert "2" in (res.data or {}).get("output", "")
    assert "冒烟" in res.summary
    assert (res.data or {}).get("duration_ms") is not None


@pytest.mark.asyncio
async def test_gate_on_output_excerpt_truncation(_gate, monkeypatch):
    from core.tools.builtin import script as script_mod

    _gate(True)
    monkeypatch.setattr("app.services.script_sdk.build_sdk", lambda **kw: {})
    script = "for i in range(200):\n    print('x' * 100)"
    res = await script_mod.run_script_tool(_FakeCtx(), script=script)
    assert res.ok is True
    data = res.data or {}
    assert data.get("truncated") is True
    assert len(data.get("output", "")) == script_mod.OUTPUT_EXCERPT_CHARS


@pytest.mark.asyncio
async def test_gate_on_hard_cap_reports_truncation(_gate, monkeypatch):
    from core.tools.builtin import script as script_mod

    _gate(True)
    monkeypatch.setattr("app.services.script_sdk.build_sdk", lambda **kw: {})
    script = "for i in range(8000):\n    print('y' * 100)"  # ~800KB > 64KB 硬上限
    res = await script_mod.run_script_tool(_FakeCtx(), script=script)
    assert res.ok is False
    assert res.truncated is True
    assert "64KB" in (res.error or "")


@pytest.mark.asyncio
async def test_audit_details_capture_script_metrics(monkeypatch):
    from core.tools.loop import _audit_tool_call
    from core.tools.result import ToolResult

    captured = {}

    async def fake_log_audit(db, user_id, action, details=None, ip_address=None):
        captured["action"] = action
        captured["details"] = details

    import app.auth as app_auth

    monkeypatch.setattr(app_auth, "log_audit", fake_log_audit)

    result = ToolResult(
        ok=True,
        summary="诊断脚本：成功",
        data={
            "output": "abc",
            "db_queries": 2,
            "http_calls": 1,
            "title": "统计用例",
        },
    )
    await _audit_tool_call(_FakeCtx(), "run_script", {"script": "print(1)"}, result)
    assert captured["action"] == "tool.run_script"
    d = captured["details"]
    assert d["db_queries"] == 2
    assert d["http_calls"] == 1
    assert d["output_bytes"] == 3
    assert d["title"] == "统计用例"

    # 失败路径：error 摘要
    fail = ToolResult(ok=False, summary="x", data={"db_queries": 0, "http_calls": 0}, error="敏感表 users 不允许查询")
    await _audit_tool_call(_FakeCtx(), "run_script", {}, fail)
    assert "敏感表" in captured["details"]["error"]
