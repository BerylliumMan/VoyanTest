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
_HIDDEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:aria-hidden\s*[=:]\s*['\"]?true|\bhidden\b)", re.IGNORECASE
)
_DISABLED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:aria-disabled\s*[=:]\s*['\"]?true|\bdisabled\b)", re.IGNORECASE
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


def extract_candidates(snapshot: str, *, frame_path: tuple[str, ...] = ()) -> tuple[LocatorCandidate, ...]:
    """Extract role/name/ref candidates from an accessibility snapshot."""
    candidates: list[LocatorCandidate] = []
    for line in snapshot.splitlines():
        ref_match = _REF_RE.search(line)
        if ref_match is None:
            continue
        role_name = _ROLE_NAME_RE.search(line)
        role = role_name.group(1) if role_name else "unknown"
        name = role_name.group(2).strip() if role_name else ""
        candidates.append(
            LocatorCandidate(
                ref=ref_match.group(1),
                role=role,
                name=name,
                text=name,
                attributes={},
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


def serialize_candidates(
    candidates: tuple[LocatorCandidate, ...],
    max_candidates: int = 40,
) -> str:
    """Serialize bounded semantic candidate data for a model decision."""
    return "\n".join(
        f"- ref={candidate.ref} role={candidate.role} name={candidate.name!r} "
        f"frame={'>'.join(candidate.frame_path) or 'main'}"
        for candidate in candidates[:max_candidates]
    )
