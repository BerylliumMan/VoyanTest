# app/crud/api_token.py — CI 触发令牌（031-api-testing-enhancements US8）
#
# 契约（data-model §1.4、contracts §1.6）：
#   · 明文形如 ``vt_<urlsafe 32>``，**仅创建响应返回一次**；库里只存 SHA-256 哈希与 8 位展示前缀
#   · 解析时校验：撤销（revoked_at）与过期（expires_at）一律失效；解析成功刷新 last_used_at
#   · 令牌携带项目范围 project_id（NULL = 全部可见项目），鉴权层用它限权
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_test import ApiToken
from app.tz import now as tz_now

TOKEN_PREFIX = "vt_"
_HASH_LEN = 64


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(str(plaintext).encode("utf-8")).hexdigest()


def generate_plaintext() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


async def create_token(
    db: AsyncSession,
    *,
    name: str,
    project_id: Optional[int] = None,
    created_by: Optional[int] = None,
    expires_in_days: Optional[int] = None,
) -> tuple[ApiToken, str]:
    """创建令牌；返回 (DB 行, 明文)。明文只在这里产生一次。"""
    plaintext = generate_plaintext()
    expires_at = None
    if expires_in_days:
        expires_at = tz_now() + timedelta(days=int(expires_in_days))
    token = ApiToken(
        name=str(name or "CI 令牌")[:100],
        token_hash=hash_token(plaintext),
        token_prefix=plaintext[:8],
        project_id=project_id,
        created_by=created_by,
        expires_at=expires_at,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return token, plaintext


async def list_tokens(db: AsyncSession, *, created_by: Optional[int] = None) -> list[ApiToken]:
    stmt = select(ApiToken).order_by(ApiToken.id.desc())
    if created_by is not None:
        stmt = stmt.where(ApiToken.created_by == created_by)
    return list((await db.execute(stmt)).scalars().all())


async def revoke_token(db: AsyncSession, token_id: int) -> bool:
    token = await db.get(ApiToken, token_id)
    if token is None:
        return False
    if token.revoked_at is None:
        token.revoked_at = tz_now()
        await db.commit()
    return True


async def resolve_token(db: AsyncSession, plaintext: str) -> Optional[ApiToken]:
    """按明文解析令牌；无效（未知/撤销/过期）返回 None；有效则刷新 last_used_at。"""
    raw = str(plaintext or "").strip()
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    token = (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    if token.expires_at is not None:
        expires = token.expires_at
        now = tz_now()
        # 兼容无时区的历史值（SQLite 测试库）
        if expires.tzinfo is None:
            now = now.replace(tzinfo=None)
        if expires < now:
            return None
    token.last_used_at = tz_now()
    await db.commit()
    return token


def to_public_dict(token: ApiToken) -> dict:
    """对外形状（不含哈希与明文）。"""
    return {
        "id": token.id,
        "name": token.name,
        "token_prefix": token.token_prefix,
        "project_id": token.project_id,
        "created_by": token.created_by,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "created_at": token.created_at.isoformat() if token.created_at else None,
    }


__all__ = [
    "TOKEN_PREFIX",
    "create_token",
    "generate_plaintext",
    "hash_token",
    "list_tokens",
    "resolve_token",
    "revoke_token",
    "to_public_dict",
]
