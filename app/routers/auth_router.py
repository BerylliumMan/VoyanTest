"""认证路由 — login / logout / me / change-password."""
import os
from datetime import timedelta
from urllib.parse import quote
from app.tz import now as tz_now
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app import db_models, models
from app.database import get_db
from app.config import get_settings
from app.auth import (
    verify_password, hash_password, validate_password_strength,
    create_session, delete_session, get_current_user, log_audit,
)
from app.rate_limiter import limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def _login_user(request: Request, username: str, password: str, db: Session):
    """Core login logic. Returns (User, session_id). Raises HTTPException on failure."""
    username = username.lower().strip()
    user = db.query(db_models.User).filter(db_models.User.username == username).first()
    if not user:
        log_audit(db, None, "login_failed", {"username": username, "reason": "not found"}, client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if user.status == "locked" and user.locked_until and user.locked_until > tz_now():
        m = int((user.locked_until - tz_now()).total_seconds() / 60)
        raise HTTPException(status_code=423, detail=f"账号已锁定，请 {m} 分钟后重试")
    if user.status == "locked" and user.locked_until and user.locked_until <= tz_now():
        user.status = "active"
        user.locked_until = None
        user.login_attempts = 0
    if user.status == "disabled":
        raise HTTPException(status_code=401, detail="账号已被禁用")

    if not verify_password(password, user.password_hash):
        user.login_attempts = (user.login_attempts or 0) + 1
        if user.login_attempts >= settings.max_login_attempts:
            user.status = "locked"
            user.locked_until = tz_now() + timedelta(minutes=settings.lock_duration_minutes)
        db.commit()
        log_audit(db, user.id, "login_failed", {"attempt": user.login_attempts}, client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    user.login_attempts = 0
    user.last_login_at = tz_now()
    db.commit()
    sid = create_session(db, user.id, client_ip(request))
    log_audit(db, user.id, "login", ip_address=client_ip(request))
    return user, sid


@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, body: models.LoginRequest, db: Session = Depends(get_db)):
    """JSON 登录."""
    user, sid = _login_user(request, body.username, body.password, db)
    data = models.LoginResponse(id=user.id, username=user.username, role=user.role, must_change_password=user.must_change_password)
    resp = JSONResponse(content=data.model_dump())
    # COOKIE_SECURE 环境变量控制 cookie 的 Secure 标志：生产环境 HTTPS 下必须设为 true
    _cookie_secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax",
                    max_age=settings.session_expire_minutes * 60, secure=_cookie_secure)
    return resp


@router.post("/login-form")
@limiter.limit("5/minute")
def login_form(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    """HTML 表单登录."""
    try:
        user, sid = _login_user(request, username, password, db)
        resp = RedirectResponse(url="/", status_code=302)
        _cookie_secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
        resp.set_cookie("session_id", sid, httponly=True, samesite="lax",
                        max_age=settings.session_expire_minutes * 60, secure=_cookie_secure)
        return resp
    except HTTPException as e:
        return RedirectResponse(url=f"/login?error={quote(e.detail)}", status_code=302)


@router.post("/logout")
def logout(request: Request, user=Depends(get_current_user), db: Session = Depends(get_db)):
    sid = request.cookies.get("session_id")
    if sid:
        delete_session(db, sid)
    log_audit(db, user.id, "logout", ip_address=client_ip(request))
    return {"message": "已登出"}


@router.get("/me")
def me(user=Depends(get_current_user)):
    return models.LoginResponse(id=user.id, username=user.username, role=user.role, must_change_password=user.must_change_password).model_dump()


@router.post("/change-password")
def change_password(request: Request, body: models.ChangePasswordRequest, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    ok, msg = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    log_audit(db, user.id, "password_changed", ip_address=client_ip(request))
    return {"message": "密码修改成功"}
