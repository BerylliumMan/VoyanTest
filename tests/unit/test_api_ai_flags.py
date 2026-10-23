"""040 T066：AI 开关默认值。"""
from app.config import Settings


def test_api_ai_defaults_enabled():
    s = Settings()
    assert s.api_ai_enabled is True
    for name in ("analysis", "generation", "data", "contract", "strategy"):
        assert getattr(s, f"api_ai_{name}_enabled") is True


def test_api_ai_limits_are_sane():
    s = Settings()
    assert s.api_ai_max_input_chars >= 1000
    assert s.api_ai_max_suggestions >= 1
    assert s.api_ai_timeout_s >= 10
