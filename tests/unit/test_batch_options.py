"""US10 批量运行器选项（delay_ms / stop_on_error）——033-api-test-maturity。

TDD：先于实现编写（预期红：schema 字段/validate_delay_ms 不存在）。
"""

from __future__ import annotations

import pytest

from app.routers.testcase.execution._schemas import BatchRunRequest, validate_delay_ms


def test_schema_accepts_runner_options():
    req = BatchRunRequest(case_ids=[1, 2], delay_ms=1500, stop_on_error=True)
    assert req.delay_ms == 1500 and req.stop_on_error is True
    # 缺省不传 → None（沿用既有行为）
    assert BatchRunRequest(case_ids=[1]).delay_ms is None


def test_validate_delay_defaults_and_bounds():
    assert validate_delay_ms(None, 60000) == 0
    assert validate_delay_ms(0, 60000) == 0
    assert validate_delay_ms(60000, 60000) == 60000


def test_validate_delay_rejects_bad_values():
    with pytest.raises(ValueError):
        validate_delay_ms(-1, 60000)
    with pytest.raises(ValueError):
        validate_delay_ms(60001, 60000)
    with pytest.raises(ValueError):
        validate_delay_ms(True, 60000)  # bool 不是合法毫秒数
    with pytest.raises(ValueError):
        validate_delay_ms("100", 60000)  # 字符串拒绝（前端应传数字）
