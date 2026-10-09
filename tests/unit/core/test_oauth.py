"""US3 OAuth2 令牌获取/缓存/锁（033-api-test-maturity）。

TDD：先于实现编写（预期红：core.api_runner.oauth 尚不存在）。
"""

from __future__ import annotations

import asyncio
import urllib.parse

import httpx
import pytest

from core.api_runner import oauth as oauth_mod
from core.api_runner.oauth import OAuthTokenError, clear_token_cache, fetch_token


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_token_cache()
    yield
    clear_token_cache()


def _patch_client(monkeypatch, oauth_mock):
    """把 oauth 模块的 client 工厂指向 MockTransport（零网络）。"""

    def _make(timeout: float = 15.0):
        return httpx.AsyncClient(transport=oauth_mock.transport, timeout=timeout)

    monkeypatch.setattr(oauth_mod, "make_client", _make)


def _cfg(**over):
    cfg = {
        "grant": "client_credentials",
        "token_url": "http://testserver/oauth/token",
        "client_id": "cid",
        "client_secret": "sec",
        "scope": "read",
        "client_auth": "body",
    }
    cfg.update(over)
    return cfg


def _form(request: httpx.Request) -> dict:
    return dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))


def test_client_credentials_fetch_and_cache(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)

    async def run():
        t1 = await fetch_token(_cfg())
        t2 = await fetch_token(_cfg())
        return t1, t2

    t1, t2 = asyncio.run(run())
    assert t1 == t2 == "tok-1"
    assert len(oauth_mock.token_calls) == 1  # 第二次命中缓存


def test_concurrent_fetch_deduplicated(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)

    async def run():
        return await asyncio.gather(*[fetch_token(_cfg()) for _ in range(5)])

    tokens = asyncio.run(run())
    assert tokens == ["tok-1"] * 5
    assert len(oauth_mock.token_calls) == 1  # 锁 + 双重检查去重


def test_cache_expiry_refetches(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)
    oauth_mock.token_payload = {"access_token": "tok-x", "expires_in": 10}

    async def run():
        await fetch_token(_cfg(), buffer_s=60)   # TTL 10s < buffer 60s → 不可用
        return await fetch_token(_cfg(), buffer_s=60)

    token = asyncio.run(run())
    assert token == "tok-x"
    assert len(oauth_mock.token_calls) == 2


def test_client_auth_basic(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)

    asyncio.run(fetch_token(_cfg(client_auth="basic")))
    req = oauth_mock.token_calls[0]
    auth = req.headers.get("authorization") or ""
    assert auth.lower().startswith("basic ")
    form = _form(req)
    # basic 模式下凭据走 Authorization 头，不落表单
    assert "client_secret" not in form and "client_id" not in form


def test_password_grant_body(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)

    asyncio.run(fetch_token(_cfg(grant="password", username="u1", password="p1")))
    form = _form(oauth_mock.token_calls[0])
    assert form.get("grant_type") == "password"
    assert form.get("username") == "u1" and form.get("password") == "p1"


def test_refresh_token_grant_body(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)

    asyncio.run(fetch_token(_cfg(grant="refresh_token", refresh_token="r1")))
    form = _form(oauth_mock.token_calls[0])
    assert form.get("grant_type") == "refresh_token"
    assert form.get("refresh_token") == "r1"


def test_body_auth_includes_client_secret(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)

    asyncio.run(fetch_token(_cfg()))
    form = _form(oauth_mock.token_calls[0])
    assert form.get("client_secret") == "sec"


def test_token_endpoint_error(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)
    oauth_mock.token_status = 500

    with pytest.raises(OAuthTokenError) as ei:
        asyncio.run(fetch_token(_cfg()))
    assert "500" in str(ei.value)


def test_missing_access_token_raises(monkeypatch, oauth_mock):
    _patch_client(monkeypatch, oauth_mock)
    oauth_mock.token_payload = {"token_type": "bearer"}

    with pytest.raises(OAuthTokenError):
        asyncio.run(fetch_token(_cfg()))
