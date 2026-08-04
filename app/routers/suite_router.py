"""测试用例集 API：CRUD + 按序执行（init_policy=once）。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models
from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db
from app.routers.testcase.execution._schemas import BatchCaseIdsRequest, BatchRunRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/suites", tags=["用例集"])


async def _get_authorized_suite(db: AsyncSession, suite_id: int, user):
    suite = await crud.get_suite(db, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="Suite not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and suite.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Suite not found")
    return suite


async def _suite_response(db: AsyncSession, suite) -> dict:
    payload = crud.suite_to_dict(suite)
    return await crud.enrich_suite_cases(db, payload)


@router.get("")
async def list_suites(
    project_id: int = Query(...),
    case_kind: Optional[str] = Query(None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> list:
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    project = await crud.get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    suites = await crud.list_suites(db, project_id, case_kind=case_kind)
    return [await _suite_response(db, s) for s in suites]


@router.post("")
async def create_suite(
    body: models.TestSuiteCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and body.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    project = await crud.get_project(db, body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        suite = await crud.create_suite(
            db,
            body.project_id,
            body.name,
            list(body.case_ids or []),
            description=body.description,
            case_kind=body.case_kind or "ui",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _suite_response(db, suite)


@router.get("/{suite_id}")
async def get_suite(
    suite_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    suite = await _get_authorized_suite(db, suite_id, user)
    return await _suite_response(db, suite)


@router.put("/{suite_id}")
async def update_suite(
    suite_id: int,
    body: models.TestSuiteUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await _get_authorized_suite(db, suite_id, user)
    try:
        suite = await crud.update_suite(
            db,
            suite_id,
            name=body.name,
            description=body.description,
            case_ids=body.case_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if suite is None:
        raise HTTPException(status_code=404, detail="Suite not found")
    return await _suite_response(db, suite)


@router.delete("/{suite_id}")
async def delete_suite(
    suite_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await _get_authorized_suite(db, suite_id, user)
    ok = await crud.delete_suite(db, suite_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Suite not found")
    return {"ok": True}


@router.post("/{suite_id}/run")
async def run_suite(
    suite_id: int,
    body: models.SuiteRunRequest,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """服务端按序执行用例集；强制 init_policy=once。"""
    suite = await _get_authorized_suite(db, suite_id, user)
    case_ids = await crud.get_suite_ordered_case_ids(db, suite_id)
    if not case_ids:
        raise HTTPException(status_code=400, detail="Empty suite cannot run")

    from app.routers.testcase.execution._server import batch_run_cases

    req = BatchRunRequest(
        case_ids=case_ids,
        environment_id=body.environment_id,
        init_case_ids=list(body.init_case_ids or []),
        init_policy="once",
    )
    result = await batch_run_cases(req, background_tasks, user, db)
    # 轻量关联：写回批次名（若 batch_id 可用）
    batch_id = result.get("batch_id")
    if batch_id:
        try:
            await crud.update_run_batch(
                db, batch_id, name=f"suite:{suite.id}:{suite.name}",
            )
        except Exception:
            logger.warning("Failed to tag batch %s with suite info", batch_id, exc_info=True)
    return result


@router.post("/{suite_id}/run-client")
async def run_suite_client(
    suite_id: int,
    body: models.SuiteRunRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """客户端 Agent 按序执行用例集；强制 init_policy=once。"""
    suite = await _get_authorized_suite(db, suite_id, user)
    case_ids = await crud.get_suite_ordered_case_ids(db, suite_id)
    if not case_ids:
        raise HTTPException(status_code=400, detail="Empty suite cannot run")

    from app.routers.testcase.execution._client import batch_run_client

    req = BatchCaseIdsRequest(
        case_ids=case_ids,
        agent_name=body.agent_name,
        init_case_ids=list(body.init_case_ids or []),
        environment_id=body.environment_id,
        backend=body.backend,
        init_policy="once",
    )
    result = await batch_run_client(req, user, db)
    batch_id = result.get("batch_id")
    if batch_id:
        try:
            await crud.update_run_batch(
                db, batch_id, name=f"suite:{suite.id}:{suite.name}",
            )
        except Exception:
            logger.warning("Failed to tag batch %s with suite info", batch_id, exc_info=True)
    return result
