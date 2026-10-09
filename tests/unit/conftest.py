"""033-api-test-maturity：共享测试夹具（mock OAuth / cookie echo / 批次参数）。

本文件位于 tests/unit/ 根，对 tests/unit/ 全部子目录（含 core/）可见。
所有夹具均为**进程内无网络**（httpx.MockTransport）。
"""

from __future__ import annotations

import httpx
import pytest


def _cookie_dict(request: httpx.Request) -> dict:
    """httpx.Request 无 .cookies 属性——从 Cookie 头解析（测试夹具辅助）。"""
    raw = request.headers.get("cookie") or ""
    out: dict[str, str] = {}
    for pair in raw.split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            key = key.strip()
            if key:
                out[key] = value.strip()
    return out


class OAuthMock:
    """模拟 OAuth2 令牌服务 + 受保护 echo；记录调用次数（缓存断言用）。"""

    def __init__(self) -> None:
        self.token_calls: list[httpx.Request] = []
        self.echo_calls: list[httpx.Request] = []
        self.token_status = 200
        self.token_payload: dict = {
            "access_token": "tok-1",
            "token_type": "bearer",
            "expires_in": 3600,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            self.token_calls.append(request)
            return httpx.Response(self.token_status, json=self.token_payload)
        if request.url.path == "/echo":
            self.echo_calls.append(request)
            if request.headers.get("authorization") != "Bearer tok-1":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(
                200,
                json={"headers": dict(request.headers), "cookies": _cookie_dict(request)},
            )
        return httpx.Response(404, json={"error": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def oauth_mock() -> OAuthMock:
    return OAuthMock()


class EchoServer:
    """记录请求并回显；可配置 Set-Cookie（cookie 会话测试用）。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.set_cookie: str | None = None
        self.set_cookie_path = "/login"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {}
        if self.set_cookie and request.url.path == self.set_cookie_path:
            headers["set-cookie"] = self.set_cookie
        body = {
            "headers": dict(request.headers),
            "cookies": _cookie_dict(request),
            "url": str(request.url),
            "body": request.content.decode("utf-8", "replace"),
        }
        return httpx.Response(200, json=body, headers=headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def echo_server() -> EchoServer:
    return EchoServer()


@pytest.fixture
def make_batch_payload():
    """构造 /api/testcases/batch-run 请求体（033 US10）。"""

    def _make(case_ids, **kw):
        payload: dict = {"case_ids": list(case_ids)}
        payload.update(kw)
        return payload

    return _make
