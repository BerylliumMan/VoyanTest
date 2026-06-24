"""Tests for app/rate_limiter.py — Limiter setup."""
from app.rate_limiter import limiter


class TestLimiter:
    def test_limiter_exists(self):
        assert limiter is not None

    def test_limiter_has_storage(self):
        assert limiter._storage is not None
