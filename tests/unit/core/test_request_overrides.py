"""US7 pre_script 请求改写（set_request_*）——033-api-test-maturity。

TDD：先于实现编写（预期红：改写函数未注入 / request_overrides 不存在）。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from core.script_sandbox import run_script


def _run(script: str, *, with_overrides: bool = True):
    return run_script(script, {"vars": {}}, with_request_overrides=with_overrides)


# ── 沙箱层 ──────────────────────────────────────────────────────────────────

def test_override_functions_record_structured_values():
    out = _run(
        'set_request_url("https://x.example.com/a")\n'
        'set_request_method("post")\n'
        'set_request_header("X-Sign", "abc")\n'
        'set_request_query("page", "2")\n'
        'set_request_body({"a": 1, "b": "{{v}}"})\n'
        'set_request_auth("bearer", token="tok")\n'
    )
    ov = out.request_overrides
    assert out.ok
    assert ov["url"] == "https://x.example.com/a"
    assert ov["method"] == "POST"  # 统一大写
    assert ov["headers"]["X-Sign"] == "abc"
    assert ov["query"]["page"] == "2"
    assert json.loads(ov["body"]) == {"a": 1, "b": "{{v}}"}
    assert ov["auth"] == {"type": "bearer", "token": "tok"}


def test_remove_functions_mark_removals():
    out = _run(
        'remove_request_header("Authorization")\n'
        'remove_request_query("token")\n'
    )
    assert out.ok
    assert out.request_overrides["remove_headers"] == ["Authorization"]
    assert out.request_overrides["remove_query"] == ["token"]


def test_invalid_method_fails_script():
    out = _run('set_request_method("FLY")')
    assert out.ok is False
    assert "method" in out.error


def test_invalid_auth_type_fails_script():
    out = _run('set_request_auth("magic")')
    assert out.ok is False
    assert "auth" in out.error


def test_override_functions_unavailable_without_flag():
    out = _run('set_request_url("https://x")', with_overrides=False)
    assert out.ok is False
    assert "set_request_url" in out.error


def test_no_overrides_means_empty_dict():
    out = _run("x = 1")
    assert out.ok and out.request_overrides == {}


# ── 执行链路：覆盖在渲染前应用、spec 不被修改 ────────────────────────────────

def _spec():
    return {
        "schema_version": 1, "variables": [{"key": "user", "value": "u1", "enable": True}],
        "fail_policy": "fail_fast",
        "steps": [{
            "order": 1, "name": "orig", "enable": True,
            "request": {"method": "GET", "url": "http://testserver/original",
                         "headers": [{"key": "X-Keep", "value": "1", "enable": True},
                                      {"key": "Authorization", "value": "Bearer old", "enable": True}],
                         "query": [], "body": {"type": "none", "content": ""},
                         "auth": {"type": "none"}},
            "assertions": [],
            "extractors": [],
            "pre_script": (
                'set_request_url("http://testserver/ok?u={{user}}")\n'
                'set_request_header("X-Over", "yes")\n'
                'remove_request_header("authorization")\n'
                'set_request_body({"hello": "world"})\n'
            ),
        }],
    }


def test_runner_applies_overrides_before_render(echo_server):
    from core.api_runner.runner import run_api_case

    spec = _spec()
    spec_snapshot = json.dumps(spec, ensure_ascii=False, sort_keys=True)
    client = httpx.AsyncClient(transport=echo_server.transport, timeout=15)

    async def _go():
        try:
            return await run_api_case(spec, environment={}, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(_go())
    assert result.status == "passed", result.steps[0].get("error")
    req = echo_server.requests[0]
    assert str(req.url) == "http://testserver/ok?u=u1"  # 覆盖值照常渲染变量
    assert req.headers.get("x-over") == "yes"
    assert req.headers.get("authorization") is None  # remove 生效
    assert req.headers.get("x-keep") == "1"  # 既有头保留
    assert req.content.decode() == '{"hello": "world"}'  # dict → JSON 文本
    # 原始 spec 未被修改
    assert json.dumps(spec, ensure_ascii=False, sort_keys=True) == spec_snapshot
