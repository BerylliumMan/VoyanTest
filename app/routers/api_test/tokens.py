# app/routers/api_test/tokens.py — CI 触发令牌管理（031 US8）
#
# 契约（contracts §1.6）：
#   POST   /api/api-test/tokens        签发（**明文仅此响应返回一次**）→ {id, name, token_prefix, plaintext, expires_at}
#   GET    /api/api-test/tokens        列表（不含哈希与明文；管理员看全部，普通用户看自己的）
#   DELETE /api/api-test/tokens/{id}   撤销（幂等；重复撤销仍 204）
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, require_admin
from app.crud import api_token as crud_token
from app.database import get_async_db

router = APIRouter()


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    project_id: Optional[int] = None
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


@router.post("/tokens", status_code=201)
async def create_token(
    payload: TokenCreateRequest,
    user: db_models.User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_async_db),  # noqa: B008
) -> dict[str, Any]:
    """签发 CI 令牌（明文只返回一次）。"""
    token, plaintext = await crud_token.create_token(
        db,
        name=payload.name,
        project_id=payload.project_id,
        created_by=user.id,
        expires_in_days=payload.expires_in_days,
    )
    data = crud_token.to_public_dict(token)
    data["plaintext"] = plaintext  # 唯一一次返回
    return data


@router.get("/tokens")
async def list_tokens(
    request: Request,
    user: db_models.User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_async_db),  # noqa: B008
) -> dict[str, Any]:
    """令牌列表（管理员看全部，普通用户看自己签发的）。"""
    created_by = None if getattr(user, "role", None) == "admin" else user.id
    rows = await crud_token.list_tokens(db, created_by=created_by)
    return {"items": [crud_token.to_public_dict(r) for r in rows]}


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: int,
    user: db_models.User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_async_db),  # noqa: B008
) -> None:
    """撤销令牌；不存在 → 404（幂等撤销仍 204）。"""
    row = await db.get(db_models.ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="令牌不存在")
    if getattr(user, "role", None) != "admin" and row.created_by != user.id:
        raise HTTPException(status_code=403, detail="只能撤销自己签发的令牌")
    await crud_token.revoke_token(db, token_id)
