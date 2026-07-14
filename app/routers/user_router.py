"""用户管理路由 — 管理员 CRUD 用户."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app import crud, models
from app.database import get_async_db
from app.auth import hash_password, validate_password_strength, require_admin, log_audit
from app.utils import client_ip
from fastapi import Request

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/", response_model=list[models.UserResponse])
async def list_users(db: AsyncSession = Depends(get_async_db), admin=Depends(require_admin)) -> list[models.UserResponse]:
    return await crud.list_users(db)


@router.post("/", response_model=models.UserResponse)
async def create_user(request: Request, body: models.UserCreate, db: AsyncSession = Depends(get_async_db), admin=Depends(require_admin)) -> models.UserResponse:
    username = body.username.lower().strip()
    if await crud.get_user_by_username(db, username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    ok, msg = validate_password_strength(body.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if body.role not in ("admin", "tester"):
        raise HTTPException(status_code=400, detail="角色必须是 admin 或 tester")
    user = await crud.create_user(
        db,
        username=username,
        password_hash=hash_password(body.password),
        role=body.role,
        must_change_password=True,
        project_ids=body.project_ids,
    )
    await log_audit(db, admin.id, "user_created", {"target": username}, client_ip(request))
    return user


@router.put("/{user_id}", response_model=models.UserResponse)
async def update_user(request: Request, user_id: int, body: models.UserUpdate, db: AsyncSession = Depends(get_async_db), admin=Depends(require_admin)) -> models.UserResponse:
    user = await crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id and body.status == "disabled":
        raise HTTPException(status_code=400, detail="不能禁用自己")
    if body.role is not None:
        if body.role not in ("admin", "tester"):
            raise HTTPException(status_code=400, detail="角色必须是 admin 或 tester")
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="状态必须是 active 或 disabled")
    user = await crud.update_user_fields(
        db,
        user_id,
        role=body.role,
        status=body.status,
        project_ids=list(body.project_ids) if body.project_ids is not None else None,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await log_audit(db, admin.id, "user_updated", {"target": user.username, "changes": body.model_dump(exclude_none=True)}, client_ip(request))
    return user


@router.put("/{user_id}/reset-password")
async def reset_password(request: Request, user_id: int, body: models.ResetPasswordRequest, db: AsyncSession = Depends(get_async_db), admin=Depends(require_admin)) -> dict:
    user = await crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    ok, msg = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    user = await crud.update_user_fields(
        db,
        user_id,
        password_hash=hash_password(body.new_password),
        must_change_password=True,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await log_audit(db, admin.id, "password_reset", {"target": user.username}, client_ip(request))
    return {"message": "密码已重置"}
