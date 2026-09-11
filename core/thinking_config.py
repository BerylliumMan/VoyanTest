"""本地 Qwen3 思考模式请求参数。"""

from __future__ import annotations

from typing import Any


def _provider_ignores_thinking_options(
    model: str | None = None, api_base: str | None = None
) -> bool:
    """外部 OpenAI 兼容端点是否不认 vLLM 私有扩展字段。

    按服务端（api_base）判定为主，模型名为辅：Google 收到未知字段会 400。
    """
    base = (api_base or "").lower()
    name = (model or "").lower()
    return "generativelanguage" in base or "gemini" in name


def build_thinking_options(
    enabled: bool | None,
    model: str | None = None,
    api_base: str | None = None,
) -> dict[str, Any]:
    """将全局配置转换为 vLLM/SGLang 的 chat template 参数。"""
    if enabled is None:
        return {}
    if _provider_ignores_thinking_options(model, api_base):
        return {}
    return {"chat_template_kwargs": {"enable_thinking": enabled}}
