# 042 US2：Flaky 状态机（纯函数）
from __future__ import annotations

STATUSES = {"candidate", "confirming", "quarantined", "recovered"}
_TRANSITIONS = {
    "candidate": {"confirming", "quarantined", "recovered"},
    "confirming": {"candidate", "quarantined"},
    "quarantined": {"recovered", "quarantined"},
    "recovered": {"candidate", "quarantined"},
}


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, set())


def next_after_confirm(*, results: list[str], confirm_runs: int) -> str:
    """复跑结果：全部通过 → candidate；出现失败 → quarantined。"""
    if not results:
        return "candidate"
    if any(str(r).lower() in ("failed", "error") for r in results):
        return "quarantined"
    return "candidate"


def next_consecutive_passes(current: int, status: str) -> int:
    if status in ("passed", "success"):
        return int(current) + 1
    return 0
