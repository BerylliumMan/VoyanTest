"""048：接口用例鉴权辅助（登录接口查找）。

生成器与自动补用例共用同一套规则，避免行为分叉。
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import api_definition as crud_api_def


def needs_auth(definition: dict) -> bool:
    """definition 是否声明了鉴权要求（解析器写入的 request_schema.security）。"""
    security = (definition.get("request_schema") or {}).get("security")
    return bool(security)


def login_candidate(row) -> dict | None:
    """把 api_definitions 行转成生成器需要的 login 定义（非 POST 或带鉴权的不算）。"""
    if str(row.method or "").upper() != "POST":
        return None
    if needs_auth({"request_schema": row.request_schema or {}}):
        return None  # 登录接口自己不该要求鉴权
    body_text = json.dumps(row.request_schema or {}, ensure_ascii=False).lower()
    path = str(row.path or "").lower()
    looks_like_login = (
        "password" in body_text
        or path.endswith(("login", "signin", "token", "auth"))
        or "/login" in path
    )
    if not looks_like_login:
        return None
    return {
        "id": row.id,
        "name": row.name,
        "method": row.method,
        "path": row.path,
        "request_schema": row.request_schema or {},
    }


async def find_login_definition(
    db: AsyncSession, project_id: int, definitions: list | None = None
) -> dict | None:
    """在项目里找登录接口定义（优先后端字段含 password 的 POST）。"""
    for row in await crud_api_def.list_api_definitions(db, project_id, limit=500):
        candidate = login_candidate(row)
        if candidate and "password" in json.dumps(
            candidate["request_schema"], ensure_ascii=False
        ).lower():
            return candidate
    for row in definitions or []:  # 兜底：目标定义里就有登录接口
        candidate = login_candidate(row)
        if candidate:
            return candidate
    return None


__all__ = ["login_candidate", "find_login_definition", "needs_auth"]
