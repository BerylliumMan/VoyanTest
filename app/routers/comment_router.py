# 050 US4：用例评论
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_async_db
from app.services import comments as comment_service

router = APIRouter(prefix="/api")


class CreateCommentRequest(BaseModel):
    entity_type: str = "case"
    entity_id: int
    body: str = Field(min_length=1, max_length=5000)
    mentions: list[int] | None = None


class UpdateCommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


@router.get("/comments")
async def list_comments(
    entity_type: str = "case",
    entity_id: int = ...,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    items = await comment_service.list_comments(
        db, user, entity_type=entity_type, entity_id=entity_id
    )
    return {"items": items}


@router.post("/comments", status_code=201)
async def create_comment(
    payload: CreateCommentRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await comment_service.create_comment(
        db, user,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        body=payload.body,
        mentions=payload.mentions,
    )


@router.patch("/comments/{comment_id}")
async def update_comment(
    comment_id: int,
    payload: UpdateCommentRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await comment_service.update_comment(db, user, comment_id, payload.body)


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await comment_service.delete_comment(db, user, comment_id)
