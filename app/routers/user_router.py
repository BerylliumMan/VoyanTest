"""用户管理路由 — 管理员 CRUD 用户."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app import db_models, models
from app.database import get_db
from app.auth import hash_password, validate_password_strength, require_admin, log_audit
from app.utils import client_ip
from fastapi import Request

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/", response_model=list[models.UserResponse])
def list_users(db: Session = Depends(get_db), admin=Depends(require_admin)) -> list[models.UserResponse]:
    return db.query(db_models.User).order_by(db_models.User.created_at.desc()).all()


@router.post("/", response_model=models.UserResponse)
def create_user(request: Request, body: models.UserCreate, db: Session = Depends(get_db), admin=Depends(require_admin)) -> models.UserResponse:
    username = body.username.lower().strip()
    existing = db.query(db_models.User).filter(db_models.User.username == username).first()
    if existing:
        raise HTTPException(status_code=409, detail="用户名已存在")
    ok, msg = validate_password_strength(body.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if body.role not in ("admin", "tester"):
        raise HTTPException(status_code=400, detail="角色必须是 admin 或 tester")
    user = db_models.User(
        username=username, password_hash=hash_password(body.password),
        role=body.role, must_change_password=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_audit(db, admin.id, "user_created", {"target": username}, client_ip(request))
    return user


@router.put("/{user_id}", response_model=models.UserResponse)
def update_user(request: Request, user_id: int, body: models.UserUpdate, db: Session = Depends(get_db), admin=Depends(require_admin)) -> models.UserResponse:
    user = db.query(db_models.User).filter(db_models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id and body.status == "disabled":
        raise HTTPException(status_code=400, detail="不能禁用自己")
    if body.role is not None:
        if body.role not in ("admin", "tester"):
            raise HTTPException(status_code=400, detail="角色必须是 admin 或 tester")
        user.role = body.role
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="状态必须是 active 或 disabled")
        user.status = body.status
    db.commit()
    db.refresh(user)
    log_audit(db, admin.id, "user_updated", {"target": user.username, "changes": body.model_dump(exclude_none=True)}, client_ip(request))
    return user


@router.put("/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, body: models.ResetPasswordRequest, db: Session = Depends(get_db), admin=Depends(require_admin)) -> dict:
    user = db.query(db_models.User).filter(db_models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    ok, msg = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = True
    db.commit()
    log_audit(db, admin.id, "password_reset", {"target": user.username}, client_ip(request))
    return {"message": "密码已重置"}
