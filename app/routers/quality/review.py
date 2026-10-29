# app/routers/quality/review.py
# 041 US2：用例评审端点（单条同步、批量 202 + 任务、应用建议）。
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user
from app.database import get_async_db
from app.routers.quality.common import (
    ensure_quality_enabled,
    get_owned_analysis,
    serialize,
)
from app.services.api_ai import ApiAiUnavailable
from app.services.quality_review import (
    ReviewNotFound,
    ReviewStale,
    apply_review_suggestion,
    review_cases,
)

router = APIRouter()


class CaseReviewRequest(BaseModel):
    project_id: int
    target_type: str = Field("draft", pattern="^(draft|case)$")
    target_ids: list[int] = Field(..., min_length=1)
    use_llm: bool = True
    force: bool = False


class ApplyReviewRequest(BaseModel):
    target_type: str = Field(..., pattern="^(draft|case)$")
    target_id: int
    base_hash: str = ""
    name: str | None = None
    steps: list[Any] | None = None
    test_steps: str | None = None
    api_spec: dict | None = None


@router.post("/case-reviews")
async def create_case_review(
    payload: CaseReviewRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await ensure_quality_enabled(db, user, payload.project_id)
    from app.config import get_settings

    if len(payload.target_ids) > get_settings().quality_review_batch_max:
        raise HTTPException(status_code=400, detail=f"单次最多 {get_settings().quality_review_batch_max} 条")
    if len(payload.target_ids) == 1:
        try:
            row, cached = await review_cases(
                db, project_id=payload.project_id, target_type=payload.target_type,
                target_ids=payload.target_ids, use_llm=payload.use_llm,
                force=payload.force, user_id=getattr(user, "id", None),
            )
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApiAiUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        data = serialize(row)
        data["cached"] = cached
        return data

    from app.services.quality_tasks import start_review_task

    try:
        task_id = start_review_task(
            project_id=payload.project_id,
            target_type=payload.target_type,
            target_ids=payload.target_ids,
            use_llm=payload.use_llm,
            force=payload.force,
            user_id=getattr(user, "id", None),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content={"task_id": task_id, "status": "queued",
                                                  "total": len(payload.target_ids)})


@router.get("/tasks/{task_id}")
async def get_review_task(
    task_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    from app.services import quality_tasks as qt
    from app.routers.quality.common import ensure_project_access

    state = qt._TASKS.get(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_project_access(user, state.get("project_id") or 0)
    return qt.get_task(task_id)


@router.post("/tasks/{task_id}/stop")
async def stop_review_task(
    task_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    from app.services import quality_tasks as qt
    from app.routers.quality.common import ensure_project_access

    state = qt._TASKS.get(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_project_access(user, state.get("project_id") or 0)
    if not qt.stop_task(task_id):
        raise HTTPException(status_code=400, detail="任务已结束")
    return {"task_id": task_id, "status": "stopped"}


@router.post("/case-reviews/{analysis_id}/apply")
async def apply_case_review(
    analysis_id: int,
    payload: ApplyReviewRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    analysis = await get_owned_analysis(db, analysis_id=analysis_id, user=user)
    if analysis.kind != "review":
        raise HTTPException(status_code=400, detail="该分析不是用例评审")
    await ensure_quality_enabled(db, user, analysis.project_id)
    try:
        return await apply_review_suggestion(
            db, analysis=analysis,
            target_type=payload.target_type, target_id=payload.target_id,
            base_hash=payload.base_hash,
            fields={
                "name": payload.name,
                "steps": payload.steps,
                "test_steps": payload.test_steps,
                "api_spec": payload.api_spec,
            },
            user_id=getattr(user, "id", None),
        )
    except ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewStale as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
