"""CSRF 防护中间件 — Double Submit Cookie 模式。

对所有 state-changing 请求（POST/PUT/DELETE），校验请求头中的
``X-CSRF-Token`` 是否与 cookie 中的 ``csrf_token`` 一致。
"""
from __future__ import annotations

import secrets
from typing import Optional

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings

_EXCLUDE_PATHS = (
    "/api/auth/login",
    "/api/auth/login-form",
    "/api/auth/logout",
    "/health",
    "/api/agents/ws",
)


class CSRFSkipCheck:
    """标记路由不检查 CSRF（用于 WebSocket 等）。"""
    pass


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    """校验请求头的 ``X-CSRF-Token`` 与 cookie 的 ``csrf_token`` 一致。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # WebSocket 请求直接跳过（BaseHTTPMiddleware 不支持 WS）
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        settings = get_settings()
        if not settings.csrf_enabled:
            return await call_next(request)

        path = request.url.path
        # 白名单路径跳过
        if any(path.startswith(p) for p in _EXCLUDE_PATHS):
            return await call_next(request)

        # GET/HEAD/OPTIONS 跳过
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        # 校验 CSRF token
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("X-CSRF-Token", "")

        if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing or invalid"},
            )

        return await call_next(request)


__all__ = ["CSRFMiddleware", "generate_csrf_token"]
