"""044 对话智能：token 估算与上下文预算（纯函数，无 IO）。

预算默认取模型窗口的 70%，可用 `chat_context_max_tokens` 覆盖。
估算采用启发式：ASCII 4 字符 ≈ 1 token，CJK 1 字符 ≈ 1 token。
"""

from __future__ import annotations

DEFAULT_CONTEXT_WINDOW = 131072
BUDGET_RATIO = 0.7
MIN_MESSAGE_CHARS = 1200
NEAR_MESSAGE_CHARS = 3000
KEEP_RECENT_MESSAGES = 2


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（足够做相对预算，不追求计费精度）。"""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return (ascii_chars + 3) // 4 + non_ascii


def estimate_messages_tokens(messages) -> int:
    """累加消息正文与工具调用参数（含 name / arguments）。"""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            total += estimate_tokens(str(m))
            continue
        total += estimate_tokens(str(m.get("content") or ""))
        total += estimate_tokens(str(m.get("name") or ""))
        for call in m.get("tool_calls") or []:
            fn = (call or {}).get("function") or {}
            total += estimate_tokens(str(fn.get("name") or ""))
            total += estimate_tokens(str(fn.get("arguments") or ""))
    return total


def resolve_context_budget_tokens(
    *, settings=None, max_context_tokens: int = DEFAULT_CONTEXT_WINDOW
) -> int:
    """配置值优先；否则按窗口 70% 计算。"""
    configured = 0
    if settings is not None:
        try:
            configured = int(getattr(settings, "chat_context_max_tokens", 0) or 0)
        except (TypeError, ValueError):
            configured = 0
    if configured > 0:
        return configured
    return int(max_context_tokens * BUDGET_RATIO)


def fit_to_budget(messages: list[dict], max_tokens: int) -> list[dict]:
    """最后兜底：超预算时从最早的非 system 消息开始截断，最后 2 条不动。"""
    if max_tokens <= 0 or not messages:
        return messages
    if estimate_messages_tokens(messages) <= max_tokens:
        return messages
    out = [dict(m) for m in messages]
    keep_from = max(1, len(out) - KEEP_RECENT_MESSAGES)
    for i in range(1, keep_from):
        content = str(out[i].get("content") or "")
        if len(content) <= MIN_MESSAGE_CHARS:
            continue
        out[i]["content"] = content[:MIN_MESSAGE_CHARS] + "\n…（上下文自动压缩：该消息较早内容已省略）"
        if estimate_messages_tokens(out) <= max_tokens:
            return out
    for i in range(keep_from, len(out)):
        content = str(out[i].get("content") or "")
        if len(content) <= NEAR_MESSAGE_CHARS:
            continue
        out[i]["content"] = content[:NEAR_MESSAGE_CHARS] + "\n…（上下文自动压缩：该消息内容已省略）"
        if estimate_messages_tokens(out) <= max_tokens:
            break
    return out
