# 042 US1：影响分析与回归计划
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.auth import get_current_user, log_audit
from app.database import get_async_db
from app.routers.quality.common import ensure_capability, ensure_project_access
from app.services.quality_impact import analyze
from app.tz import now as tz_now

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


class PlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    environment_id: Optional[int] = None
    agent_id: Optional[int] = None
    items: Optional[list[PlanItem]] = None


class PlanRun(BaseModel):
    include_init: bool = True
    run_mode: str = "server"  # server | client
    init_policy: str = "once"
    # 执行时临时覆盖计划里保存的环境/客户端（不传则用计划配置）
    environment_id: Optional[int] = None
    agent_id: Optional[int] = None


def _serialize(row: db_models.QualityRegressionPlan, extras: dict | None = None) -> dict:
    extras = extras or {}
    return {
        "id": row.id, "project_id": row.project_id, "name": row.name,
        "description": row.description, "environment_id": row.environment_id,
        "agent_id": row.agent_id, "items": row.items or [],
        "environment_name": extras.get("environment_name"),
        "agent_name": extras.get("agent_name"),
        "last_batch_status": extras.get("last_batch_status"),
        "last_batch_time": extras.get("last_batch_time"),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_batch_id": row.last_batch_id,
    }


async def _plan_extras(db: AsyncSession, rows: list[db_models.QualityRegressionPlan]) -> dict[int, dict]:
    """批量补齐环境名、客户端名、最近批次状态，避免列表逐行查询。"""
    env_ids = {int(r.environment_id) for r in rows if r.environment_id}
    agent_ids = {int(r.agent_id) for r in rows if r.agent_id}
    batch_ids = {int(r.last_batch_id) for r in rows if r.last_batch_id}
    envs: dict[int, str] = {}
    agents: dict[int, str] = {}
    batches: dict[int, db_models.RunBatch] = {}
    if env_ids:
        res = await db.execute(
            select(db_models.Environment.id, db_models.Environment.name)
            .where(db_models.Environment.id.in_(env_ids))
        )
        envs = {int(i): n for i, n in res.all()}
    if agent_ids:
        res = await db.execute(
            select(db_models.Agent.id, db_models.Agent.name)
            .where(db_models.Agent.id.in_(agent_ids))
        )
        agents = {int(i): n for i, n in res.all()}
    if batch_ids:
        res = await db.execute(
            select(db_models.RunBatch).where(db_models.RunBatch.id.in_(batch_ids))
        )
        batches = {int(b.id): b for b in res.scalars().all()}
    out: dict[int, dict] = {}
    for r in rows:
        batch = batches.get(int(r.last_batch_id)) if r.last_batch_id else None
        out[int(r.id)] = {
            "environment_name": envs.get(int(r.environment_id)) if r.environment_id else None,
            "agent_name": agents.get(int(r.agent_id)) if r.agent_id else None,
            "last_batch_status": batch.status if batch else None,
            "last_batch_time": batch.created_at.isoformat() if batch and batch.created_at else None,
        }
    return out


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
    extras = await _plan_extras(db, rows)
    return {"items": [_serialize(r, extras.get(int(r.id))) for r in rows]}


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
    extras = await _plan_extras(db, [row])
    return _serialize(row, extras.get(int(row.id)))


