"""认证模块 — 密码哈希、Session 管理、FastAPI 依赖注入."""
import base64
import hashlib
import hmac
import uuid
import json
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from passlib.hash import bcrypt

from app.tz import now as tz_now
from app.config import get_settings

settings = get_settings()

_SESSION_SIG_SEP = "."


def _session_signing_key() -> bytes:
    raw = settings.session_secret_key
    if raw:
        return raw.encode()
    return b"voyantest-dev-fallback-secret-key!!"


def _sign_session(session_id: str, user_id: int) -> str:
    msg = f"{user_id}{_SESSION_SIG_SEP}{session_id}".encode()
    digest = hmac.new(_session_signing_key(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:22]


def _verify_session(session_id: str, user_id: int, signature: str) -> bool:
    expected = _sign_session(session_id, user_id)
    return hmac.compare_digest(expected, signature)


# ==================== 密码工具 ====================

def hash_password(password: str) -> str:
    return bcrypt.hash(password)


def verify_password(password: str, hash_value: str) -> bool:
    return bcrypt.verify(password, hash_value)


def validate_password_strength(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "密码至少 8 位"
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    special_chars = set("!@#$%^&*()_+-=[]{}|;':\",./<>?~")
    has_special = any(c in special_chars for c in password)
    if not has_letter or not has_digit or not has_special:
        return False, "密码需包含字母、数字和特殊字符"
    return True, ""


# ==================== Session 管理 (async) ====================

def _parse_session_cookie(raw: str) -> tuple[str, str | None]:
    if _SESSION_SIG_SEP in raw:
        sid, sig = raw.rsplit(_SESSION_SIG_SEP, 1)
        return sid, sig
    return raw, None


async def create_session(db: AsyncSession, user_id: int, ip_address: str = None) -> str:
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
    await db.commit()
    sig = _sign_session(session_id, user_id)
    return f"{session_id}{_SESSION_SIG_SEP}{sig}"


async def get_session(db: AsyncSession, session_id: str):
    from app import db_models
    raw_sid, _sig = _parse_session_cookie(session_id)
    now = tz_now()
    result = await db.execute(
        select(db_models.Session).where(
            db_models.Session.id == raw_sid,
            db_models.Session.expires_at > now,
        )
    )
    session = result.scalar_one_or_none()
    if session:
        session.last_activity = now
        session.expires_at = now + timedelta(minutes=settings.session_expire_minutes)
        await db.commit()
    return session


async def delete_session(db: AsyncSession, session_id: str):
    from app import db_models
    raw_sid, _sig = _parse_session_cookie(session_id)
    await db.execute(delete(db_models.Session).where(db_models.Session.id == raw_sid))
    await db.commit()


async def cleanup_expired_sessions(db: AsyncSession):
    from app import db_models
    await db.execute(delete(db_models.Session).where(db_models.Session.expires_at <= tz_now()))
    await db.commit()


async def log_audit(db: AsyncSession, user_id: int | None, action: str, details: dict = None, ip_address: str = None):
    from app import db_models
    log = db_models.AuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps(details) if details else None,
        ip_address=ip_address,
    )
    db.add(log)
    await db.commit()


# ==================== FastAPI 依赖注入 ====================

from fastapi import Request, HTTPException, Depends
from app.database import get_async_db


def get_session_id_from_cookie(request: Request) -> str | None:
    raw = request.cookies.get("session_id")
    if raw is None:
        return None
    sid, _sig = _parse_session_cookie(raw)
    return sid


async def _get_validated_session(db: AsyncSession, request: Request) -> object | None:
    raw_sid = request.cookies.get("session_id")
    if not raw_sid:
        return None
    sid, sig = _parse_session_cookie(raw_sid)

    sess = await get_session(db, sid)
    if sess is None:
        return None
    if sig is not None:
        if not _verify_session(sid, sess.user_id, sig):
            return None
    return sess


async def get_current_user(request: Request, db: AsyncSession = Depends(get_async_db)):
    sess = await _get_validated_session(db, request)
    if sess is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    from app import db_models
    result = await db.execute(
        select(db_models.User).where(db_models.User.id == sess.user_id)
    )
    user = result.scalar_one_or_none()
    if not user or user.status == "disabled":
        raise HTTPException(status_code=401, detail="账号已被禁用")
    return user


def require_admin(user=Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_project_access(project_id: int = None):
    def dependency(user=Depends(get_current_user)):
        if user.role == "admin":
            return user
        if project_id is not None:
            allowed = user.project_ids or []
            if project_id not in allowed:
                raise HTTPException(status_code=403, detail="无权访问该项目")
        return user
    return dependency


def get_user_project_filter(user) -> list[int] | None:
    if user.role == "admin":
        return None
    return user.project_ids or []
