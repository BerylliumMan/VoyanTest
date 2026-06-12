"""认证模块 — 密码哈希、Session 管理、FastAPI 依赖注入."""
import uuid
import json
from datetime import timedelta
from app.tz import now as tz_now
from passlib.hash import bcrypt
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings

settings = get_settings()


# ==================== 密码工具 (T008) ====================

def hash_password(password: str) -> str:
    return bcrypt.hash(password)


def verify_password(password: str, hash_value: str) -> bool:
    return bcrypt.verify(password, hash_value)


def validate_password_strength(password: str) -> tuple[bool, str]:
    """T045: 密码强度校验 — >=8位, 含字母、数字和特殊字符"""
    if len(password) < 8:
        return False, "密码至少 8 位"
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    special_chars = set("!@#$%^&*()_+-=[]{}|;':\",./<>?~")
    has_special = any(c in special_chars for c in password)
    if not has_letter or not has_digit or not has_special:
        return False, "密码需包含字母、数字和特殊字符"
    return True, ""


# ==================== Session 管理 (T009) ====================

def create_session(db: DbSession, user_id: int, ip_address: str = None) -> str:
    """创建会话，返回 session_id。T009"""
    from app import db_models
    session_id = uuid.uuid4().hex
    now = tz_now()
    session = db_models.Session(
        id=session_id,
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.session_expire_minutes),
        last_activity=now,
    )
    db.add(session)
    db.commit()
    return session_id


def get_session(db: DbSession, session_id: str):
    """获取有效会话，过期返回 None。"""
    from app import db_models
    now = tz_now()
    session = db.query(db_models.Session).filter(
        db_models.Session.id == session_id,
        db_models.Session.expires_at > now,
    ).first()
    if session:
        # 续期
        session.last_activity = now
        session.expires_at = now + timedelta(minutes=settings.session_expire_minutes)
        db.commit()
    return session


def delete_session(db: DbSession, session_id: str):
    """删除会话（登出）。"""
    from app import db_models
    db.query(db_models.Session).filter(
        db_models.Session.id == session_id
    ).delete()
    db.commit()


def cleanup_expired_sessions(db: DbSession):
    """清理过期会话。"""
    from app import db_models
    db.query(db_models.Session).filter(
        db_models.Session.expires_at <= tz_now()
    ).delete()
    db.commit()


# ==================== 审计日志 (T051) ====================

def log_audit(db: DbSession, user_id: int | None, action: str, details: dict = None, ip_address: str = None):
    """写入审计日志。"""
    from app import db_models
    log = db_models.AuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps(details) if details else None,
        ip_address=ip_address,
    )
    db.add(log)
    db.commit()


# ==================== FastAPI 依赖注入 (T010) ====================

from fastapi import Request, HTTPException, Depends
from app.database import get_db


def get_session_id_from_cookie(request: Request) -> str | None:
    return request.cookies.get("session_id")


def get_current_user(request: Request, db: DbSession = Depends(get_db)):
    """验证登录状态，返回 User 或 raise 401。T010"""
    session_id = get_session_id_from_cookie(request)
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    session = get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=401, detail="会话已过期，请重新登录")
    from app import db_models
    user = db.query(db_models.User).filter(db_models.User.id == session.user_id).first()
    if not user or user.status == "disabled":
        raise HTTPException(status_code=401, detail="账号已被禁用")
    return user


def require_admin(user=Depends(get_current_user)):
    """验证管理员角色，否则 raise 403。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
