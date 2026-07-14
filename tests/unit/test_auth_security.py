"""Tests for app/auth.py security-critical functions (no DB needed)."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from app.auth import (
    _session_signing_key,
    _sign_session,
    _verify_session,
    _parse_session_cookie,
    _get_validated_session,
    require_project_access,
    get_user_project_filter,
)


class TestSessionSigningKey:
    def test_returns_bytes(self):
        key = _session_signing_key()
        assert isinstance(key, bytes)

    def test_returns_deterministic_with_env(self, monkeypatch):
        monkeypatch.setenv("SESSION_SECRET_KEY", "test-key-1234567890")
        k1 = _session_signing_key()
        k2 = _session_signing_key()
        assert k1 == k2
        assert len(k1) >= 32


class TestSessionSigning:
    def test_sign_and_verify_roundtrip(self):
        sig = _sign_session("session_abc", 42)
        assert _verify_session("session_abc", 42, sig) is True

    def test_verify_wrong_session_id(self):
        sig = _sign_session("session_abc", 42)
        assert _verify_session("session_wrong", 42, sig) is False

    def test_verify_wrong_user_id(self):
        sig = _sign_session("session_abc", 42)
        assert _verify_session("session_abc", 99, sig) is False

    def test_verify_tampered_signature(self):
        sig = _sign_session("session_abc", 42)
        assert _verify_session("session_abc", 42, sig + "x") is False


class TestParseSessionCookie:
    def test_parses_full_cookie(self):
        # rsplit('.', 1) → SID='abc123.v1', SIG='signature_here'
        sid, version = _parse_session_cookie("abc123.v1.signature_here")
        assert sid == "abc123.v1"
        assert version == "signature_here"

    def test_no_dot_returns_raw(self):
        sid, version = _parse_session_cookie("abc123")
        assert sid == "abc123"
        assert version is None

    def test_empty_string_returns_empty_sid(self):
        sid, version = _parse_session_cookie("")
        assert sid == ""
        assert version is None


class TestGetValidatedSession:
    @pytest.mark.asyncio
    async def test_no_session_cookie_returns_none(self):
        req = MagicMock()
        req.cookies = {}
        db = MagicMock()
        result = await _get_validated_session(db, req)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_session_in_db_returns_none(self):
        req = MagicMock()
        req.cookies = {"session_id": "nonexistent"}
        db = MagicMock()
        with patch("app.auth.get_session", AsyncMock(return_value=None)):
            result = await _get_validated_session(db, req)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_none(self):
        req = MagicMock()
        req.cookies = {"session_id": "bad_sig_session.v1.badsig"}
        db = MagicMock()
        mock_session = MagicMock()
        mock_session.user_id = 1
        with patch("app.auth.get_session", AsyncMock(return_value=mock_session)):
            result = await _get_validated_session(db, req)
        assert result is None


class TestRequireProjectAccess:
    def test_admin_has_access(self):
        user = MagicMock()
        user.role = "admin"
        result = require_project_access(project_id=1)(user=user)
        assert result.role == "admin"

    def test_tester_with_access(self):
        user = MagicMock()
        user.role = "tester"
        user.project_ids = [1, 2, 3]
        result = require_project_access(project_id=2)(user=user)
        assert result is user

    def test_tester_without_access_raises(self):
        user = MagicMock()
        user.role = "tester"
        user.project_ids = [4, 5]
        with pytest.raises(HTTPException) as exc:
            require_project_access(project_id=1)(user=user)
        assert exc.value.status_code == 403


class TestGetUserProjectFilter:
    def test_admin_returns_none(self):
        user = MagicMock()
        user.role = "admin"
        assert get_user_project_filter(user) is None

    def test_tester_returns_project_ids(self):
        user = MagicMock()
        user.role = "tester"
        user.project_ids = [1, 2]
        assert get_user_project_filter(user) == [1, 2]
