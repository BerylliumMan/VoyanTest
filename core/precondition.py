# core/precondition.py
"""Shared LLM response text extraction (OTA / synthesis).

nl_goal precondition verify/decide loops have been removed.
"""
from __future__ import annotations

from typing import Any


def extract_response_text(message: Any) -> str:
    """读取兼容 Qwen3 思考模式的最终文本响应。

    部分 OpenAI-compatible 服务在思考模式下将最终答案放在
    ``reasoning_content``/``reasoning``/``thinking``，而 ``content`` 为空；正常情况下仍优先使用
    ``content``，避免把内部推理误当作动作。
    """
    def flatten(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(part for item in value if (part := flatten(item)))
        if isinstance(value, dict):
            for key in ("text", "content", "value"):
                part = flatten(value.get(key))
                if part:
                    return part
        return ""

    fields = ("content", "reasoning_content", "reasoning", "thinking", "text")
    for field in fields:
        value = message.get(field) if isinstance(message, dict) else getattr(message, field, None)
        text = flatten(value)
        if text:
            return text
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        for field in fields:
            text = flatten(extra.get(field))
            if text:
                return text
    return ""