@router.patch("/regression-plans/{plan_id}")
async def update_plan(plan_id: int, body: PlanUpdate, user=Depends(get_current_user),
                      db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await _get_plan(db, plan_id, user)
    await ensure_capability(db, user, row.project_id, "impact")
    payload = body.model_dump(exclude_unset=True)
    if "items" in payload:
        items = payload["items"] or []
        if not items:
            raise HTTPException(status_code=400, detail="回归计划不能为空")
        valid = []
        for item in items:
            case = await db.get(db_models.TestCase, int(item.get("case_id") or 0))
            if case is None or case.project_id != row.project_id:
                raise HTTPException(status_code=400, detail=f"用例 {item.get('case_id')} 不存在或不属于当前项目")
            valid.append(item)
        payload["items"] = valid
    for key, value in payload.items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    await log_audit(db, getattr(user, "id", None), "quality.regression_plan_update",
                    details={"plan_id": row.id, "fields": sorted(payload.keys())})
    extras = await _plan_extras(db, [row])
    return _serialize(row, extras.get(int(row.id)))


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
    # 执行弹窗可临时覆盖计划里保存的环境与客户端
    env_id = body.environment_id if body.environment_id is not None else row.environment_id
    agent_id = body.agent_id if body.agent_id is not None else row.agent_id
    run_mode = "client" if (body.run_mode == "client" or agent_id) else "server"
    from app.crud.run import set_batch_source_override, reset_batch_source_override
    token = set_batch_source_override("regression_plan", row.id)
    try:
        if run_mode == "client":
            from app.routers.testcase.execution._client import batch_run_client
            from app.routers.testcase.execution._schemas import BatchCaseIdsRequest
            agent = await db.get(db_models.Agent, agent_id) if agent_id else None
            req = BatchCaseIdsRequest(
                case_ids=case_ids, environment_id=env_id,
                agent_name=agent.name if agent else None, init_case_ids=init_ids,
                init_policy="once", source="regression_plan", source_id=row.id,
            )
            result = await batch_run_client(req, user, db)
        else:
            from app.routers.testcase.execution._server import batch_run_cases
            from app.routers.testcase.execution._schemas import BatchRunRequest
            req = BatchRunRequest(
                case_ids=case_ids, environment_id=env_id,
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


@router.get("/overview")
async def quality_overview(project_id: int, user=Depends(get_current_user),
                           db: AsyncSession = Depends(get_async_db)) -> dict:
    """质量概览：近 14 天执行、Flaky、需求覆盖与无来源用例。"""
    await ensure_capability(db, user, project_id, "impact")
    since = tz_now() - timedelta(days=14)

    total_cases = (await db.execute(
        select(func.count()).select_from(db_models.TestCase)
        .where(db_models.TestCase.project_id == project_id)
    )).scalar_one()

    run_rows = (await db.execute(
        select(db_models.TestRun.status, func.count())
        .join(db_models.RunBatch, db_models.RunBatch.id == db_models.TestRun.batch_id)
        .where(
            db_models.RunBatch.project_id == project_id,
            func.coalesce(
                db_models.TestRun.start_time, db_models.TestRun.end_time,
                db_models.RunBatch.created_at,
            ) >= since,
        )
        .group_by(db_models.TestRun.status)
    )).all()
    passed = sum(int(n) for s, n in run_rows if s in ("passed", "success"))
    failed = sum(int(n) for s, n in run_rows if s in ("failed", "error"))
    total_runs = sum(int(n) for _s, n in run_rows)

    flaky_rows = (await db.execute(
        select(db_models.QualityFlakyState.status, func.count())
        .where(db_models.QualityFlakyState.project_id == project_id)
        .group_by(db_models.QualityFlakyState.status)
    )).all()
    flaky_counts = {s: int(n) for s, n in flaky_rows}

    req_total = (await db.execute(
        select(func.count()).select_from(db_models.QualityRequirement)
        .where(db_models.QualityRequirement.project_id == project_id)
    )).scalar_one()
    linked_req = (await db.execute(
        select(func.count(func.distinct(db_models.QualityRequirementLink.requirement_id)))
        .join(db_models.QualityRequirement,
              db_models.QualityRequirement.id == db_models.QualityRequirementLink.requirement_id)
        .where(db_models.QualityRequirement.project_id == project_id)
    )).scalar_one()
    linked_cases = (await db.execute(
        select(func.count(func.distinct(db_models.QualityRequirementLink.case_id)))
        .join(db_models.QualityRequirement,
              db_models.QualityRequirement.id == db_models.QualityRequirementLink.requirement_id)
        .where(db_models.QualityRequirement.project_id == project_id)
    )).scalar_one()

    return {
        "total_cases": int(total_cases),
        "runs_14d": int(total_runs),
        "passed_14d": passed,
        "failed_14d": failed,
        "pass_rate_14d": round(passed / total_runs * 100, 1) if total_runs else None,
        "flaky_candidates": flaky_counts.get("candidate", 0) + flaky_counts.get("confirming", 0),
        "flaky_quarantined": flaky_counts.get("quarantined", 0),
        "requirements": int(req_total),
        "requirements_uncovered": max(0, int(req_total) - int(linked_req)),
        "unlinked_cases": max(0, int(total_cases) - int(linked_cases)),
    }


def crud_quality():
    from app.crud import quality
    return quality
