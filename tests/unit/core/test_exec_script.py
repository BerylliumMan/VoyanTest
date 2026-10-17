"""036+：执行链诊断脚本（run_script 动作）单测。

覆盖：
- runner 动作拦截（act → _execute_script_action）：门控关闭拒绝、成功/失败结果文本、
  元数据回传（result["script"]）
- build_tool_status：脚本输出回喂模型（节选）
- journal 覆盖守卫：run_script 只覆盖「脚本/查询/核对」类步骤
- step_logs：run_script 行渲染 + 证据行去重
"""
from __future__ import annotations

import pytest


class _FakeSettings:
    def __init__(self, enabled=True):
        self.exec_script_enabled = enabled
        self.chat_script_timeout_ms = 30000


def _make_runner():
    from core.agent_runner.runner import AgentRunner

    class _MCP:
        async def call_tool(self, *a, **k):
            return {"success": True, "text": ""}

    return AgentRunner(mcp_manager=_MCP(), goal="g", llm_client=None)


@pytest.mark.asyncio
async def test_runner_script_gate_off(monkeypatch):
    import app.config as app_config

    monkeypatch.setattr(app_config, "get_settings", lambda: _FakeSettings(False))
    runner = _make_runner()
    out = await runner._execute_script_action({"action": "run_script", "value": "print(1)"})
    assert out["success"] is True
    assert "未开启" in out["text"]


@pytest.mark.asyncio
async def test_runner_script_executes_and_reports(monkeypatch):
    import app.config as app_config

    monkeypatch.setattr(app_config, "get_settings", lambda: _FakeSettings(True))

    async def fake_execute_script(*, user_id, script, title="", timeout_ms=None):
        return {
            "ok": True, "output": "count= 83", "error": None, "duration_ms": 42,
            "db_queries": 1, "http_calls": 0, "truncated": False, "title": title or None,
        }

    monkeypatch.setattr("app.services.exec_script.execute_script", fake_execute_script)

    async def fake_resolve(*a, **k):
        return 7

    monkeypatch.setattr("app.services.exec_script.resolve_script_user_id", fake_resolve)

    runner = _make_runner()
    out = await runner._execute_script_action({
        "action": "run_script", "value": "print(1)", "element_desc": "查用例数",
    })
    assert out["success"] is True
    assert "查用例数" in out["text"] and "count= 83" in out["text"]
    assert out["script"]["db_queries"] == 1

    # 失败路径：结果文本带原因，动作仍视为派发成功（不触发定位类恢复）
    async def fake_fail(*, user_id, script, title="", timeout_ms=None):
        return {
            "ok": False, "output": "", "error": "敏感表 users 不允许查询", "duration_ms": 5,
            "db_queries": 0, "http_calls": 0, "truncated": False, "title": None,
        }

    monkeypatch.setattr("app.services.exec_script.execute_script", fake_fail)
    out2 = await runner._execute_script_action({"action": "run_script", "value": "x"})
    assert out2["success"] is True
    assert "失败" in out2["text"] and "敏感表" in out2["text"]


def test_build_tool_status_feeds_script_output():
    from core.mcp_args import build_tool_status

    result = {
        "success": True,
        "text": "诊断脚本「查库」成功（42ms，DB 1 / HTTP 0）：\ncount= 83",
        "script": {"ok": True, "duration_ms": 42},
    }
    status = build_tool_status(result)
    assert "count= 83" in status
    # 普通动作不带 script 键 → 保持「成功」
    assert build_tool_status({"success": True, "text": "raw mcp text"}) == "成功"


def test_journal_guard_run_script_covers_only_script_steps():
    from core.goal_agent_loop import journal_entry_covers_checklist

    entry = {"success": True, "action": "run_script", "value": "print(1)"}
    assert journal_entry_covers_checklist(entry, step_description="用诊断脚本查询数据库用例总数") is True
    assert journal_entry_covers_checklist(entry, step_description="点击【导出数据】按钮") is False
    assert journal_entry_covers_checklist(entry, step_description="断言页面出现【完成】") is False
    assert journal_entry_covers_checklist(entry, step_description="打开【数据页】") is False
    assert journal_entry_covers_checklist(entry, step_description="") is False


def test_render_and_evidence_rows():
    from core import step_logs

    msg = step_logs.render_step_message(
        "run_script",
        {"title": "查用例数", "script": {"ok": True, "duration_ms": 42, "db_queries": 1, "http_calls": 0}},
    )
    assert msg == "诊断脚本「查用例数」：成功（42ms，DB 1 / HTTP 0）"

    fail = step_logs.render_step_message(
        "run_script",
        {"script": {"ok": False, "error": "仅允许只读查询"}},
    )
    assert "失败" in fail and "仅允许只读查询" in fail

    rows = [
        {"tool": "run_script", "message": msg},
        {"tool": "click", "message": "click e1"},
        {"tool": "run_script", "message": msg},
        {"tool": "run_script", "message": "诊断脚本「另一条」：成功"},
    ]
    out = step_logs.script_evidence_rows(rows)
    assert len(out) == 2


def test_case_requires_run_script_guard():
    """含诊断脚本步骤 → 禁用编译先行/固化回放（防 Playwright 脚本假通过，case 700 实测）。"""
    from core.script_synthesize import case_requires_run_script

    assert case_requires_run_script(
        [{"description": "打开【数据页】"}, {"description": "用诊断脚本查询数据库：test_cases 表 id=699 的用例名"}]
    ) is True
    assert case_requires_run_script([{"description": "点击查询按钮"}]) is False
    assert case_requires_run_script([{"description": "查询订单列表"}]) is False
    assert case_requires_run_script([{"description": "执行脚本核对库存数量"}]) is True
    assert case_requires_run_script([]) is False
    assert case_requires_run_script(None) is False
