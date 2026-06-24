"""Tests for app/rate_limiter.py — Limiter setup."""
from app.rate_limiter import limiter


class TestLimiter:
    def test_limiter_exists(self):
        assert limiter is not None

    def test_limiter_has_key_func(self):
        assert limiter.key_func is not None
