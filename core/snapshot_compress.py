"""快照智能压缩（025-ref-click US1，契约 C3）。

替换旧的固定 8000/24000 字符硬截断。策略（ref 行优先）：
1. 所有带 [ref= 的交互元素行无条件保留——LLM 必须能看到全部可点目标
2. 无 ref 文本行在剩余预算内采样，超出部分折叠为省略标记
3. 输出严格 ≤ SNAPSHOT_MAX_CHARS（默认 30000，env 可调）

幂等：输出长度恒 ≤ 上限，二次调用走早退分支返回原文。
"""
from __future__ import annotations

import os
import re

_REF_LINE_RE = re.compile(r"\[ref=")
_DEFAULT_MAX_CHARS = 30000
_MARKER_RESERVE = 60


def max_snapshot_chars() -> int:
    try:
        return int(os.getenv("SNAPSHOT_MAX_CHARS", str(_DEFAULT_MAX_CHARS)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CHARS


def compress_snapshot(text: str, max_chars: int | None = None) -> str:
    """压缩无障碍快照：ref 行全保留，文本行按预算采样折叠。"""
    if not text:
        return text or ""
    limit = max_chars if max_chars is not None else max_snapshot_chars()
    if len(text) <= limit:
        return text

    lines = text.split("\n")
    ref_lines = [ln for ln in lines if _REF_LINE_RE.search(ln)]
    text_lines = [ln for ln in lines if not _REF_LINE_RE.search(ln)]

    # 极端情况：ref 块自身超限——按头部截断保最大数量的 ref 行
    ref_block = "\n".join(ref_lines)
    if len(ref_block) + _MARKER_RESERVE > limit:
        cut = ref_block[: limit - len("\n\n[... TRUNCATED]")]
        nl = cut.rfind("\n")
        if nl > limit // 2:
            cut = cut[:nl]
        return cut + "\n\n[... TRUNCATED]"

    # 页面根标题（首个非空非 ref 行）置顶保留上下文
    head = next((ln for ln in text_lines if ln.strip()), "")
    out: list[str] = list(ref_lines)
    used = len(ref_block) + (len(head) + 1 if head else 0)
    omitted = 0
    for ln in text_lines:
        if ln is head:
            continue
        cost = len(ln) + 1
        if used + cost <= limit - _MARKER_RESERVE:
            out.append(ln)
            used += cost
        else:
            omitted += 1
    if omitted:
        out.append("… (%d text lines omitted) …" % omitted)
    result = "\n".join(([head] if head else []) + out)

    if len(result) > limit:  # 理论兜底
        result = result[:limit] + "\n\n[... TRUNCATED]"
    return result
