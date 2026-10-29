# 042 US2：Flaky 治理服务（候选/确认/隔离/恢复/自动跳过）
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.config import get_settings
from app.tz import now as tz_now
from core.flaky_state import next_after_confirm, next_consecutive_passes

logger = logging.getLogger(__name__)


async def _case_status_counts(db: AsyncSession, project_id: int, days: int) -> dict[int, dict]:
    since = tz_now() - timedelta(days=days)
    rows = await db.execute(
        select(
            db_models.TestRun.case_id,
            db_models.TestRun.status,
            func.count().label("n"),
            func.max(func.coalesce(db_models.TestRun.start_time, db_models.TestRun.end_time,
                                   db_models.RunBatch.created_at)).label("last_at"),
        )
        .join(db_models.RunBatch, db_models.RunBatch.id == db_models.TestRun.batch_id)
        .where(
            db_models.RunBatch.project_id == project_id,
            db_models.TestRun.case_id.is_not(None),
            func.coalesce(db_models.TestRun.start_time, db_models.TestRun.end_time,
                          db_models.RunBatch.created_at) >= since,
        )
        .group_by(db_models.TestRun.case_id, db_models.TestRun.status)
    )
    out: dict[int, dict] = {}
    for cid, status, n, last_at in rows.all():
        if cid is None:
            continue
        item = out.setdefault(int(cid), {"passed": 0, "failed": 0, "last_failed_at": None})
        if status in ("passed", "success"):
            item["passed"] += int(n)
        elif status in ("failed", "error"):
            item["failed"] += int(n)
            if last_at and (item["last_failed_at"] is None or last_at > item["last_failed_at"]):
                item["last_failed_at"] = last_at
    return out


async def refresh_candidates(db: AsyncSession, project_id: int, days: int = 14) -> list[db_models.QualityFlakyState]:
    settings = get_settings()
    counts = await _case_status_counts(db, project_id, days)
    existing = {int(s.case_id): s for s in (await crud_quality_list(db, project_id))}
    for cid, item in counts.items():
        if item["passed"] <= 0 or item["failed"] <= 0:
            continue
        if item["passed"] + item["failed"] < settings.quality_flaky_min_runs:
            continue
        current = existing.get(cid)
        await db_models_and_upsert(
            db, project_id, cid, item,
            status=(current.status if current is not None else "candidate"),
        )
    rows = await crud_quality_list(db, project_id)
    return rows


async def crud_quality_list(db, project_id):
    from app.crud import quality as cq
    return await cq.list_flaky(db, project_id)


async def db_models_and_upsert(db, project_id, case_id, item, status="candidate"):
    from app.crud import quality as cq
    return await cq.upsert_flaky(
        db, project_id=project_id, case_id=case_id,
        status=status, sample_runs=item["passed"] + item["failed"],
        passed=item["passed"], failed=item["failed"],
        last_failed_at=item.get("last_failed_at"),
    )


async def confirm(db: AsyncSession, *, project_id: int, case_id: int, environment_id: int | None,
                  user_id: int | None) -> dict:
    settings = get_settings()
    from app.crud import quality as cq
    state = await cq.get_flaky(db, case_id)
    if state is None:
        raise ValueError("未找到该用例的 Flaky 候选")
    if state.project_id != project_id:
        raise PermissionError("跨项目")
    if int(state.sample_runs or 0) < settings.quality_flaky_min_runs:
        raise ValueError(f"样本不足（{state.sample_runs}<{settings.quality_flaky_min_runs}）")
    n = max(1, int(settings.quality_flaky_confirm_runs))
    from core.runner import run_batch_test_cases
    results: list[str] = []
    for _ in range(n):
        await run_batch_test_cases(
            [case_id], project_id, batch_id=None, environment_id=environment_id,
        )
        row = await db.execute(
            select(db_models.TestRun.status)
            .where(db_models.TestRun.case_id == case_id)
            .order_by(db_models.TestRun.id.desc()).limit(1)
        )
        results.append(str(row.scalar_one_or_none() or "error"))

    status = next_after_confirm(results=results, confirm_runs=n)
    state = await cq.upsert_flaky(
        db, project_id=project_id, case_id=case_id,
        status=status, last_checked_at=tz_now(),
        updated_by=user_id,
        reason=("复跑出现失败，已隔离" if status == "quarantined" else "复跑全部通过，未隔离"),
    )
    return {
        "case_id": case_id, "status": state.status, "confirm_runs": n,
        "results": results, "sample_runs": state.sample_runs,
        "passed": state.passed, "failed": state.failed,
    }


