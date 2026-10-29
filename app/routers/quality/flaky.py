# 042 US2：Flaky 治理端点
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.auth import get_current_user, log_audit
from app.database import get_async_db
from app.routers.quality.common import ensure_capability
from app.services import quality_flaky

router = APIRouter()


class ConfirmRequest(BaseModel):
    environment_id: Optional[int] = None


class ReasonRequest(BaseModel):
    reason: Optional[str] = None


@router.get("/flaky")
async def list_flaky(project_id: int, status: Optional[str] = None,
                     user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, project_id, "flaky")
    await quality_flaky.refresh_candidates(db, project_id)
    rows = await quality_flaky.crud_quality_list(db, project_id)
    if status:
        rows = [r for r in rows if r.status == status]
    return {"items": [{
        "case_id": r.case_id, "status": r.status, "sample_runs": r.sample_runs,
        "passed": r.passed, "failed": r.failed,
        "pass_rate": round((r.passed / (r.passed + r.failed)) * 100, 1)
        if (r.passed + r.failed) else None,
        "consecutive_passes": r.consecutive_passes,
        "last_failed_at": r.last_failed_at.isoformat() if r.last_failed_at else None,
        "reason": r.reason, "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]}


@router.post("/flaky/{case_id}/confirm")
async def confirm(case_id: int, body: ConfirmRequest,
                  user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    from app import db_models
    case = await db.get(db_models.TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    await ensure_capability(db, user, case.project_id, "flaky")
    try:
        result = await quality_flaky.confirm(
            db, project_id=case.project_id, case_id=case_id,
            environment_id=body.environment_id, user_id=getattr(user, "id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await log_audit(db, getattr(user, "id", None), "quality.flaky_confirm",
                    details={"case_id": case_id, "status": result.get("status")})
    return result


@router.post("/flaky/{case_id}/quarantine")
async def quarantine(case_id: int, body: ReasonRequest,
                     user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    from app import db_models
    case = await db.get(db_models.TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    await ensure_capability(db, user, case.project_id, "flaky")
    row = await quality_flaky.quarantine(
        db, project_id=case.project_id, case_id=case_id,
        user_id=getattr(user, "id", None), reason=body.reason,
    )
    await log_audit(db, getattr(user, "id", None), "quality.flaky_quarantine",
                    details={"case_id": case_id, "status": row.status})
    return {"case_id": case_id, "status": row.status}


@router.post("/flaky/{case_id}/release")
async def release(case_id: int, body: ReasonRequest,
                  user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    from app import db_models
    case = await db.get(db_models.TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    await ensure_capability(db, user, case.project_id, "flaky")
    row = await quality_flaky.release(
        db, project_id=case.project_id, case_id=case_id,
        user_id=getattr(user, "id", None),
    )
    await log_audit(db, getattr(user, "id", None), "quality.flaky_release",
                    details={"case_id": case_id, "status": row.status})
    return {"case_id": case_id, "status": row.status}
