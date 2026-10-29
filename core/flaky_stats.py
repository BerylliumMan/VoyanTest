# core/flaky_stats.py
# 041 US3：Flaky 确定性统计（模型不参与判定）。
from __future__ import annotations

from typing import Any, Iterable

_TERMINAL = {"passed", "failed"}
_EXCLUDED = {"cancelled", "canceled", "skipped", "pending", "running", "paused"}


def _row(item: Any) -> dict:
    if isinstance(item, dict):
        return {
            "case_id": item.get("case_id"),
            "status": str(item.get("status") or "").lower(),
            "started_at": item.get("started_at") or item.get("start_time"),
        }
    return {
        "case_id": getattr(item, "case_id", None),
        "status": str(getattr(item, "status", "") or "").lower(),
        "started_at": getattr(item, "start_time", None),
    }


def flaky_stats(
    runs: Iterable[Any],
    *,
    min_runs: int = 5,
) -> list[dict]:
    """按用例统计窗口内的 passed/failed；样本不足或全为同状态不标 Flaky。"""
    buckets: dict[int, dict] = {}
    for raw in runs or []:
        row = _row(raw)
        cid = row["case_id"]
        if cid is None:
            continue
        status = row["status"]
        if status in _EXCLUDED or status not in _TERMINAL:
            continue
        b = buckets.setdefault(cid, {
            "case_id": cid,
            "runs": 0,
            "passed": 0,
            "failed": 0,
            "last_status": None,
            "last_failed_at": None,
            "last_at": None,
        })
        b["runs"] += 1
        if status == "passed":
            b["passed"] += 1
        else:
            b["failed"] += 1
            at = row["started_at"]
            if at and (not b["last_failed_at"] or str(at) > str(b["last_failed_at"])):
                b["last_failed_at"] = at
        at = row["started_at"]
        if at and (not b["last_at"] or str(at) > str(b["last_at"])):
            b["last_at"] = at
            b["last_status"] = status

    out = []
    for b in buckets.values():
        enough = b["runs"] >= min_runs
        flaky = enough and b["passed"] > 0 and b["failed"] > 0
        if not flaky:
            continue
        out.append({
            **b,
            "pass_rate": round(b["passed"] / max(1, b["runs"]), 3),
            "enough_samples": enough,
            "flaky": True,
        })
    out.sort(key=lambda x: (-x["failed"], x["case_id"]))
    return out
