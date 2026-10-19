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
# 截断保护（2026-10-19）：ref 块整体超限时按行级优先级挑选——
# 中文后台列表页动辄数百个行内链接/复选框，盲目从头截断会把工具栏按钮
# 挤出预算（实测：模型「截图里看得到按钮、候选里却拿不到 ref」）。
_CTRL_LINE_RE = re.compile(
    r"^\s*[-*]?\s*(button|textbox|searchbox|combobox|switch|tab|menuitem|"
    r"slider|spinbutton|listbox)\b",
    re.IGNORECASE,
)
_SECONDARY_LINE_RE = re.compile(
    r"^\s*[-*]?\s*(link|checkbox|radio|option|treeitem)\b", re.IGNORECASE
)
_CURSOR_POINTER_RE = re.compile(r"\[cursor\s*=\s*pointer\]", re.IGNORECASE)
_DEFAULT_MAX_CHARS = 30000
_MARKER_RESERVE = 60


def _ref_line_tier(line: str) -> int:
    if _CTRL_LINE_RE.search(line):
        return 0
    if _SECONDARY_LINE_RE.search(line):
        return 1
    if _CURSOR_POINTER_RE.search(line):
        return 0
    return 2


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

    # 极端情况：ref 块自身超限——优先保留操作控件行（按钮/输入框/可点元素），
    # 再按原顺序补足能放下的其它行；绝不允许把工具栏按钮挤掉（2026-10-19 实测）。
    ref_block = "\n".join(ref_lines)
    if len(ref_block) + _MARKER_RESERVE > limit:
        ranked = sorted(range(len(ref_lines)), key=lambda i: (_ref_line_tier(ref_lines[i]), i))
        keep: list[int] = []
        used = 0
        for i in ranked:
            cost = len(ref_lines[i]) + 1
            if used + cost > limit - _MARKER_RESERVE:
                continue  # 放不下则跳过；更短的低优先级行仍可补足预算
            keep.append(i)
            used += cost
        keep.sort()
        return "\n".join(ref_lines[i] for i in keep) + "\n\n[... TRUNCATED]"

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
