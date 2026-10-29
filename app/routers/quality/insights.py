# app/routers/quality/insights.py
# 041 US3：失败聚类 + Flaky 端点。
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_async_db
from app.routers.quality.common import ensure_quality_enabled, serialize
from app.services.quality_insights import generate_insights

router = APIRouter()


class InsightsRequest(BaseModel):
    project_id: int
    days: int | None = None
    module_id: int | None = None
    include_ai: bool = True
    force: bool = False


@router.post("/insights")
async def insights_endpoint(
    payload: InsightsRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await ensure_quality_enabled(db, user, payload.project_id)
    try:
        row, cached = await generate_insights(
            db, project_id=payload.project_id,
            days=payload.days or 14, module_id=payload.module_id,
            include_ai=payload.include_ai, force=payload.force,
            user_id=getattr(user, "id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = serialize(row)
    data["cached"] = cached
    return data
