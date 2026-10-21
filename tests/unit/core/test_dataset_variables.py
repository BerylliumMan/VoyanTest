"""039：参数占位符发现/校验/渲染（core/dataset_variables.py）。"""
from __future__ import annotations

from types import SimpleNamespace

from core.dataset_variables import (
    PARAM_RE,
    declared_parameter_names,
    discover_parameters,
    missing_columns,
    parameter_names,
    render_steps,
    substitute_text,
    validate_parameters,
)


def _steps():
    return [
        {
            "step_order": 1,
            "description": "打开登录页",
            "parsed_result": None,
            "structured_step": None,
        },
        {
            "step_order": 2,
            "description": "用账号 {{username}}、密码 {{ password }} 登录",
            "parsed_result": "进入首页",
            "structured_step": {
                "action": "fill",
                "target_name": "Username",
                "value": "{{username}}",
            },
        },
    ]


def test_discover_parameters_in_order():
    assert discover_parameters(_steps()) == ["username", "password"]


def test_discover_supports_orm_steps():
    step = SimpleNamespace(
        step_order=1,
        description="搜索 {{keyword}}",
        parsed_result=None,
        structured_step=None,
    )
    assert discover_parameters([step]) == ["keyword"]


def test_declared_names_dedup():
    assert declared_parameter_names(
        [{"name": "username"}, {"name": " password "}, {"name": "username"}]
    ) == ["username", "password"]


def test_validate_missing_declaration_reports_step():
    errors = validate_parameters(_steps(), [{"name": "username"}])
    assert len(errors) == 1
    assert "password" in errors[0] and "步骤 2" in errors[0]


def test_validate_declared_ok():
    assert validate_parameters(_steps(), [{"name": "username"}, {"name": "password"}]) == []


def test_substitute_text():
    assert substitute_text("u={{username}}", {"username": "alice"}) == "u=alice"
    assert substitute_text("{{missing}}", {}) == "{{missing}}"


def test_substitute_text_strict_raises():
    import pytest

    with pytest.raises(KeyError):
        substitute_text("{{missing}}", {}, strict=True)


def test_render_steps_does_not_mutate_and_renders_structured():
    steps = _steps()
    rendered = render_steps(steps, {"username": "alice", "password": "pw"})
    assert rendered[1]["description"] == "用账号 alice、密码 pw 登录"
    assert rendered[1]["structured_step"]["value"] == "alice"
    assert steps[1]["description"].startswith("用账号 {{username}}")


def test_parameter_names_declared_first():
    assert parameter_names([{"name": "password"}], _steps()) == ["password", "username"]


def test_missing_columns():
    assert missing_columns(_steps(), ["username"]) == ["password"]
    assert missing_columns(_steps(), ["username", "password"]) == []


def test_param_regex_requires_identifier():
    assert PARAM_RE.findall("{{123}} {{ok_name}}") == ["ok_name"]
