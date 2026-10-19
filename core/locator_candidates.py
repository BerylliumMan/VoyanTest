"""Candidate extraction and snapshot-version validation for UI actions."""

from __future__ import annotations

import re
from hashlib import sha256
from dataclasses import dataclass
from typing import Final, Mapping


_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:ref|target)\s*[=:]\s*((?:f\d+e\d+)|(?:e\d+))", re.IGNORECASE
)
_ROLE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*[-*]?\s*([a-z][\w-]*)\s+['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)
# 无名称元素的角色识别（如 `- textbox [ref=e13]`：交互控件常无可访问名——
# 此前只认「角色 + 引号名称」，把这类元素标成 unknown、丢失控件角色，模型无法识别
# 输入框/按钮（2026-10-14 实测：无标注表格表单页全部元素退化为 unknown）
_ROLE_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*[-*]?\s*([a-z][\w-]*)\s*(?:\[|:|\s*$)",
    re.IGNORECASE,
)
# 冒号行内文本形式（如 `- generic [ref=f1e9] [cursor=pointer]: 新增`）：
# 中文后台常见「div 按钮」——非标准控件没有 role/name，但快照在冒号后带可见文案。
# 此前该文案被丢弃 → 候选渲染为空名，模型「截图里看得到按钮、候选里却找不到」
# （2026-10-19 实测复现：函件录入类页面 div 工具栏按钮全部空名）。
_COLON_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*[-*]?\s*([a-z][\w-]*)\s*(?:\[[^\]]*\]\s*)*:\s*(.+)$",
    re.IGNORECASE,
)
# 子文本节点形式（如 `<span onclick>新增</span>` → `- generic [ref=e2]:` 下一行
# `  - text: 新增`）：标签在直接子文本节点上（2026-10-19 实测复现，第三种形态）。
_TEXT_NODE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[-*]?\s*text:\s*(.+)$")
_HIDDEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:aria-hidden\s*[=:]\s*['\"]?true|\bhidden\b)", re.IGNORECASE
)
_DISABLED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:aria-disabled\s*[=:]\s*['\"]?true|\bdisabled\b)", re.IGNORECASE
)
# 可点击标记：div 按钮/图标按钮（非标准控件）靠 `[cursor=pointer]` 暴露可点性，
# 截断保护据此把它们视为操作控件（2026-10-19）。
_CURSOR_POINTER_RE: Final[re.Pattern[str]] = re.compile(
    r"\[cursor\s*=\s*pointer\]", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class LocatorCandidate:
    """One actionable element advertised by the current snapshot."""

    ref: str
    role: str
    name: str
    text: str
    attributes: Mapping[str, str]
    frame_path: tuple[str, ...]
    visible: bool = True
    enabled: bool = True
    obscured: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotState:
    """Immutable snapshot state used to bind decisions to a page version."""

    version: str
    url: str
    raw_snapshot: str
    candidates: tuple[LocatorCandidate, ...]


@dataclass(frozen=True, slots=True)
class LocatorValidation:
    """Result of validating a model-selected candidate ref."""

    valid: bool
    failure_kind: str | None = None
    candidate: LocatorCandidate | None = None


@dataclass(frozen=True, slots=True)
class LocatorDecision:
    """Model decision constrained to a snapshot candidate set."""

    action: str
    candidate_ref: str | None
    snapshot_version: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class LocatorEvidence:
    """Persistable evidence connecting an action to before/after state."""

    before_snapshot_version: str
    after_snapshot_version: str
    verification_type: str
    verification_value: str | None
    verified: bool
    error: str | None = None


def _child_text_label(lines: list[str], idx: int, *, max_look: int = 8) -> str:
    """取 ref 元素首个直接子文本节点的文案（非标准控件标签的第三种来源）。

    仅当 ref 行的整个更深块中**不出现任何带 ref 的行**时生效——容器里若还有
    其它可点元素（如工具栏 div 同时包着「新增」文本与「删除」链接），标签会
    误导点击（2026-10-19 实测：模型点了容器中心、span 的 onclick 不触发），
    这种情况交由 click 的 text 文案兜底处理。
    """
    base_indent = len(lines[idx]) - len(lines[idx].lstrip())
    label = ""
    for j in range(idx + 1, min(idx + 1 + max_look, len(lines))):
        line = lines[j]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        if _REF_RE.search(line):
            return ""  # 更深块里还有可点元素，不臆测容器标签
        if not label:
            m = _TEXT_NODE_RE.match(line)
            if m:
                label = m.group(1).strip().strip('"').strip()[:80]
    return label


def extract_candidates(snapshot: str, *, frame_path: tuple[str, ...] = ()) -> tuple[LocatorCandidate, ...]:
    """Extract role/name/ref candidates from an accessibility snapshot."""
    candidates: list[LocatorCandidate] = []
    lines = snapshot.splitlines()
    for idx, line in enumerate(lines):
        ref_match = _REF_RE.search(line)
        if ref_match is None:
            continue
        role_name = _ROLE_NAME_RE.search(line)
        if role_name is not None:
            role = role_name.group(1)
            name = role_name.group(2).strip()
        else:
            colon = _COLON_TEXT_RE.match(line)
            if colon is not None:
                # 非标准控件（div 按钮等）：取冒号后的可见文案作标签
                role = colon.group(1)
                name = colon.group(2).strip().strip('"').strip()[:80]
            else:
                role_only = _ROLE_ONLY_RE.search(line)
                role = role_only.group(1) if role_only is not None else "unknown"
                name = ""
        if not name:
            name = _child_text_label(lines, idx)
        candidates.append(
            LocatorCandidate(
                ref=ref_match.group(1),
                role=role,
                name=name,
                text=name,
                attributes=(
                    {"cursor": "pointer"} if _CURSOR_POINTER_RE.search(line) else {}
                ),
                frame_path=frame_path,
                visible=not _HIDDEN_RE.search(line),
                enabled=not _DISABLED_RE.search(line),
            )
        )
    return tuple(candidates)


def snapshot_has_visible_overlay(snapshot: str) -> bool:
    """Detect visible dialog/alert regions advertised by a snapshot."""
    return bool(
        re.search(
            r"(?:dialog|alertdialog|el-dialog__wrapper|notification)",
            snapshot,
            re.IGNORECASE,
        )
    )


def snapshot_version(snapshot: str) -> str:
    """Create a deterministic short version for a snapshot payload."""
    return sha256(snapshot.encode("utf-8")).hexdigest()[:16]


def is_snapshot_ref(value: str | None) -> bool:
    """Return whether a selector uses the MCP snapshot ref syntax."""
    return bool(value and re.fullmatch(r"(?:f\d+e\d+|e\d+)", value))


_EPHEMERAL_REF_RE: Final[re.Pattern[str]] = re.compile(r"(?:ref_|probe_idx_)\d+", re.I)
# Playwright 侧的 ref 选择器语法（MCP 内部就是 page.locator('aria-ref=e3')）。
# 它同样只在当次快照有效，一旦被固化，下次直跑必定指向不存在的元素。
_ARIA_REF_SELECTOR_RE: Final[re.Pattern[str]] = re.compile(
    r"aria-ref\s*=\s*(?:f\d+)?e\d+", re.I
)


def is_ephemeral_locator_ref(value: str | None) -> bool:
    """快照 ref / probe 候选 id 都是临时的，绝不能当作可持久化定位符。

    真实 ref 形态为 ``e12``（主 frame）或 ``f3e24``（第 3 个 frame 的第 24 个
    元素）；等价写法 ``aria-ref=e12`` 也只在当次快照有效。判定必须与提取侧共用
    :func:`is_snapshot_ref`，**不要**自带只认某个固定 frame 号的写法（历史 bug：
    ``(?:e|f5e|ref_|probe_idx_)\\d+`` 只认 frame 5，于是 iframe 内的 ref 漏判，
    被当成持久定位符写进 journal.replay.playwright_locator，合成进固化脚本后变成
    ``page.locator("f3e24")`` —— 下次直跑必然「找不到元素」）。
    """
    s = (value or "").strip()
    if not s:
        return False
    if is_snapshot_ref(s) or _EPHEMERAL_REF_RE.fullmatch(s):
        return True
    return bool(_ARIA_REF_SELECTOR_RE.search(s))


_REF_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"(?:f\d+e\d+|e\d+)")


