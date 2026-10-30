"""044 对话智能：三层上下文压缩（microcompact / 摘要选择 / auto compact）。

阈值公式沿用 ZCode 的生产参数：
    threshold = context_window - min(max_output, 21000) - 13000
"""

from __future__ import annotations

from dataclasses import dataclass

from core.chat.budget import estimate_tokens

CLEARED_MARKER = "[Old tool result content cleared]"
CLEARED_NOTE = "…（旧工具结果已清理）"
DEFAULT_BUFFER_TOKENS = 13000
DEFAULT_MAX_OUTPUT_CAP = 21000
DEFAULT_KEEP_RECENT_TOOL_RESULTS = 5
DEFAULT_MIN_SAVINGS = 256


@dataclass
class CompactPolicy:
    """压缩策略：输入+输出共享窗口，阈值要扣掉输出预留与安全余量。"""

    context_window: int
    max_output_tokens: int
    buffer_tokens: int = DEFAULT_BUFFER_TOKENS

    @property
    def threshold(self) -> int:
        return (
            self.context_window
            - min(self.max_output_tokens, DEFAULT_MAX_OUTPUT_CAP)
            - self.buffer_tokens
        )


def microcompact(
    messages: list[dict],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT_TOOL_RESULTS,
    min_savings: int = DEFAULT_MIN_SAVINGS,
) -> list[dict]:
    """清空较早的工具结果内容，保留最近 N 组；节省不足则原样返回。"""
    if not messages:
        return messages
    tool_indices = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    if len(tool_indices) <= keep_recent:
        return messages

    clear_indices = tool_indices[:-keep_recent] if keep_recent > 0 else tool_indices
    out: list[dict] | None = None
    saved = 0
    for i in clear_indices:
        content = str(messages[i].get("content") or "")
        if len(content) <= len(CLEARED_MARKER):
            continue
        saved += estimate_tokens(content) - estimate_tokens(CLEARED_MARKER)
        if out is None:
            out = [dict(m) for m in messages]
        out[i]["content"] = CLEARED_MARKER + "\n" + CLEARED_NOTE
    if out is None or saved < min_savings:
        return messages
    return out


def _group_by_assistant(messages: list[dict]) -> list[list[dict]]:
    """把消息切成「user 提问 + assistant 回答 + 其工具结果」的组。"""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role == "assistant":
            # 最后一个 user 及其后内容归入本组；更早的独立成组
            split = len(cur)
            for j in range(len(cur) - 1, -1, -1):
                if cur[j].get("role") == "user":
                    split = j
                    break
            if split < len(cur):
                prefix = cur[:split]
                if prefix:
                    groups.append(prefix)
                cur = cur[split:]
            elif cur:
                groups.append(cur)
                cur = []
            cur.append(m)
        else:
            cur.append(m)
    if cur:
        groups.append(cur)
    return groups


def select_for_summary(
    messages: list[dict], *, preserve_groups: int = 1
) -> tuple[list[dict], list[dict]]:
    """返回 (较早消息, 保留原文的最近 N 组)。"""
    msgs = list(messages or [])
    if not msgs:
        return [], []
    if preserve_groups <= 0:
        return msgs, []
    groups = _group_by_assistant(msgs)
    if len(groups) <= preserve_groups:
        return [], msgs
    older = [m for g in groups[:-preserve_groups] for m in g]
    recent = [m for g in groups[-preserve_groups:] for m in g]
    return older, recent


async def auto_compact(
    messages: list[dict], llm_call, *, policy: CompactPolicy
) -> tuple[list[dict], str | None]:
    """较早消息摘要化；未到阈值、无 LLM 或摘要为空时原样返回。"""
    from core.chat.budget import estimate_messages_tokens

    if estimate_messages_tokens(messages) <= policy.threshold:
        return messages, None
    older, recent = select_for_summary(messages, preserve_groups=1)
    if not older or llm_call is None:
        return messages, None
    from core.chat.context import summarize_history

    summary = await summarize_history(llm_call, older)
    if not summary:
        return messages, None
    head = [{"role": "system", "content": f"【较早对话摘要】\n{summary}"}]
    return head + recent, summary
