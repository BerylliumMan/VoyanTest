# 042 US1：影响分析与回归计划
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.auth import get_current_user, log_audit
from app.database import get_async_db
from app.routers.quality.common import ensure_capability, ensure_project_access
from app.services.quality_impact import analyze

router = APIRouter()


class ImpactRequest(BaseModel):
    project_id: int
    baseline_at: Optional[datetime] = None
    recent_days: Optional[int] = Field(default=None, ge=1, le=365)
    stale_days: Optional[int] = Field(default=None, ge=1, le=3650)
    include_quarantined: bool = False
    manual_case_ids: list[int] = []


class PlanItem(BaseModel):
    case_id: int
    case_kind: str = "ui"
    reasons: list[dict] = []
    note: str = ""


class PlanCreate(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    environment_id: Optional[int] = None
    agent_id: Optional[int] = None
    items: list[PlanItem] = []


class PlanRun(BaseModel):
    include_init: bool = True
    run_mode: str = "server"  # server | client
    init_policy: str = "once"


def _serialize(row: db_models.QualityRegressionPlan) -> dict:
    return {
        "id": row.id, "project_id": row.project_id, "name": row.name,
        "description": row.description, "environment_id": row.environment_id,
        "agent_id": row.agent_id, "items": row.items or [],
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_batch_id": row.last_batch_id,
    }


async def _get_plan(db: AsyncSession, plan_id: int, user) -> db_models.QualityRegressionPlan:
    row = await db.get(db_models.QualityRegressionPlan, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="回归计划不存在")
    ensure_project_access(user, row.project_id)
    return row


@router.post("/impact-analysis")
async def impact_analysis(body: ImpactRequest, user=Depends(get_current_user),
                         db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, body.project_id, "impact")
    return await analyze(
        db, project_id=body.project_id, baseline_at=body.baseline_at,
        recent_days=body.recent_days, stale_days=body.stale_days,
        include_quarantined=body.include_quarantined,
        manual_case_ids=body.manual_case_ids,
    )


@router.get("/regression-plans")
async def list_plans(project_id: int, user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, project_id, "impact")
    rows = await crud_quality().list_regression_plans(db, project_id)
    return {"items": [_serialize(r) for r in rows]}


@router.post("/regression-plans")
async def create_plan(body: PlanCreate, user=Depends(get_current_user),
                      db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, body.project_id, "impact")
    if not body.items:
        raise HTTPException(status_code=400, detail="回归计划不能为空")
    valid = []
    for item in body.items:
        case = await db.get(db_models.TestCase, item.case_id)
        if case is None or case.project_id != body.project_id:
            raise HTTPException(status_code=400, detail=f"用例 {item.case_id} 不存在或不属于当前项目")
        valid.append(item.model_dump())
    row = await crud_quality().create_regression_plan(
        db, project_id=body.project_id, name=body.name, description=body.description,
        environment_id=body.environment_id, agent_id=body.agent_id,
        items=valid, created_by=getattr(user, "id", None),
    )
    await log_audit(db, getattr(user, "id", None), "quality.regression_plan_save",
                    details={"plan_id": row.id, "project_id": row.project_id, "count": len(valid)})
    return _serialize(row)


@router.get("/regression-plans/{plan_id}")
async def get_plan(plan_id: int, user=Depends(get_current_user),
                   db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await _get_plan(db, plan_id, user)
    return _serialize(row)


@router.delete("/regression-plans/{plan_id}")
async def delete_plan(plan_id: int, user=Depends(get_current_user),
                      db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await _get_plan(db, plan_id, user)
    await crud_quality().delete_regression_plan(db, row)
    return {"deleted": plan_id}


@router.post("/regression-plans/{plan_id}/run")
async def run_plan(plan_id: int, body: PlanRun, background_tasks: BackgroundTasks,
                   user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await _get_plan(db, plan_id, user)
    await ensure_capability(db, user, row.project_id, "impact")
    case_ids = []
    invalid = []
    for item in (row.items or []):
        case = await db.get(db_models.TestCase, int(item.get("case_id") or 0))
        if case is None or case.project_id != row.project_id:
            invalid.append(item.get("case_id"))
        else:
            case_ids.append(int(case.id))
    if not case_ids:
        raise HTTPException(status_code=409, detail="计划中的用例均已失效")
    init_ids = []
    if body.include_init:
        res = await db.execute(
            select(db_models.TestCase.id).where(
                db_models.TestCase.project_id == row.project_id,
                db_models.TestCase.is_init.is_(True),
            )
        )
        init_ids = [int(x) for x in res.scalars().all()]
    result = None
    from app.crud.run import set_batch_source_override, reset_batch_source_override
    token = set_batch_source_override("regression_plan", row.id)
    try:
        if body.run_mode == "client":
            from app.routers.testcase.execution._client import batch_run_client
            from app.routers.testcase.execution._schemas import BatchCaseIdsRequest
            agent = await db.get(db_models.Agent, row.agent_id) if row.agent_id else None
            req = BatchCaseIdsRequest(
                case_ids=case_ids, environment_id=row.environment_id,
                agent_name=agent.name if agent else None, init_case_ids=init_ids,
                init_policy="once", source="regression_plan", source_id=row.id,
            )
            result = await batch_run_client(req, user, db)
        else:
            from app.routers.testcase.execution._server import batch_run_cases
            from app.routers.testcase.execution._schemas import BatchRunRequest
            req = BatchRunRequest(
                case_ids=case_ids, environment_id=row.environment_id,
                init_case_ids=init_ids, init_policy="once",
            )
            result = await batch_run_cases(req, BackgroundTasks(), user, db)
    finally:
        reset_batch_source_override(token)
    batch_id = result.get("batch_id") if isinstance(result, dict) else None
    if batch_id:
        await crud_quality().update_regression_plan(db, row, last_batch_id=batch_id)
    await log_audit(db, getattr(user, "id", None), "quality.regression_plan_run",
                    details={"plan_id": row.id, "batch_id": batch_id, "case_count": len(case_ids)})
    return {**(result or {}), "invalid_case_ids": invalid,
            "plan_case_count": len(case_ids), "init_cases": len(init_ids)}


def crud_quality():
    from app.crud import quality
    return quality