def extract_ref_token(value: str | None) -> str | None:
    """Pull the bare ref out of a pasted candidate line.

    The model sometimes copies a whole serialized candidate
    (``ref=f3e24 role=textbox name='X' frame=main``) instead of the
    bare token. Only rewrite strings that look like such a paste
    (``ref=`` marker); plain CSS selectors pass through untouched.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or is_snapshot_ref(text):
        return value
    if "ref=" in text or ("role=" in text and "frame=" in text):
        match = _REF_TOKEN_RE.search(text)
        if match:
            return match.group(0)
    return value


def validate_candidate_ref(
    ref: str | None,
    *,
    snapshot_version: str,
    decision_version: str | None,
    candidates: tuple[LocatorCandidate, ...],
) -> LocatorValidation:
    """Reject invented refs and decisions bound to an old snapshot.

    A missing decision version carries no staleness evidence (the model
    may drop the field), so only an explicit version mismatch is stale.
    """
    if decision_version is not None and decision_version != snapshot_version:
        return LocatorValidation(False, "stale_ref")
    if not ref:
        return LocatorValidation(False, "no_candidate")
    candidate = next((item for item in candidates if item.ref == ref), None)
    if candidate is None:
        return LocatorValidation(False, "no_candidate")
    if not candidate.visible:
        return LocatorValidation(False, "not_visible", candidate)
    if not candidate.enabled:
        return LocatorValidation(False, "disabled", candidate)
    if candidate.obscured:
        return LocatorValidation(False, "obscured", candidate)
    return LocatorValidation(True, candidate=candidate)


def actionable_candidates(
    candidates: tuple[LocatorCandidate, ...],
) -> tuple[LocatorCandidate, ...]:
    """Keep only candidates that are eligible for a direct action."""
    return tuple(
        candidate
        for candidate in candidates
        if candidate.visible and candidate.enabled and not candidate.obscured
    )


# 候选截断保护（2026-10-19）：中文后台列表页动辄数百个行内链接/复选框，
# 静默截断会把工具栏按钮挤出模型可见范围（实测复现：DOM 后置的「新增」按钮
# ref 排在第 621 位，前 80 截断后完全不可见；模型只能在截图里干瞪眼）。
# 截断时按角色优先级挑选：操作控件 > 链接/复选框 > 其它；保持原顺序输出。
_CONTROL_ROLES: Final[frozenset[str]] = frozenset({
    "button", "textbox", "searchbox", "combobox", "switch", "tab",
    "menuitem", "menuitemcheckbox", "menuitemradio", "slider", "spinbutton", "listbox",
})
_SECONDARY_ROLES: Final[frozenset[str]] = frozenset({
    "link", "checkbox", "radio", "option", "treeitem",
})


def prioritize_candidates(
    candidates: tuple[LocatorCandidate, ...], cap: int,
) -> tuple[tuple[LocatorCandidate, ...], int]:
    """截断保护：候选超限时优先保留操作控件，返回 (选中候选, 被省略数)。

    未超限时原样返回（omitted=0）。选中的候选保持原顺序，方便模型理解页面结构。
    """
    if cap <= 0 or len(candidates) <= cap:
        return tuple(candidates), 0

    def _tier(candidate: LocatorCandidate) -> int:
        role = (candidate.role or "").lower()
        if role in _CONTROL_ROLES:
            return 0
        if role in _SECONDARY_ROLES:
            return 1
        # 非标准控件（div 按钮/图标按钮）：cursor=pointer 即视为操作控件
        if candidate.attributes.get("cursor") == "pointer":
            return 0
        return 2

    ranked = sorted(range(len(candidates)), key=lambda i: (_tier(candidates[i]), i))
    keep = sorted(ranked[:cap])
    return tuple(candidates[i] for i in keep), len(candidates) - cap


def serialize_candidates(
    candidates: tuple[LocatorCandidate, ...],
    max_candidates: int = 40,
) -> str:
    """Serialize bounded semantic candidate data for a model decision."""
    selected, omitted = prioritize_candidates(candidates, max_candidates)
    lines = [
        f"- ref={candidate.ref} role={candidate.role} name={candidate.name!r} "
        f"frame={'>'.join(candidate.frame_path) or 'main'}"
        for candidate in selected
    ]
    if omitted:
        lines.append(f"（另有 {omitted} 个候选未列出：优先展示按钮/输入框等操作控件）")
    return "\n".join(lines)
