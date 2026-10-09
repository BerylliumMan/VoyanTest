"""033（US3）：OAuth2 令牌获取与进程内缓存（client_credentials / password / refresh_token）。

契约见 ``specs/033-api-test-maturity/contracts/api-maturity.md`` §C3：
- 指纹 ``(token_url, client_id, scope, grant)`` 进程内缓存，TTL = expires_in − buffer；
- 并发同指纹加锁去重（同批次令牌端点 ≤1 次调用）；
- 失败抛可读 ``OAuthTokenError``（不静默、不重试）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_TTL_S = 300.0

__all__ = ["OAuthTokenError", "fetch_token", "clear_token_cache", "make_client"]


class OAuthTokenError(Exception):
    """令牌获取失败（携带可读原因）。"""


# 进程内缓存：fingerprint -> (valid_until_monotonic, token)
_cache: dict[tuple, tuple[float, str]] = {}
_locks: dict[tuple, asyncio.Lock] = {}


def make_client(*, timeout: float = 15.0) -> httpx.AsyncClient:
    """client 工厂（测试可 monkeypatch 注入 MockTransport）。"""
    return httpx.AsyncClient(timeout=timeout)


def clear_token_cache() -> None:
    """清空令牌缓存（测试/密钥轮换用）。"""
    _cache.clear()


def _fingerprint(cfg: dict) -> tuple:
    return (
        str(cfg.get("token_url") or ""),
        str(cfg.get("client_id") or ""),
        str(cfg.get("scope") or ""),
        str(cfg.get("grant") or "client_credentials"),
    )


def _get_lock(fp: tuple) -> asyncio.Lock:
    lock = _locks.get(fp)
    if lock is None:
        lock = asyncio.Lock()
        _locks[fp] = lock
    return lock


async def fetch_token(cfg: dict, *, buffer_s: int = 60, timeout_s: float = 15.0) -> str:
    """获取 access_token（缓存命中直接返回；否则请求 token 端点）。"""
    token_url = str(cfg.get("token_url") or "").strip()
    if not token_url:
        raise OAuthTokenError("缺少 token_url")

    fp = _fingerprint(cfg)
    cached = _cache.get(fp)
    if cached and time.monotonic() < cached[0]:
        logger.debug("OAuth2 令牌缓存命中: client_id=%s grant=%s", fp[1], fp[3])
        return cached[1]

    async with _get_lock(fp):
        cached = _cache.get(fp)  # 双重检查（并发等待者不重复请求）
        if cached and time.monotonic() < cached[0]:
            logger.debug("OAuth2 令牌缓存命中（锁内）: client_id=%s grant=%s", fp[1], fp[3])
            return cached[1]

        token, expires_in = await _request_token(cfg, token_url, timeout_s)
        ttl = expires_in if expires_in is not None else DEFAULT_TOKEN_TTL_S
        valid_window = ttl - max(0, int(buffer_s))
        if valid_window > 0:
            _cache[fp] = (time.monotonic() + valid_window, token)
        logger.info(
            "OAuth2 令牌已获取（缓存未命中）: client_id=%s grant=%s token_url=%s expires_in=%s",
            fp[1], fp[3], fp[0], expires_in,
        )
        return token


async def _request_token(cfg: dict, token_url: str, timeout_s: float) -> tuple[str, Optional[float]]:
    grant = str(cfg.get("grant") or "client_credentials")
    client_id = str(cfg.get("client_id") or "")
    client_secret = str(cfg.get("client_secret") or "")
    scope = str(cfg.get("scope") or "")

    data: dict[str, str] = {"grant_type": grant}
    if scope:
        data["scope"] = scope

    use_basic = str(cfg.get("client_auth") or "body").lower() == "basic"
    auth: Optional[tuple] = None
    if use_basic:
        auth = (client_id, client_secret)
    else:
        if client_id:
            data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret

    if grant == "password":
        data["username"] = str(cfg.get("username") or "")
        data["password"] = str(cfg.get("password") or "")
    elif grant == "refresh_token":
        data["refresh_token"] = str(cfg.get("refresh_token") or "")

    try:
        async with make_client(timeout=timeout_s) as client:
            resp = await client.post(
                token_url,
                data=data,
                auth=auth,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OAuthTokenError(f"token 端点不可达: {type(exc).__name__}: {exc}") from None

    if resp.status_code >= 400:
        raise OAuthTokenError(f"token 端点返回 HTTP {resp.status_code}: {resp.text[:120]}")

    try:
        payload = resp.json()
    except ValueError:
        raise OAuthTokenError("token 端点响应不是 JSON") from None
    if not isinstance(payload, dict):
        raise OAuthTokenError("token 端点响应格式非法")

    token = str(payload.get("access_token") or "")
    if not token:
        raise OAuthTokenError("token 端点响应缺少 access_token")

    expires_in: Optional[float] = None
    raw_expires = payload.get("expires_in")
    if raw_expires is not None:
        try:
            expires_in = float(raw_expires)
        except (TypeError, ValueError):
            expires_in = None
    return token, expires_in
