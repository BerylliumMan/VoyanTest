# 042 US1：影响分析服务（平台内信号 → 候选集 → 回归计划）
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.services.quality_impact_queries import (
    changed_definition_ids, latest_run_baseline, case_last_runs,
)
from core.impact_signals import build_candidates, signal
from app.tz import now as tz_now


async def _case_meta(db: AsyncSession, project_id: int) -> dict[int, dict]:
    rows = await db.execute(
        select(db_models.TestCase).where(db_models.TestCase.project_id == project_id)
    )
    return {
        int(tc.id): {
            "case_kind": tc.case_kind or "ui",
            "name": tc.name,
            "module_id": tc.module_id,
        }
        for tc in rows.scalars().all()
    }


def _definition_refs(tc: db_models.TestCase) -> set[int]:
    ids: set[int] = set()
    if tc.api_definition_id:
        ids.add(int(tc.api_definition_id))
    spec = tc.api_spec if isinstance(tc.api_spec, dict) else {}
    for step in spec.get("steps") or []:
        if isinstance(step, dict) and step.get("definition_id"):
            try:
                ids.add(int(step["definition_id"]))
            except (TypeError, ValueError):
                pass
    return ids


async def analyze(
    db: AsyncSession,
    *,
    project_id: int,
    baseline_at=None,
    recent_days: int | None = None,
    stale_days: int | None = None,
    include_quarantined: bool = False,
    manual_case_ids: list[int] | None = None,
) -> dict:
    settings = get_settings()
    recent_days = int(recent_days or settings.quality_impact_recent_days)
    stale_days = int(stale_days or settings.quality_impact_stale_days)

    baseline_source = "manual"
    if baseline_at is None:
        baseline_at, baseline_source = await latest_run_baseline(db, project_id)
        if baseline_at is None:
            baseline_at = tz_now() - timedelta(days=7)
            baseline_source = "default_7d"

    changed_ids = await changed_definition_ids(db, project_id, baseline_at)
    changed: dict[int, dict] = {}
    if changed_ids:
        rows = await db.execute(
            select(db_models.ApiDefinition).where(db_models.ApiDefinition.id.in_(changed_ids))
        )
        for d in rows.scalars().all():
            changed[int(d.id)] = d

    cases = await db.execute(
        select(db_models.TestCase).where(db_models.TestCase.project_id == project_id)
    )
    all_cases = list(cases.scalars().all())
    meta = {
        int(tc.id): {
            "case_kind": tc.case_kind or "ui",
            "name": tc.name,
            "module_id": tc.module_id,
        }
        for tc in all_cases
    }

    signals: dict[int, list[dict]] = {}
    for tc in all_cases:
        refs = _definition_refs(tc)
        for did in refs & set(changed):
            d = changed[did]
            signals.setdefault(int(tc.id), []).append(signal(
                "definition_change", ref=f"definition:{did}",
                detail=f"{d.method} {d.path}（v{d.version}）",
            ))

    # 近 N 天失败
    since = tz_now() - timedelta(days=recent_days)
    failed = await db.execute(
        select(db_models.TestRun.case_id, db_models.TestRun.id)
        .join(db_models.RunBatch, db_models.RunBatch.id == db_models.TestRun.batch_id)
        .where(
            db_models.RunBatch.project_id == project_id,
            db_models.TestRun.status.in_(["failed", "error"]),
            db_models.TestRun.case_id.is_not(None),
            func.coalesce(
                db_models.TestRun.start_time,
                db_models.TestRun.end_time,
                db_models.RunBatch.created_at,
            ) >= since,
        )
    )
    for case_id, run_id in failed.all():
        if case_id is None:
            continue
        cid = int(case_id)
        signals.setdefault(cid, []).append(signal(
            "recent_failure", ref=f"run:{run_id}", detail=f"最近 {recent_days} 天失败过",
        ))

    # 长期未执行
    last = await case_last_runs(db, project_id)
    stale_before = tz_now() - timedelta(days=stale_days)
    for tc in all_cases:
        cid = int(tc.id)
        last_at = last.get(cid)
        if last_at is None:
            continue
        if last_at < stale_before:
            signals.setdefault(cid, []).append(signal(
                "stale", ref=f"case:{cid}", detail=f"最近执行 {last_at.date().isoformat()}",
            ))

    # Flaky / 已隔离
    flaky = await db.execute(
        select(db_models.QualityFlakyState).where(
            db_models.QualityFlakyState.project_id == project_id,
            db_models.QualityFlakyState.status.in_(["candidate", "quarantined"]),
        )
    )
    for state in flaky.scalars().all():
        if state.status == "quarantined" and not include_quarantined:
            continue
        signals.setdefault(int(state.case_id), []).append(signal(
            "flaky", ref=f"case:{state.case_id}",
            detail=f"Flaky {state.passed} 通过 / {state.failed} 失败",
        ))

    manual = [int(x) for x in (manual_case_ids or [])]
    candidates = build_candidates(
        signals, case_meta=meta, manual_case_ids=manual,
        max_candidates=settings.quality_impact_max_candidates,
    )
    counts: dict[str, int] = {}
    for item in candidates:
        for reason in item["reasons"]:
            counts[reason["type"]] = counts.get(reason["type"], 0) + 1
    counts["total"] = len(candidates)
    notes = []
    if not changed_ids:
        notes.append("未发现基线之后有接口定义变更")
    if not candidates:
        notes.append("没有足够信号，建议全量执行或手工选择用例")
    return {
        "baseline_at": baseline_at.isoformat() if baseline_at else None,
        "baseline_source": baseline_source,
        "candidates": candidates,
        "counts": counts,
        "notes": notes,
        "changed_definition_ids": sorted(changed_ids),
    }
