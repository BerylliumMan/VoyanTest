"""接口测试首页统计。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db
from app.tz import now as tz_now

router = APIRouter()


def _ensure_project_access(user, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


@router.get("/overview")
async def api_test_overview(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """接口数、用例数、场景数、覆盖率和近 7 天场景通过率。"""
    _ensure_project_access(user, project_id)
    definition_count = int(
        (
            await db.execute(
                select(func.count()).select_from(db_models.ApiDefinition).where(
                    db_models.ApiDefinition.project_id == project_id
                )
            )
        ).scalar()
        or 0
    )
    case_count = int(
        (
            await db.execute(
                select(func.count()).select_from(db_models.TestCase).where(
                    db_models.TestCase.project_id == project_id,
                    db_models.TestCase.case_kind == "api",
                )
            )
        ).scalar()
        or 0
    )
    scenario_count = int(
        (
            await db.execute(
                select(func.count()).select_from(db_models.ApiScenario).where(
                    db_models.ApiScenario.project_id == project_id
                )
            )
        ).scalar()
        or 0
    )
    covered_ids = set(
        (
            await db.execute(
                select(db_models.TestCase.api_definition_id).where(
                    db_models.TestCase.project_id == project_id,
                    db_models.TestCase.case_kind == "api",
                    db_models.TestCase.api_definition_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    covered = len(covered_ids)
    since = tz_now() - timedelta(days=7)
    batches = (
        await db.execute(
            select(db_models.RunBatch).where(
                db_models.RunBatch.project_id == project_id,
                db_models.RunBatch.source == "api_scenario",
                db_models.RunBatch.created_at >= since,
            )
        )
    ).scalars().all()
    passed = sum(int(b.passed or 0) for b in batches)
    failed = sum(int(b.failed or 0) for b in batches)
    finished = passed + failed
    return {
        "definition_count": definition_count,
        "case_count": case_count,
        "scenario_count": scenario_count,
        "covered_definitions": covered,
        "uncovered_definitions": max(0, definition_count - covered),
        "coverage": (covered / definition_count) if definition_count else None,
        "scenario_runs_7d": len(batches),
        "scenario_passed_7d": passed,
        "scenario_failed_7d": failed,
        "scenario_pass_rate_7d": (passed / finished) if finished else None,
    }
