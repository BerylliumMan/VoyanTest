"""US3 认证继承链（步骤→用例→场景→环境默认）——033-api-test-maturity。

TDD：先于实现编写（预期红：resolve_auth_chain/apply_auth_chain 不存在）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core.api_runner import oauth as oauth_mod
from core.api_runner.oauth import clear_token_cache
from core.api_runner.request_builder import apply_auth_chain, resolve_auth_chain


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_token_cache()
    yield
    clear_token_cache()


# ── 解析链 ──────────────────────────────────────────────────────────────────

OAUTH_ENV = {
    "type": "oauth2",
    "oauth2": {"grant": "client_credentials", "token_url": "http://t/oauth", "client_id": "c"},
}
BEARER_CASE = {"type": "bearer", "token": "case-token"}
BASIC_SCENARIO = {"type": "basic", "username": "u", "password": "p"}


def test_step_explicit_wins_all_layers():
    step = {"type": "api_key", "key": "X-K", "value": "v", "in": "header"}
    out = resolve_auth_chain(step, case_auth=BEARER_CASE, scenario_auth=BASIC_SCENARIO, env_auth=OAUTH_ENV)
    assert out == step


def test_inherit_falls_to_case():
    out = resolve_auth_chain({"type": "inherit"}, case_auth=BEARER_CASE, scenario_auth=BASIC_SCENARIO, env_auth=OAUTH_ENV)
    assert out["type"] == "bearer" and out["token"] == "case-token"


def test_inherit_skips_missing_case_to_scenario():
    out = resolve_auth_chain({"type": "inherit"}, case_auth=None, scenario_auth=BASIC_SCENARIO, env_auth=OAUTH_ENV)
    assert out["type"] == "basic"


def test_inherit_chain_reaches_env_default():
    out = resolve_auth_chain({"type": "inherit"}, case_auth=None, scenario_auth=None, env_auth=OAUTH_ENV)
    assert out["type"] == "oauth2"


def test_inherit_across_chain_middle_inherit():
    """场景自身为 inherit 时继续向上。"""
    out = resolve_auth_chain({"type": "inherit"}, case_auth=None, scenario_auth={"type": "inherit"}, env_auth=BEARER_CASE)
    assert out["type"] == "bearer"


def test_all_layers_missing_becomes_none():
    out = resolve_auth_chain({"type": "inherit"}, case_auth=None, scenario_auth=None, env_auth=None)
    assert out["type"] == "none"


def test_none_step_stops_chain():
    """显式 none 视为配置（不继承）。"""
    out = resolve_auth_chain({"type": "none"}, case_auth=BEARER_CASE, scenario_auth=None, env_auth=OAUTH_ENV)
    assert out["type"] == "none"


def test_apply_auth_chain_rewrites_steps_without_mutation():
    spec = {
        "auth": BEARER_CASE,
        "steps": [
            {"order": 1, "request": {"url": "https://x", "auth": {"type": "inherit"}}},
            {"order": 2, "request": {"url": "https://y", "auth": {"type": "none"}}},
        ],
    }
    out = apply_auth_chain(spec, scenario_auth=None, env_default_auth=OAUTH_ENV)
    assert out["steps"][0]["request"]["auth"]["type"] == "bearer"
    assert out["steps"][1]["request"]["auth"]["type"] == "none"
    # 原 spec 不被就地修改
    assert spec["steps"][0]["request"]["auth"]["type"] == "inherit"


# ── api_spec 校验 ───────────────────────────────────────────────────────────

def test_validate_auth_oauth2_requires_fields():
    from core.api_spec import validate_api_spec

    spec = {
        "steps": [{
            "order": 1,
            "request": {"method": "GET", "url": "https://x", "auth": {"type": "oauth2", "oauth2": {"grant": "client_credentials"}}},
        }],
    }
    problems = validate_api_spec(spec)
    assert any("oauth2" in p and "token_url" in p for p in problems)


def test_validate_auth_oauth2_password_requires_credentials():
    from core.api_spec import validate_api_spec

    spec = {
        "steps": [{
            "order": 1,
            "request": {"method": "GET", "url": "https://x", "auth": {
                "type": "oauth2",
                "oauth2": {"grant": "password", "token_url": "http://t", "client_id": "c"},
            }},
        }],
    }
    problems = validate_api_spec(spec)
    assert any("password" in p for p in problems)


def test_validate_auth_inherit_ok_and_unknown_rejected():
    from core.api_spec import validate_api_spec

    ok = validate_api_spec({
        "steps": [{"order": 1, "request": {"method": "GET", "url": "https://x", "auth": {"type": "inherit"}}}],
    })
    assert not [p for p in ok if "auth" in p]
    bad = validate_api_spec({
        "steps": [{"order": 1, "request": {"method": "GET", "url": "https://x", "auth": {"type": "magic"}}}],
    })
    assert any("auth.type 非法" in p for p in bad)


def test_validate_auth_oauth2_ok():
    from core.api_spec import validate_api_spec

    spec = {
        "steps": [{
            "order": 1,
            "request": {"method": "GET", "url": "https://x", "auth": {
                "type": "oauth2",
                "oauth2": {"grant": "refresh_token", "token_url": "http://t", "client_id": "c", "refresh_token": "r"},
            }},
        }],
    }
    assert not [p for p in validate_api_spec(spec) if "auth" in p]


# ── 执行端到端（MockTransport）─────────────────────────────────────────────

def _oauth_spec(step_auth: dict) -> dict:
    return {
        "schema_version": 1,
        "variables": [],
        "fail_policy": "fail_fast",
        "steps": [{
            "order": 1, "name": "echo", "enable": True,
            "request": {
                "method": "GET", "url": "http://testserver/echo",
                "headers": [], "query": [],
                "body": {"type": "none", "content": ""},
                "auth": step_auth,
            },
            "assertions": [{"type": "status_code", "condition": "equals", "expected": 200, "enable": True}],
            "extractors": [],
        }],
    }


def _run(spec, *, environment=None, scenario_auth=None, client=None):
    from core.api_runner.runner import run_api_case

    async def _go():
        return await run_api_case(
            spec, environment=environment or {}, client=client, scenario_auth=scenario_auth,
        )

    return asyncio.run(_go())


def test_run_case_with_oauth2_fetches_token_and_injects_bearer(monkeypatch, oauth_mock):
    def _make(timeout: float = 15.0):
        return httpx.AsyncClient(transport=oauth_mock.transport, timeout=timeout)

    monkeypatch.setattr(oauth_mod, "make_client", _make)
    client = httpx.AsyncClient(transport=oauth_mock.transport, timeout=15)

    spec = _oauth_spec({
        "type": "oauth2",
        "oauth2": {"grant": "client_credentials", "token_url": "http://testserver/oauth/token",
                    "client_id": "cid", "client_secret": "sec"},
    })
    result = _run(spec, client=client)
    asyncio.run(client.aclose())
    assert result.status == "passed", result.error
    assert len(oauth_mock.token_calls) == 1
    assert oauth_mock.echo_calls and oauth_mock.echo_calls[0].headers.get("authorization") == "Bearer tok-1"


def test_inherit_chain_uses_env_default_auth(monkeypatch, oauth_mock):
    def _make(timeout: float = 15.0):
        return httpx.AsyncClient(transport=oauth_mock.transport, timeout=timeout)

    monkeypatch.setattr(oauth_mod, "make_client", _make)
    client = httpx.AsyncClient(transport=oauth_mock.transport, timeout=15)

    env = {"default_auth": {
        "type": "oauth2",
        "oauth2": {"grant": "client_credentials", "token_url": "http://testserver/oauth/token",
                    "client_id": "cid", "client_secret": "sec"},
    }}
    result = _run(_oauth_spec({"type": "inherit"}), environment=env, client=client)
    asyncio.run(client.aclose())
    assert result.status == "passed", result.error
    assert len(oauth_mock.token_calls) == 1


def test_scenario_auth_layer_used_when_step_inherits(monkeypatch, oauth_mock):
    def _make(timeout: float = 15.0):
        return httpx.AsyncClient(transport=oauth_mock.transport, timeout=timeout)

    monkeypatch.setattr(oauth_mod, "make_client", _make)
    client = httpx.AsyncClient(transport=oauth_mock.transport, timeout=15)

    scenario_auth = {
        "type": "oauth2",
        "oauth2": {"grant": "client_credentials", "token_url": "http://testserver/oauth/token",
                    "client_id": "cid", "client_secret": "sec"},
    }
    result = _run(_oauth_spec({"type": "inherit"}), scenario_auth=scenario_auth, client=client)
    asyncio.run(client.aclose())
    assert result.status == "passed", result.error


def test_oauth_failure_is_readable_step_error(monkeypatch, oauth_mock):
    def _make(timeout: float = 15.0):
        return httpx.AsyncClient(transport=oauth_mock.transport, timeout=timeout)

    monkeypatch.setattr(oauth_mod, "make_client", _make)
    oauth_mock.token_status = 503
    client = httpx.AsyncClient(transport=oauth_mock.transport, timeout=15)

    spec = _oauth_spec({
        "type": "oauth2",
        "oauth2": {"grant": "client_credentials", "token_url": "http://testserver/oauth/token",
                    "client_id": "cid", "client_secret": "sec"},
    })
    result = _run(spec, client=client)
    asyncio.run(client.aclose())
    assert result.status == "failed"
    assert "获取 OAuth2 令牌失败" in (result.steps[0].get("error") or "")
