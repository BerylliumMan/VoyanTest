"""认证路由 — login / logout / me / change-password."""
from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import quote
from app.tz import now as tz_now
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app import crud, models
from app.database import get_async_db
from app.config import get_settings
from app.auth import (
    verify_password, hash_password, validate_password_strength,
    create_session, delete_session, get_current_user, log_audit,
)
from app.rate_limiter import limiter
from app.utils import client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


async def _login_user(request: Request, username: str, password: str, db: AsyncSession) -> tuple[models.User, str]:
    """Core login logic. Returns (User, session_id). Raises HTTPException on failure."""
    username = username.lower().strip()
    user = await crud.get_user_by_username(db, username)
    if not user:
        await log_audit(db, None, "login_failed", {"username": username, "reason": "not found"}, client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if user.status == "locked" and user.locked_until and user.locked_until > tz_now():
        m = int((user.locked_until - tz_now()).total_seconds() / 60)
        raise HTTPException(status_code=423, detail=f"账号已锁定，请 {m} 分钟后重试")

    # 自动解锁过期锁定（CRUD 封装：commit + refresh）
    user = await crud.unlock_user_if_expired(db, user) or user

    if user.status == "disabled":
        raise HTTPException(status_code=401, detail="账号已被禁用")

    if not verify_password(password, user.password_hash):
        new_attempts = (user.login_attempts or 0) + 1
        update_kwargs: dict = {"login_attempts": new_attempts}
        if new_attempts >= settings.max_login_attempts:
            update_kwargs["status"] = "locked"
            update_kwargs["locked_until"] = tz_now() + timedelta(minutes=settings.lock_duration_minutes)
        await crud.update_user_fields(db, user.id, **update_kwargs)
        await log_audit(db, user.id, "login_failed", {"attempt": new_attempts}, client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 成功登录：重置 attempts + 记录 last_login_at
    await crud.update_user_fields(
        db, user.id,
        login_attempts=0,
        last_login_at=tz_now(),
    )
    # 重新读取以拿到最新字段（包含上面 update 后的 login_attempts=0）
    user = await crud.get_user_by_id(db, user.id)
    sid = await create_session(db, user.id, client_ip(request))
    await log_audit(db, user.id, "login", ip_address=client_ip(request))
    return user, sid


@router.post("/login")
@limiter.limit("30/minute")
async def login(request: Request, body: models.LoginRequest, db: AsyncSession = Depends(get_async_db)) -> JSONResponse:
    """JSON 登录."""
    user, sid = await _login_user(request, body.username, body.password, db)
    data = models.LoginResponse(id=user.id, username=user.username, role=user.role, must_change_password=user.must_change_password)
    resp = JSONResponse(content=data.model_dump())
    # COOKIE_SECURE 环境变量控制 cookie 的 Secure 标志：生产环境 HTTPS 下必须设为 true
    _cookie_secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax",
                    max_age=settings.session_expire_minutes * 60, secure=_cookie_secure)
    return resp


@router.post("/login-form")
@limiter.limit("30/minute")
async def login_form(request: Request, username: str = Form(...), password: str = Form(...), db: AsyncSession = Depends(get_async_db)) -> RedirectResponse:
    """HTML 表单登录."""
    try:
        user, sid = await _login_user(request, username, password, db)
        resp = RedirectResponse(url="/", status_code=302)
        _cookie_secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
        resp.set_cookie("session_id", sid, httponly=True, samesite="lax",
                        max_age=settings.session_expire_minutes * 60, secure=_cookie_secure)
        return resp
    except HTTPException as e:
        return RedirectResponse(url=f"/login?error={quote(e.detail)}", status_code=302)


@router.post("/logout")
async def logout(request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    sid = request.cookies.get("session_id")
    if sid:
        await delete_session(db, sid)
    await log_audit(db, user.id, "logout", ip_address=client_ip(request))
    return {"message": "已登出"}


@router.get("/me")
async def me(user=Depends(get_current_user)) -> dict:
    return models.LoginResponse(id=user.id, username=user.username, role=user.role, must_change_password=user.must_change_password).model_dump()


@router.post("/change-password")
async def change_password(request: Request, body: models.ChangePasswordRequest, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    ok, msg = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    await crud.update_user_fields(
        db, user.id,
        password_hash=hash_password(body.new_password),
        must_change_password=False,
    )
    await log_audit(db, user.id, "password_changed", ip_address=client_ip(request))
    return {"message": "密码修改成功"}
