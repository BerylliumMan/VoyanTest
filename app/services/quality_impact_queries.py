# 042 US1：影响分析查询（定义基线 / 变更定义 / 用例最近执行）
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models


async def latest_run_baseline(db: AsyncSession, project_id: int) -> tuple[datetime | None, str]:
    """最近一次成功批次时间；没有则最近一次回归计划时间。"""
    row = await db.execute(
        select(db_models.RunBatch.created_at)
        .where(
            db_models.RunBatch.project_id == project_id,
            db_models.RunBatch.status == "passed",
        )
        .order_by(db_models.RunBatch.created_at.desc())
        .limit(1)
    )
    at = row.scalar_one_or_none()
    if at is not None:
        return at, "last_success_run"
    plan = await db.execute(
        select(db_models.QualityRegressionPlan.created_at)
        .where(db_models.QualityRegressionPlan.project_id == project_id)
        .order_by(db_models.QualityRegressionPlan.created_at.desc())
        .limit(1)
    )
    at = plan.scalar_one_or_none()
    if at is not None:
        return at, "last_plan"
    return None, "default_7d"


async def changed_definition_ids(db: AsyncSession, project_id: int, baseline_at: datetime) -> set[int]:
    """基线之后有更新的定义；用 updated_at/created_at 兼顾历史空值。"""
    rows = await db.execute(
        select(db_models.ApiDefinition.id).where(
            db_models.ApiDefinition.project_id == project_id,
            func.coalesce(
                db_models.ApiDefinition.updated_at,
                db_models.ApiDefinition.created_at,
            ) >= baseline_at,
        )
    )
    return {int(x) for x in rows.scalars().all()}


async def case_last_runs(db: AsyncSession, project_id: int) -> dict[int, datetime]:
    rows = await db.execute(
        select(
            db_models.TestRun.case_id,
            func.max(func.coalesce(
                db_models.TestRun.start_time,
                db_models.TestRun.end_time,
                db_models.RunBatch.created_at,
            )),
        )
        .join(db_models.RunBatch, db_models.RunBatch.id == db_models.TestRun.batch_id)
        .where(
            db_models.RunBatch.project_id == project_id,
            db_models.TestRun.case_id.is_not(None),
        )
        .group_by(db_models.TestRun.case_id)
    )
    return {int(cid): at for cid, at in rows.all() if cid is not None and at is not None}
