# 050 US5：用例人工评审
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_async_db
from app.services import case_review as review_service

router = APIRouter(prefix="/api")


class CreateReviewRequest(BaseModel):
    case_id: int
    reviewer_id: int
    note: str | None = None


class DecisionRequest(BaseModel):
    status: str
    comment: str | None = Field(default=None, max_length=5000)


@router.post("/case-reviews", status_code=201)
async def create_review(
    payload: CreateReviewRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await review_service.create_review(
        db, user, case_id=payload.case_id, reviewer_id=payload.reviewer_id, note=payload.note
    )


@router.get("/case-reviews")
async def list_reviews(
    status: str | None = None,
    mine_pending: bool = False,
    case_id: int | None = None,
    project_id: int | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    items = await review_service.list_reviews(
        db, user, status=status, mine_pending=mine_pending,
        case_id=case_id, project_id=project_id,
    )
    return {"items": items}


@router.post("/case-reviews/{review_id}/decision")
async def decide_review(
    review_id: int,
    payload: DecisionRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await review_service.decide_review(
        db, user, review_id, status=payload.status, comment=payload.comment
    )


@router.post("/case-reviews/{review_id}/cancel")
async def cancel_review(
    review_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await review_service.cancel_review(db, user, review_id)
