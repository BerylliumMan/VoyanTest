"""报告步骤行渲染与 run_logs 回退映射（2026-10-10 实测缺陷回归）。

背景：
- 批次 483（服务端 OTA，run 737）详情显示"两个步骤都失败但结果通过"：
  服务端路径只写了一条摘要行（level="INFO" 大写）+ 一条 console 行（level="warning"），
  回退映射 `lg.level in ("info", ...)` 大小写不敏感缺失 → INFO 行被判为失败步骤；
  warning 行同样被判失败。
- 修复：core/step_logs.py 统一「agent_tool_calls → 步骤行」；report_router
  的 run_logs 回退映射大小写不敏感，warning 类运行级信息标 status="info"。
"""
from types import SimpleNamespace

import pytest

from core import step_logs
from core.console_evidence import parse_console_messages
from app.routers.report_router import _steps_from_run_log_rows


def test_render_step_message_labels_values_and_error():
    assert step_logs.render_step_message("browser_navigate", {"url": "http://x"}) == "导航到 http://x"
    assert step_logs.render_step_message("fill", {"value": "admin", "selector": "e1"}) == "fill admin"
    assert step_logs.render_step_message("click", {"selector": "e9"}) == "click e9"
    msg = step_logs.render_step_message("click", {"selector": "e9"}, "未知操作: click")
    assert msg.startswith("click e9 | 未知操作: click")


def test_render_step_message_uncertain_note():
    msg = step_logs.render_step_message("click", {"selector": "e1", "_uncertain": True})
    assert "_uncertain" in msg or "不确定" in msg


def test_render_step_message_empty():
    assert step_logs.render_step_message("done", None) == "done"


@pytest.mark.asyncio
async def test_build_step_logs_from_tool_calls(monkeypatch):
    rows = [
        SimpleNamespace(id=1, turn_number=1, tool_name="goto", tool_args={"value": "http://a"}, success=1, error_message=None),
        SimpleNamespace(id=2, turn_number=2, tool_name="assert_text", tool_args={"selector": "e47", "value": "VoyanTest"}, success=0, error_message="未知操作: assert_text"),
        SimpleNamespace(id=3, turn_number=3, tool_name="wait", tool_args={"value": "VoyanTest"}, success=1, error_message=None),
    ]

    class _Res:
        def scalars(self):
            return self

        def all(self):
            return rows

    class _Db:
        async def execute(self, stmt):
            return _Res()

    shots = {}
    logs = await step_logs.build_step_logs_from_tool_calls(
        _Db(), 415, resolve_screenshot=lambda t: shots.get(t)
    )
    assert [l["level"] for l in logs] == ["info", "error", "info"]
    assert logs[1]["message"].startswith("assert_text VoyanTest | 未知操作")
    # 失败行尝试解析截图（这里未配置 → None）；成功行不解析
    assert logs[0]["screenshot_path"] is None


def test_steps_from_run_log_rows_case_insensitive_and_info():
    rows = [
        SimpleNamespace(step_id=None, level="INFO", message="AgentRunner 完成: status=completed", screenshot_path=None),
        SimpleNamespace(step_id=None, level="warning", message='[console] Returning 10 messages...', screenshot_path=None),
        SimpleNamespace(step_id=None, level="ERROR", message="断言失败", screenshot_path="/x.png"),
        SimpleNamespace(step_id=3, level="info", message="固化脚本回放：打开 — 通过", screenshot_path=None),
    ]
    steps = _steps_from_run_log_rows(rows)
    assert steps[0]["success"] is True and steps[0]["status"] is None
    assert steps[1]["success"] is None and steps[1]["status"] == "info"
    assert steps[2]["success"] is False
    assert steps[3]["step_number"] == 3 and steps[3]["success"] is True


def test_parse_console_messages_skips_zero_count_header():
    text = 'Returning 0 messages for level "error".\n### Result\n[ERROR] boom @ http://a'
    entries = parse_console_messages(text)
    assert entries == ["[ERROR] boom @ http://a"]
    # 非零计数行保留（含数量信息）
    text2 = 'Returning 2 messages for level "error"'
    assert parse_console_messages(text2) == [text2]
