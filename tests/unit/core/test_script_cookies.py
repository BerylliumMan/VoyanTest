"""US8 脚本 cookie 读写会话语义 —— 033-api-test-maturity。

TDD：先于实现编写（预期红：cookie 函数未注入 / ScriptResult 无 set_cookies）。
"""

from __future__ import annotations

import asyncio

import httpx

from core.script_sandbox import run_script


def _run(script, *, with_cookies: bool = True, context=None):
    return run_script(script, context or {"vars": {}}, with_cookies=with_cookies)


def test_set_and_delete_recorded():
    out = _run(
        'set_cookie("token", "t1")\n'
        'set_cookie("s2", "v2", domain="example.com", path="/api")\n'
        'delete_cookie("old")\n'
    )
    assert out.ok
    assert out.set_cookies == [
        {"name": "token", "value": "t1"},
        {"name": "s2", "value": "v2", "domain": "example.com", "path": "/api"},
    ]
    assert out.delete_cookies == [{"name": "old"}]


def test_get_response_cookie_parses_header():
    ctx = {"vars": {}, "headers": {"set-cookie": "sid=abc123; Path=/; HttpOnly"}}
    out = _run('got = get_response_cookie("sid")\nmissing = get_response_cookie("nope")\n', context=ctx)
    assert out.ok
    assert out.variables.get("got") == "abc123"
    assert out.variables.get("missing") in (None, "")


def test_get_cookie_reads_session_context():
    ctx = {"vars": {}, "cookies": {"t2": "sess-val"}}
    out = _run('got = get_cookie("t2")\n', context=ctx)
    assert out.ok and out.variables.get("got") == "sess-val"


def test_unavailable_when_flag_off():
    out = _run('set_cookie("a", "b")', with_cookies=False)
    assert out.ok is False
    assert "set_cookie" in out.error


def test_invalid_cookie_name_fails():
    out = _run('set_cookie("", "v")')
    assert out.ok is False
    assert "cookie" in out.error.lower() or "名称" in out.error


# ── 执行链路：写 jar → 后续步骤携带 ─────────────────────────────────────────

def _spec():
    return {
        "schema_version": 1, "variables": [], "fail_policy": "fail_fast",
        "steps": [
            {
                "order": 1, "name": "login", "enable": True,
                "request": {"method": "GET", "url": "http://testserver/login", "headers": [], "query": [],
                             "body": {"type": "none", "content": ""}, "auth": {"type": "none"}},
                "assertions": [],
                "extractors": [],
                "post_script": (
                    'got = get_response_cookie("sid")\n'
                    'check("读到登录 sid", got == "abc123")\n'
                    'set_cookie("token", "t1")\n'
                ),
            },
            {
                "order": 2, "name": "next", "enable": True,
                "request": {"method": "GET", "url": "http://testserver/ok", "headers": [], "query": [],
                             "body": {"type": "none", "content": ""}, "auth": {"type": "none"}},
                "assertions": [],
                "extractors": [],
            },
        ],
    }


def test_runner_writes_cookie_to_session_and_next_step_carries_it(echo_server):
    from core.api_runner.runner import run_api_case

    echo_server.set_cookie = "sid=abc123; Path=/"
    client = httpx.AsyncClient(transport=echo_server.transport, timeout=15)

    async def _go():
        try:
            return await run_api_case(_spec(), environment={}, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(_go())
    assert result.status == "passed", result.steps[0].get("error")
    step1 = result.steps[0]
    assert step1["assertions"][0]["name"] == "读到登录 sid" and step1["assertions"][0]["passed"]
    second = echo_server.requests[1]
    assert "token=t1" in (second.headers.get("cookie") or "")


def test_runner_delete_cookie_removes_from_session(echo_server):
    from core.api_runner.runner import run_api_case

    spec = _spec()
    spec["steps"][0]["post_script"] = 'set_cookie("token", "t1")'
    spec["steps"].insert(1, {
        "order": 2, "name": "del", "enable": True,
        "request": {"method": "GET", "url": "http://testserver/mid", "headers": [], "query": [],
                     "body": {"type": "none", "content": ""}, "auth": {"type": "none"}},
        "assertions": [], "extractors": [],
        "post_script": 'delete_cookie("token")',
    })
    client = httpx.AsyncClient(transport=echo_server.transport, timeout=15)

    async def _go():
        try:
            return await run_api_case(spec, environment={}, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(_go())
    assert result.status == "passed", result.steps[0].get("error")
    assert "token=t1" in (echo_server.requests[1].headers.get("cookie") or "")
    assert "token=t1" not in (echo_server.requests[2].headers.get("cookie") or "")
