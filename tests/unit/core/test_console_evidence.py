"""US2 单测：console 证据采集（先 RED）。

覆盖：
- browser_console_messages 文本解析（容错：### Result 区块 / Total 头 / 空行）
- 去重 / 上限截断（记录省略条数）
- 归属合并（失败步骤 message 合并；无失败步 → 运行级汇总行；空条目 no-op）
- console 动作参数构造（build_mcp_args / TOOL_MAP）
- collect_console_via_mcp（正常 / 异常静默降级）
"""
from __future__ import annotations

import pytest


# ── T014：动作映射 ─────────────────────────────────────────────────────────


def test_console_action_mapping():
    from core.mcp_args import TOOL_MAP, build_mcp_args

    assert TOOL_MAP.get("console") == "browser_console_messages"
    args = build_mcp_args("console", selector="", value="")
    assert args == {"level": "error", "all": True}


# ── T013：解析 / 去重 / 合并 ───────────────────────────────────────────────


def test_parse_console_messages_tolerant():
    from core.console_evidence import parse_console_messages

    text = "\n".join([
        "Total messages: 3 (Errors: 2, Warnings: 1)",
        "### Result",
        "[error] Failed to load resource: net::ERR_NAME_NOT_RESOLVED",
        "",
        "[error] Uncaught TypeError: x is not a function",
        "  ",
        "[warning] deprecation notice",
    ])
    entries = parse_console_messages(text)
    assert entries == [
        "[error] Failed to load resource: net::ERR_NAME_NOT_RESOLVED",
        "[error] Uncaught TypeError: x is not a function",
        "[warning] deprecation notice",
    ]
    assert parse_console_messages(None) == []
    assert parse_console_messages("") == []


def test_parse_truncates_long_lines():
    from core.console_evidence import MAX_ENTRY_CHARS, parse_console_messages

    entries = parse_console_messages("[error] " + "x" * 1000)
    assert len(entries) == 1
    assert len(entries[0]) == MAX_ENTRY_CHARS


def test_dedupe_cap():
    from core.console_evidence import dedupe_cap

    kept, truncated = dedupe_cap(["a", "b", "a", "c"], 10)
    assert kept == ["a", "b", "c"] and truncated == 0

    kept, truncated = dedupe_cap(["a", "b", "c", "d"], 2)
    assert kept == ["a", "b"] and truncated == 2


def test_apply_merges_into_last_failed_step():
    from core.console_evidence import apply_console_evidence

    logs = [
        {"step_id": None, "level": "info", "message": "点击 登录", "screenshot_path": None},
        {"step_id": None, "level": "error", "message": "断言 商品列表 | 超时", "screenshot_path": None},
        {"step_id": None, "level": "info", "message": "截图", "screenshot_path": None},
    ]
    out = apply_console_evidence(logs, ["[error] Boom", "[error] Bang"])
    assert out is logs
    assert out[1]["message"].endswith("| [console] [error] Boom; [error] Bang")
    assert len(out) == 3  # 不新增行


def test_apply_appends_run_level_row_when_no_failure():
    from core.console_evidence import apply_console_evidence

    logs = [{"step_id": None, "level": "info", "message": "全部通过", "screenshot_path": None}]
    out = apply_console_evidence(logs, ["[error] Console noise"])
    assert len(out) == 2
    assert out[-1]["level"] == "warning"
    assert out[-1]["message"].startswith("[console] [error] Console noise")


def test_apply_empty_entries_noop():
    from core.console_evidence import apply_console_evidence

    logs = [{"step_id": None, "level": "info", "message": "x", "screenshot_path": None}]
    out = apply_console_evidence(logs, [])
    assert out == logs


def test_apply_respects_cap_and_marks_truncation():
    from core.console_evidence import apply_console_evidence

    logs: list = []
    out = apply_console_evidence(logs, [f"e{i}" for i in range(5)], max_entries=2)
    assert len(out) == 1
    assert "其余 3 条已省略" in out[0]["message"]


# ── T013：MCP 采集适配（runner 路径）───────────────────────────────────────


class _FakeMCPManager:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.asyncio
async def test_collect_console_via_mcp_ok():
    from core.console_evidence import collect_console_via_mcp

    mgr = _FakeMCPManager({
        "success": True,
        "text": "### Result\n[error] A\n[error] B",
    })
    entries = await collect_console_via_mcp(mgr, max_entries=20)
    assert entries == ["[error] A", "[error] B"]
    assert mgr.calls == [("browser_console_messages", {"level": "error", "all": True})]


@pytest.mark.asyncio
async def test_collect_console_via_mcp_degrades_silently():
    from core.console_evidence import collect_console_via_mcp

    mgr = _FakeMCPManager(RuntimeError("mcp dead"))
    assert await collect_console_via_mcp(mgr) == []

    mgr2 = _FakeMCPManager({"success": False, "text": "boom"})
    assert await collect_console_via_mcp(mgr2) == []

    class _NoCallTool:
        pass

    assert await collect_console_via_mcp(_NoCallTool()) == []
