"""US4 脚本命名断言（check 系列）——033-api-test-maturity。

TDD：先于实现编写（预期红：check 系列尚未注入、ScriptResult.tests 不存在）。
"""

from __future__ import annotations

import asyncio

import httpx

from core.script_sandbox import run_script


def _run(script: str, *, with_checks: bool = True):
    return run_script(script, {"vars": {}}, with_checks=with_checks)


# ── 基础行为 ────────────────────────────────────────────────────────────────

def test_check_pass_and_fail_collected():
    out = _run(
        'check("状态码 200", 200 == 200)\n'
        'check("字段存在", "id" in "id,name")\n'
        'check_eq("故意失败", 1, 2)\n'
    )
    assert out.ok is True  # 脚本本身执行成功
    assert len(out.tests) == 3
    assert [t["ok"] for t in out.tests] == [True, True, False]
    assert out.tests[0]["name"] == "状态码 200"
    assert "1" in out.tests[2]["message"] and "2" in out.tests[2]["message"]


def test_checks_do_not_abort_remaining_collectively():
    out = _run(
        'check("a", False)\n'
        'check("b", False)\n'
        'check("c", True)\n'
    )
    assert out.ok is True and len(out.tests) == 3  # 收集式：全部执行


def test_check_close_tolerance():
    out = _run('check_close("π", 3.14159, 3.14, tol=0.01)')
    assert out.tests[0]["ok"] is True
    out2 = _run('check_close("π", 3.14159, 3.0, tol=0.01)')
    assert out2.tests[0]["ok"] is False


def test_check_match_invalid_pattern_fails_not_raises():
    out = _run('check_match("regex", "abc", "[")')
    assert out.ok is True
    assert out.tests[0]["ok"] is False
    assert "正则" in out.tests[0]["message"]


def test_check_helpers_matrix():
    out = _run(
        'check_eq("eq", 1, 1)\n'
        'check_ne("ne", 1, 2)\n'
        'check_in("in", 2, [1, 2, 3])\n'
        'check_not_in("nin", 9, [1, 2, 3])\n'
        'check_len("len", [1, 2], 2)\n'
        'check_true("t", True)\n'
        'check_false("f", False)\n'
    )
    assert out.ok and all(t["ok"] for t in out.tests)
    assert len(out.tests) == 7


def test_failed_helper_message_contains_values():
    out = _run('check_eq("相等", "actual-x", "expected-y")')
    msg = out.tests[0]["message"]
    assert "actual-x" in msg and "expected-y" in msg


# ── 开关与边界 ──────────────────────────────────────────────────────────────

def test_checks_not_available_without_flag():
    out = _run('check("x", True)', with_checks=False)
    assert out.ok is False
    assert "check" in out.error


def test_checks_cap_200():
    script = "\n".join(f'check("c{i}", True)' for i in range(201))
    out = _run(script)
    assert out.ok is False
    assert "上限" in out.error


def test_no_checks_means_empty_tests():
    out = _run('x = 1 + 1')
    assert out.ok and out.tests == []


def test_check_name_and_message_truncated():
    out = _run(f'check("{"n" * 600}", False)')
    assert out.ok
    assert len(out.tests[0]["name"]) <= 200


# ── 执行链路集成（check → 步骤断言）──────────────────────────────────────────

def _post_script_spec(post_script: str, assertions=None) -> dict:
    return {
        "schema_version": 1, "variables": [], "fail_policy": "fail_fast",
        "steps": [{
            "order": 1, "name": "echo", "enable": True,
            "request": {"method": "GET", "url": "http://testserver/echo", "headers": [], "query": [],
                         "body": {"type": "none", "content": ""}, "auth": {"type": "none"}},
            "assertions": assertions or [],
            "extractors": [],
            "post_script": post_script,
        }],
    }


def test_runner_merges_checks_into_step_assertions(echo_server):
    from core.api_runner.runner import run_api_case

    client = httpx.AsyncClient(transport=echo_server.transport, timeout=15)
    spec = _post_script_spec(
        'check("响应包含 url", True)\ncheck("字段存在", False)\n',
        assertions=[{"type": "status_code", "condition": "equals", "expected": 200, "enable": True}],
    )
    result = asyncio.run(run_api_case(spec, environment={}, client=client))
    asyncio.run(client.aclose())
    step = result.steps[0]
    names = [a["name"] for a in step["assertions"]]
    assert "响应包含 url" in names and "字段存在" in names
    script_checks = [a for a in step["assertions"] if a.get("source") == "post_script"]
    assert len(script_checks) == 2
    # 失败 check → 步骤失败（与结构化断言同权）
    assert result.status == "failed"
    assert step["assertions"][0]["passed"] is True


def test_runner_all_checks_pass(echo_server):
    from core.api_runner.runner import run_api_case

    client = httpx.AsyncClient(transport=echo_server.transport, timeout=15)
    spec = _post_script_spec('check("ok", True)')
    result = asyncio.run(run_api_case(spec, environment={}, client=client))
    asyncio.run(client.aclose())
    assert result.status == "passed"
    assert len(result.steps[0]["assertions"]) == 1
