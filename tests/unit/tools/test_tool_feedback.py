"""044 US3：结构化工具反馈（先红）。"""

from core.tools.feedback import (
    format_tool_error,
    format_validation_error,
    tool_message_content,
)
from core.tools.result import ToolResult, error_result


def test_tool_error_uses_wrapper_and_includes_name():
    text = format_tool_error("run_case", "环境不可达")
    assert text.startswith("<tool_use_error>")
    assert "run_case" in text and "环境不可达" in text
    assert text.endswith("</tool_use_error>")


def test_validation_error_lists_each_issue():
    text = format_validation_error("read_file", ["缺少参数 file_path", "参数 offset 类型应为 number"])
    assert "缺少参数 file_path" in text
    assert "参数 offset 类型应为 number" in text


def test_tool_message_content_marks_truncation_with_recovery():
    r = ToolResult(ok=True, summary="ok", data={"truncated": True}, truncated=True)
    text = tool_message_content(r, tool_name="search_cases")
    assert "truncated" in text
    assert "翻页" in text


def test_failed_result_renders_structured_error():
    r = error_result("未知工具：foo")
    text = tool_message_content(r, tool_name="foo")
    assert "<tool_use_error>" in text
