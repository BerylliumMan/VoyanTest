# app/routers/quality/analysis.py
# 041 US1：UI/客户端失败归因端点。
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user
from app.crud import api_ai as crud_api_ai
from app.database import get_async_db
from app.routers.quality.common import (
    ensure_quality_enabled,
    get_owned_analysis,
    project_id_for_run,
    serialize,
)
from app.services.api_ai import ApiAiUnavailable
from app.services.quality_analysis import analyze_ui_failure

router = APIRouter()


class AnalyzeFailureRequest(BaseModel):
    run_id: int
    force: bool = False


@router.post("/failure-analysis")
async def analyze_failure_endpoint(
    payload: AnalyzeFailureRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    run = await db.get(db_models.TestRun, payload.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    project_id = await project_id_for_run(db, run)
    if project_id is None:
        raise HTTPException(status_code=404, detail="运行记录未关联项目")
    await ensure_quality_enabled(db, user, project_id)
    if run.status not in ("failed", "error"):
        raise HTTPException(status_code=400, detail="仅失败运行可做 AI 分析")
    try:
        row, cached = await analyze_ui_failure(
            db, run=run, project_id=project_id,
            user_id=getattr(user, "id", None), force=payload.force,
        )
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    data = serialize(row)
    data["cached"] = cached
    return data


@router.get("/analyses/{analysis_id}")
async def get_analysis_endpoint(
    analysis_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    row = await get_owned_analysis(db, analysis_id=analysis_id, user=user)
    return serialize(row)
