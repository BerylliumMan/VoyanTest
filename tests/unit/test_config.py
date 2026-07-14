"""Tests for app/config.py — Settings + get_settings."""
import os
from app.config import Settings, get_settings


class TestSettings:
    def test_default_values(self):
        s = Settings()
        assert s.app_host == "0.0.0.0"
        assert s.app_port == 8002
        assert s.browser_type == "chromium"
        assert s.headless is True
        assert s.session_expire_minutes == 30
        assert s.max_login_attempts == 5
        assert s.cookie_secure is False
        assert s.log_level == "INFO"

    def test_overrides_from_env(self, monkeypatch):
        monkeypatch.setenv("APP_PORT", "9090")
        monkeypatch.setenv("BROWSER_TYPE", "firefox")
        monkeypatch.setenv("HEADLESS", "false")
        s = Settings()
        assert s.app_port == 9090
        assert s.browser_type == "firefox"
        assert s.headless is False

    def test_cors_defaults(self):
        s = Settings()
        assert "localhost:3000" in s.cors_allow_origins
        assert s.cors_allow_credentials is True


class TestGetSettings:
    def test_returns_settings(self):
        s = get_settings()
        assert isinstance(s, Settings)
        assert s.app_host == "0.0.0.0"

    def test_cached(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # lru_cache
