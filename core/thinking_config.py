"""本地 Qwen3 思考模式请求参数。"""

from __future__ import annotations

from typing import Any


def build_thinking_options(enabled: bool | None) -> dict[str, Any]:
    """将全局配置转换为 vLLM/SGLang 的 chat template 参数。"""
    if enabled is None:
        return {}
    return {"chat_template_kwargs": {"enable_thinking": enabled}}
