# tests/unit/test_auth.py
"""认证模块单元测试 — 密码哈希、会话管理、密码强度校验。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.tz import now as tz_now
from app.auth import (
    hash_password, verify_password, validate_password_strength,
    create_session, get_session, delete_session, cleanup_expired_sessions,
)


@pytest.fixture
def admin_user(db, ensure_admin_user):
    """返回数据库中的 admin 用户对象。"""
    import asyncio
    from app import db_models
    async def _get():
        result = await db.execute(
            select(db_models.User).where(db_models.User.username == "admin")
        )
        return result.scalar_one_or_none()
    return asyncio.run(_get())


class TestPasswordHashing:
    """密码哈希与验证。"""

    @pytest.mark.asyncio
    async def test_hash_and_verify(self):
        hashed = hash_password("Admin@2024")
        assert verify_password("Admin@2024", hashed)

    @pytest.mark.asyncio
    async def test_wrong_password(self):
        hashed = hash_password("Admin@2024")
        assert not verify_password("wrong_password", hashed)

    @pytest.mark.asyncio
    async def test_different_hashes_for_same_password(self):
        h1 = hash_password("same_pass")
        h2 = hash_password("same_pass")
        assert h1 != h2  # bcrypt 使用随机 salt


class TestPasswordStrength:
    """密码强度校验。"""

    @pytest.mark.asyncio
    async def test_valid_password(self):
        ok, msg = validate_password_strength("Admin@2024")
        assert ok
        assert msg == ""

    @pytest.mark.asyncio
    async def test_too_short(self):
        ok, msg = validate_password_strength("Ab1")
        assert not ok
        assert "8" in msg

    @pytest.mark.asyncio
    async def test_no_digit(self):
        ok, msg = validate_password_strength("abcdefgh")
        assert not ok
        assert "数字" in msg

    @pytest.mark.asyncio
    async def test_no_letter(self):
        ok, msg = validate_password_strength("12345678")
        assert not ok
        assert "字母" in msg

    @pytest.mark.asyncio
    async def test_exactly_8_chars(self):
        ok, msg = validate_password_strength("A1@bcdef")
        assert ok


class TestSessionManagement:
    """会话创建、获取、删除、过期清理。"""

    @pytest.mark.asyncio
    async def test_create_and_get_session(self, db, admin_user):
        sid = await create_session(db, admin_user.id)
        assert sid is not None
        session = await get_session(db, sid)
        assert session is not None
        assert session.user_id == admin_user.id

    @pytest.mark.asyncio
    async def test_delete_session(self, db, admin_user):
        sid = await create_session(db, admin_user.id)
        await delete_session(db, sid)
        session = await get_session(db, sid)
        assert session is None

    @pytest.mark.asyncio
    async def test_expired_session_returns_none(self, db, admin_user):
        """手动设置过期时间，验证过期会话返回 None。"""
        from app import db_models
        sid = "expired_session_123"
        now = tz_now()
        session = db_models.Session(
            id=sid,
            user_id=admin_user.id,
            created_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),  # 已过期
            last_activity=now - timedelta(hours=2),
        )
        db.add(session)
        await db.commit()
        result = await get_session(db, sid)
        assert result is None

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions(self, db, admin_user):
        from app import db_models
        now = tz_now()
        expired = db_models.Session(
            id="cleanup_expired_1",
            user_id=admin_user.id,
            created_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
            last_activity=now - timedelta(hours=2),
        )
        db.add(expired)
        await db.commit()
        await cleanup_expired_sessions(db)
        result = await db.execute(
            select(db_models.Session).where(db_models.Session.id == "cleanup_expired_1")
        )
        record = result.scalar_one_or_none()
        assert record is None

    @pytest.mark.asyncio
    async def test_session_renewal_on_access(self, db, admin_user):
        """访问有效会话时，过期时间应被续期。"""
        sid = await create_session(db, admin_user.id)
        session_before = await get_session(db, sid)
        expires_before = session_before.expires_at
        # 再次获取，应续期
        session_after = await get_session(db, sid)
        assert session_after.expires_at >= expires_before


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_log_audit_basic(self, db, admin_user):
        from app.auth import log_audit
        await log_audit(db, user_id=admin_user.id, action="unit_test_action")
        from app import db_models
        result = await db.execute(
            select(db_models.AuditLog).where(db_models.AuditLog.action == "unit_test_action")
        )
        logs = result.scalars().all()
        assert len(logs) >= 1

    @pytest.mark.asyncio
    async def test_log_audit_with_details(self, db, admin_user):
        from app.auth import log_audit
        await log_audit(db, user_id=admin_user.id, action="detail_action", details={"key": "val"})
        from app import db_models
        result = await db.execute(
            select(db_models.AuditLog).where(db_models.AuditLog.action == "detail_action")
        )
        logs = result.scalars().all()
        assert len(logs) >= 1


class TestAuthDeps:
    @pytest.mark.asyncio
    async def test_get_session_id_from_cookie(self):
        from app.auth import get_session_id_from_cookie
        req = MagicMock()
        req.cookies = {"session_id": "test_sid"}
        assert get_session_id_from_cookie(req) == "test_sid"

    @pytest.mark.asyncio
    async def test_get_session_id_from_cookie_missing(self):
        from app.auth import get_session_id_from_cookie
        req = MagicMock()
        req.cookies = {}
        assert get_session_id_from_cookie(req) is None

    @pytest.mark.asyncio
    async def test_get_current_user_no_session(self):
        from app.auth import get_current_user
        req = MagicMock()
        req.cookies = {}
        with pytest.raises(Exception) as excinfo:
            await get_current_user(request=req, db=MagicMock())
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user_expired_session(self, db):
        """无效 session_id 应返回 401。"""
        from app.auth import get_current_user
        req = MagicMock()
        req.cookies = {"session_id": "this-session-does-not-exist"}
        with pytest.raises(Exception) as excinfo:
            await get_current_user(request=req, db=db)
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user_disabled(self, db, admin_user):
        """已禁用用户应返回 401。"""
        from app.auth import get_current_user, create_session
        admin_user.status = "disabled"
        await db.commit()
        sid = await create_session(db, admin_user.id)
        req = MagicMock()
        req.cookies = {"session_id": sid}
        with pytest.raises(Exception) as excinfo:
            await get_current_user(request=req, db=db)
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_require_admin_passes(self):
        from app.auth import require_admin
        user = MagicMock()
        user.role = "admin"
        assert require_admin(user=user).role == "admin"

    @pytest.mark.asyncio
    async def test_get_current_user_success(self, db, admin_user):
        from app.auth import get_current_user, create_session
        sid = await create_session(db, admin_user.id)
        req = MagicMock()
        req.cookies = {"session_id": sid}
        user = await get_current_user(request=req, db=db)
        assert user is not None
        assert user.id == admin_user.id

    @pytest.mark.asyncio
    async def test_require_admin_forbidden(self):
        from app.auth import require_admin
        user = MagicMock()
        user.role = "tester"
        with pytest.raises(Exception) as excinfo:
            require_admin(user=user)
        assert excinfo.value.status_code == 403
