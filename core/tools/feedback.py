"""044：工具成功/失败/校验错误的统一模型可见格式。

模型能否自我纠错，取决于它看到的不是裸 "Error"，而是「哪个工具、为什么、怎么修」。
"""

from __future__ import annotations

import json

from core.tools.result import TRUNCATION_GUIDANCE, ToolResult


def format_tool_error(name: str, message: str, *, hint: str | None = None) -> str:
    """失败统一信封；hint 是给模型的下一步修复建议。"""
    body = str(message or "未知错误")
    if hint:
        body += f"\n修复建议：{hint}"
    return f"<tool_use_error>\n工具 {name or '未知'} 执行失败：{body}\n</tool_use_error>"


def format_validation_error(name: str, issues: list[str]) -> str:
    """参数校验失败：逐条列出问题，便于模型逐项修正。"""
    lines = "\n".join(f"- {issue}" for issue in issues) or "- 参数不符合工具定义"
    return (
        f"<tool_use_error>\n工具 {name or '未知'} 参数校验失败：\n{lines}\n"
        "请修正参数后重试。\n</tool_use_error>"
    )


def tool_message_content(result: ToolResult, *, tool_name: str = "") -> str:
    """工具结果进入 messages 的最终文本（成功 JSON / 截断指引 / 失败信封）。"""
    if not result.ok:
        return format_tool_error(
            tool_name,
            result.error or result.summary or "未知错误",
            hint="检查参数与上下文中的对象 id；必要时先查询再重试",
        )
    text = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    if result.truncated:
        text += "\n" + TRUNCATION_GUIDANCE
    return text
