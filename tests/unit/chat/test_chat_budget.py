"""044 US1：token 估算与上下文预算（先红）。"""

from core.chat.budget import (
    estimate_messages_tokens,
    estimate_tokens,
    fit_to_budget,
    resolve_context_budget_tokens,
)


def test_estimate_tokens_mixed_text():
    assert estimate_tokens("hello world") == 3          # 11 个 ASCII / 4 向上取整
    assert estimate_tokens("你好世界") == 4               # CJK 1 字 1 token
    assert estimate_tokens("") == 0


def test_estimate_messages_tokens_sums_content_and_args():
    msgs = [
        {"role": "system", "content": "x" * 400},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}]},
    ]
    assert estimate_messages_tokens(msgs) >= 100


def test_resolve_context_budget_uses_70_percent_window():
    class S:
        chat_context_max_tokens = 0

    assert resolve_context_budget_tokens(settings=S(), max_context_tokens=131072) == 91750
    S.chat_context_max_tokens = 60000
    assert resolve_context_budget_tokens(settings=S(), max_context_tokens=131072) == 60000


def test_fit_to_budget_keeps_system_and_newest_messages():
    msgs = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "old " * 2000},
        {"role": "assistant", "content": "new " * 10},
    ]
    out = fit_to_budget(msgs, max_tokens=200)
    assert out[-1]["content"] == msgs[-1]["content"]
    assert out[0]["role"] == "system"
    assert sum(len(m["content"]) for m in out) < sum(len(m["content"]) for m in msgs)