async def quarantine(db: AsyncSession, *, project_id: int, case_id: int,
                     user_id: int | None, reason: str | None = None):
    from app.crud import quality as cq
    state = await cq.get_flaky(db, case_id)
    if state is None:
        raise ValueError("未找到该用例的 Flaky 记录")
    if state.project_id != project_id:
        raise PermissionError("跨项目")
    return await cq.upsert_flaky(
        db, project_id=project_id, case_id=case_id, status="quarantined",
        reason=reason or "人工隔离", updated_by=user_id,
    )


async def release(db: AsyncSession, *, project_id: int, case_id: int, user_id: int | None):
    from app.crud import quality as cq
    state = await cq.get_flaky(db, case_id)
    if state is None:
        raise ValueError("未找到该用例的 Flaky 记录")
    if state.project_id != project_id:
        raise PermissionError("跨项目")
    return await cq.upsert_flaky(
        db, project_id=project_id, case_id=case_id, status="recovered",
        consecutive_passes=0, reason="人工解除隔离", updated_by=user_id,
    )


async def quarantined_ids(db: AsyncSession, case_ids: list[int]) -> set[int]:
    from app.crud import quality as cq
    return await cq.quarantined_case_ids(db, case_ids)


AUTO_SOURCES = {"suite", "scheduler", "regression_plan", "full"}


async def filter_auto_cases(db: AsyncSession, case_ids: list[int], source: str | None):
    """自动展开执行跳过已隔离用例；显式批跑（source 为空）不跳过。"""
    if (source or "") not in AUTO_SOURCES:
        return list(case_ids), 0
    quarantined = await quarantined_ids(db, case_ids)
    kept = [cid for cid in case_ids if int(cid) not in quarantined]
    return kept, len(quarantined & set(int(x) for x in case_ids))


async def on_batch_finished(db: AsyncSession, batch_id: int) -> None:
    """批次完成后更新隔离用例连续通过计数；幂等、失败不阻断批次。"""
    try:
        ids = await db.execute(
            select(db_models.TestRun.case_id).where(
                db_models.TestRun.batch_id == batch_id,
                db_models.TestRun.case_id.is_not(None),
            )
        )
        case_ids = {int(x) for x in ids.scalars().all()}
        if not case_ids:
            return
        rows = await db.execute(
            select(db_models.QualityFlakyState).where(
                db_models.QualityFlakyState.case_id.in_(case_ids)
            )
        )
        states = list(rows.scalars().all())
        settings = get_settings()
        for state in states:
            tr = await db.execute(
                select(db_models.TestRun.status)
                .where(db_models.TestRun.batch_id == batch_id, db_models.TestRun.case_id == state.case_id)
                .order_by(db_models.TestRun.id.desc()).limit(1)
            )
            status = str(tr.scalar_one_or_none() or "")
            state.consecutive_passes = next_consecutive_passes(state.consecutive_passes, status)
            if state.status == "quarantined" and state.consecutive_passes >= settings.quality_flaky_recover_runs:
                state.status = "recovered"
                state.reason = f"连续 {state.consecutive_passes} 次通过，自动恢复"
            await db.commit()
    except Exception:
        logger.warning("Flaky 恢复回调失败 batch=%s", batch_id, exc_info=True)
