# 042 US1：影响信号合并与候选构建（纯函数，无 I/O）
from __future__ import annotations

from typing import Iterable

SIGNAL_LABELS = {
    "definition_change": "接口定义变更",
    "recent_failure": "近期失败",
    "stale": "长期未执行",
    "flaky": "不稳定用例",
    "module_risk": "同模块近期失败",
    "manual": "手工添加",
}


def signal(kind: str, *, ref: str = "", detail: str = "") -> dict:
    return {"type": kind, "ref": str(ref or ""), "detail": str(detail or "")}


def reason_text(reason: dict) -> str:
    label = SIGNAL_LABELS.get(reason.get("type", ""), reason.get("type", "信号"))
    detail = reason.get("detail") or ""
    return f"{label}：{detail}" if detail else label


def merge_signals(signals: Iterable[dict]) -> list[dict]:
    """同一 (type, ref) 只保留一条；同类型的单值信号（近期失败/长期未执行等）只留首条。"""
    seen = set()
    seen_single = set()
    out: list[dict] = []
    single_types = {"recent_failure", "stale", "flaky", "manual", "module_risk"}
    for item in signals or []:
        kind = str(item.get("type") or "")
        ref = str(item.get("ref") or "")
        key = (kind, ref)
        if not kind or key in seen:
            continue
        if kind in single_types and kind in seen_single:
            continue
        seen.add(key)
        seen_single.add(kind)
        out.append(signal(kind, ref=ref, detail=item.get("detail", "")))
    return out


def build_candidates(
    signals_by_case: dict[int, list[dict]],
    *,
    case_meta: dict[int, dict] | None = None,
    manual_case_ids: Iterable[int] = (),
    max_candidates: int = 300,
) -> list[dict]:
    """合并同一用例的多个信号，按历史优先级排序，去重并截断。"""
    meta = case_meta or {}
    merged: dict[int, list[dict]] = {}
    for case_id, items in (signals_by_case or {}).items():
        merged[int(case_id)] = merge_signals(items)
    for case_id in manual_case_ids or []:
        cid = int(case_id)
        merged.setdefault(cid, [])
        if not any(r.get("type") == "manual" for r in merged[cid]):
            merged[cid].append(signal("manual", ref=f"case:{cid}", detail="用户手工添加"))

    priority = {
        "definition_change": 0, "recent_failure": 1, "module_risk": 2,
        "flaky": 3, "stale": 4, "manual": 5,
    }
    rows = []
    for cid, reasons in merged.items():
        if not reasons:
            continue
        reasons = sorted(reasons, key=lambda r: priority.get(r.get("type", ""), 99))
        item = meta.get(cid, {})
        rows.append({
            "case_id": cid,
            "case_kind": item.get("case_kind") or "ui",
            "name": item.get("name") or f"用例 {cid}",
            "module_id": item.get("module_id"),
            "reasons": reasons,
        })
    rows.sort(key=lambda r: (min(priority.get(x.get("type", ""), 99) for x in r["reasons"]), r["case_id"]))
    return rows[:max(0, int(max_candidates))]
