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


def test_render_fetch_step_message_with_api_meta():
    """036：fetch 行按 api 元数据渲染 + 「以 API 方式完成」标注 + 存证路径。"""
    msg = step_logs.render_step_message(
        "fetch",
        {
            "api": {"method": "POST", "url": "/api/export", "status": 200, "size": 51234},
            "evidence_path": "reports/agent_1_20261016/evidence/export.csv",
        },
    )
    assert "页面 API" in msg
    assert "POST /api/export" in msg
    assert "200" in msg
    assert "以 API 方式完成" in msg
    assert "export.csv" in msg

    fail = step_logs.render_step_message(
        "fetch",
        {"api": {"method": "GET", "url": "/missing", "status": 404}},
        "页面 API 返回 404",
    )
    assert "404" in fail and "以 API 方式完成" in fail

    trunc = step_logs.render_step_message(
        "fetch",
        {
            "api": {"method": "GET", "url": "/big", "status": 200, "size": 999},
            "evidence_path": "reports/x/evidence/big.bin",
            "evidence_truncated": True,
        },
    )
    assert "已截断" in trunc


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
    # 036：行内携带工具名（供 fetch 证据行过滤）+ tool_names 过滤
    assert logs[0]["tool"] == "goto"
    fetch_only = await step_logs.build_step_logs_from_tool_calls(_Db(), 415, tool_names=["fetch"])
    assert fetch_only == []


def test_build_case_step_logs_passed_run():
    steps = [
        {"step_order": 1, "description": "打开【数据导出中心】页面"},
        {"step_order": 2, "description": "点击【导出数据】按钮"},
        {"step_order": 3, "description": "调用页面 API 确认包含 alpha"},
    ]
    rows = step_logs.build_case_step_logs(steps, {1, 2, 3}, True, None)
    assert [r["level"] for r in rows] == ["info", "info", "info"]
    assert rows[0]["message"] == "打开【数据导出中心】页面 — 通过"
    assert rows[2]["message"] == "调用页面 API 确认包含 alpha — 通过"


def test_build_case_step_logs_failed_run_marks_first_uncovered():
    steps = [
        {"step_order": 1, "description": "打开页面"},
        {"step_order": 2, "description": "点击按钮"},
        {"step_order": 3, "description": "断言出现状态"},
    ]
    rows = step_logs.build_case_step_logs(
        steps, {1}, False, "剩余步骤无法完成：[2]（连续 5 轮 done 被拒且无进展）"
    )
    assert rows[0]["level"] == "info" and rows[0]["message"].endswith("— 通过")
    assert rows[1]["level"] == "error"
    assert "点击按钮 — 失败：" in rows[1]["message"]
    assert "剩余步骤无法完成" in rows[1]["message"]
    assert rows[2]["level"] == "warning" and rows[2]["message"].endswith("— 未执行")


def test_build_case_step_logs_order_fallback_and_empty():
    # step_order 缺失 → 按序号兜底；covered 支持字符串数字
    steps = [{"description": "步骤甲"}, {"step_order": "", "description": "步骤乙"}]
    rows = step_logs.build_case_step_logs(steps, ["1"], False, None)
    assert rows[0]["message"] == "步骤甲 — 通过"
    assert rows[1]["level"] == "error" and "步骤乙 — 失败" in rows[1]["message"]
    # 空 steps → 空行
    assert step_logs.build_case_step_logs([], {1}, True, None) == []


def test_fetch_evidence_rows_dedupes_and_filters():
    msg = "页面 API GET /api/export.csv → 200 · 26B（以 API 方式完成） · 存证 reports/x.csv"
    tool_rows = [
        {"tool": "fetch", "message": msg},
        {"tool": "click", "message": "click e1"},
        {"tool": "fetch", "message": msg},  # 重复调用 → 去重
        {"tool": "fetch", "message": "页面 API GET /api/other → 404"},
    ]
    out = step_logs.fetch_evidence_rows(tool_rows)
    assert len(out) == 2
    assert out[0]["message"] == msg
    assert "other" in out[1]["message"]


def test_merge_fetch_evidence_into_steps():
    step_rows = [
        {"message": "打开页面 — 通过", "step_order": 1, "level": "info"},
        {"message": "调用 API 确认 — 通过", "step_order": 3, "level": "info"},
    ]
    fetch_rows = [
        {"tool": "fetch", "turn": 3,
         "message": "页面 API GET /api/x → 200 · 26B（以 API 方式完成） · 存证 reports/e.csv"},
        {"tool": "fetch", "turn": 9, "message": "页面 API GET /api/y → 404"},  # 无映射 → 残余行
    ]
    journal = [{"action": "fetch", "turn": 3, "checklist_index": 3}]
    leftovers = step_logs.merge_fetch_evidence_into_steps(step_rows, fetch_rows, journal)
    assert len(leftovers) == 1 and "api/y" in leftovers[0]["message"]
    assert step_rows[1]["message"] == (
        "调用 API 确认 — 通过（GET /api/x → 200 · 26B · 存证 reports/e.csv）"
    )
    # 未覆盖步骤不受影响
    assert step_rows[0]["message"] == "打开页面 — 通过"


def test_console_and_network_favicon_filtered():
    from core.console_evidence import parse_console_messages
    from core.run_evidence import network_problem_lines

    entries = parse_console_messages(
        '### Result\n[ERROR] Failed to load resource: the server responded with a status of 404 '
        '(File not found) @ http://x/favicon.ico:0\n[ERROR] real error @ http://x:0'
    )
    assert entries == ["[ERROR] real error @ http://x:0"]

    lines = network_problem_lines(
        "### Result\n1. [GET] http://x/favicon.ico => [404] File not found\n"
        "2. [GET] http://x/api => [500] boom"
    )
    assert len(lines) == 1 and "api" in lines[0]


def test_failure_reason_trimmed():
    rows = step_logs.build_case_step_logs(
        [{"step_order": 1, "description": "打开页面"}],
        set(), False,
        "导航失败，已停止执行: http://x — ### Error\nError: net::ERR_CONNECTION_REFUSED\nCall log: ...",
    )
    assert "### Error" not in rows[0]["message"]
    assert "Call log" not in rows[0]["message"]
    assert "导航失败，已停止执行: http://x" in rows[0]["message"]


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
