"""Tests for shared fixtures in tests/conftest.py."""


class TestEnsureAdminUser:
    def test_creates_admin(self, db, ensure_admin_user):
        """ensure_admin_user fixture 创建干净可用的 admin 用户。"""
        from app import db_models
        from app.auth import verify_password

        user = db.query(db_models.User).filter(db_models.User.username == "admin").first()
        assert user is not None
        assert verify_password("Admin@2024", user.password_hash)
        assert user.must_change_password is False

    def test_resets_existing_admin(self, db, ensure_admin_user):
        """ensure_admin_user 覆盖已有 admin 的密码和 must_change_password。"""
        from app import db_models
        from app.auth import hash_password, verify_password
        from tests.conftest import _ensure_admin_user_body

        existing = db.query(db_models.User).filter(db_models.User.username == "admin").first()
        existing.password_hash = hash_password("wrong_password")
        existing.must_change_password = True
        db.commit()

        _ensure_admin_user_body(db)

        updated = db.query(db_models.User).filter(db_models.User.username == "admin").first()
        assert verify_password("Admin@2024", updated.password_hash)
        assert updated.must_change_password is False
        assert updated.role == "admin"
        assert updated.status == "active"
