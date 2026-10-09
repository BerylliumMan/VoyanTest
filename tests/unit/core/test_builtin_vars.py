"""US9 动态内置变量扩充 —— 033-api-test-maturity。

TDD：先于实现编写（预期红：6 个新变量未实现）。
"""

from __future__ import annotations

import re

from core.api_runner.variables import _builtin


def test_existing_variables_regression():
    assert re.fullmatch(r"[0-9a-f-]{36}", _builtin("$uuid"))
    assert _builtin("$timestamp").isdigit()
    assert _builtin("$timestampMs").isdigit() and len(_builtin("$timestampMs")) >= 13
    assert _builtin("$randomInt").isdigit()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", _builtin("$date"))


def test_random_alpha_numeric():
    v = _builtin("$randomAlphaNumeric")
    assert v is not None and re.fullmatch(r"[A-Za-z0-9]{10}", v)


def test_random_password_has_classes():
    v = _builtin("$randomPassword")
    assert v is not None and len(v) == 12
    assert re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"[0-9]", v)


def test_random_boolean():
    values = {_builtin("$randomBoolean") for _ in range(40)}
    assert values <= {"true", "false"} and values == {"true", "false"}


def test_random_color():
    assert re.fullmatch(r"#[0-9a-f]{6}", _builtin("$randomColor") or "")


def test_random_ipv4():
    v = _builtin("$randomIPv4") or ""
    parts = v.split(".")
    assert len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def test_random_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _builtin("$randomSemver") or "")


def test_unknown_still_none():
    assert _builtin("$nope") is None
    assert _builtin("plain") is None


def test_variables_are_independent_across_calls():
    a = _builtin("$randomAlphaNumeric")
    b = _builtin("$randomAlphaNumeric")
    assert a != b  # 每次调用独立生成（36^10 碰撞概率可忽略）
