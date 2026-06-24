"""Tests for shared fixtures in tests/conftest.py."""
import pytest
from sqlalchemy import select


class TestEnsureAdminUser:
    @pytest.mark.asyncio
    async def test_creates_admin(self, db, ensure_admin_user):
        """ensure_admin_user fixture 创建干净可用的 admin 用户。"""
        from app import db_models
        from app.auth import verify_password

        result = await db.execute(
            select(db_models.User).where(db_models.User.username == "admin")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert verify_password("Admin@2024", user.password_hash)
        assert user.must_change_password is False

    @pytest.mark.asyncio
    async def test_resets_existing_admin(self, db, ensure_admin_user):
        """ensure_admin_user 覆盖已有 admin 的密码和 must_change_password。"""
        from app import db_models
        from app.auth import hash_password, verify_password
        from tests.conftest import _ensure_admin_user_body

        result = await db.execute(
            select(db_models.User).where(db_models.User.username == "admin")
        )
        existing = result.scalar_one_or_none()
        existing.password_hash = hash_password("wrong_password")
        existing.must_change_password = True
        await db.commit()

        await _ensure_admin_user_body(db)

        result2 = await db.execute(
            select(db_models.User).where(db_models.User.username == "admin")
        )
        updated = result2.scalar_one_or_none()
        assert verify_password("Admin@2024", updated.password_hash)
        assert updated.must_change_password is False
        assert updated.role == "admin"
        assert updated.status == "active"
