"""Tests for app/tz.py — timezone utilities."""
from app.tz import now, CST


class TestTimezone:
    def test_cst_is_utc_plus_8(self):
        assert CST.utcoffset(None).total_seconds() == 28800  # 8 * 3600

    def test_now_returns_aware_datetime(self):
        dt = now()
        assert dt.tzinfo is not None
        assert dt.tzinfo is CST

    def test_now_returns_current_year(self):
        from datetime import datetime as dt_mod
        dt = now()
        assert dt.year == dt_mod.now(CST).year
