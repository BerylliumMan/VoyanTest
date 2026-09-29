# app/crud/api_history.py — 调试请求历史（031-api-testing-enhancements T028）
#
# 契约（contracts §1.2/§1.3）：
#   · 每次真实调试发送（非 dry_run）写一行；传输失败也照录（status_code=NULL + error 摘要）
#   · 每用户保留上限 200 条（写后修剪，防止无限增长）
#   · 按用户隔离；列表按 id 倒序（最新在前），支持 before_id 游标分页
#   · headers_masked/body_preview 均为脱敏后内容（调用方负责，见 app/routers/api_test/debug.py）
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)

MAX_HISTORY_PER_USER = 200
DEFAULT_LIMIT = 50


async def add_history(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: Optional[int],
    method: str,
    url: str,
    headers_masked: Optional[dict] = None,
    body_preview: Optional[str] = None,
    status_code: Optional[int] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> db_models.ApiRequestHistory:
    """写入一条历史并修剪（每用户最多 MAX_HISTORY_PER_USER 条）。"""
    row = db_models.ApiRequestHistory(
        user_id=user_id,
        project_id=project_id,
        method=str(method or "GET")[:16],
        url=str(url or "")[:4000],
        headers_masked=headers_masked or {},
        body_preview=(body_preview or None),
        status_code=status_code,
        duration_ms=duration_ms,
        error=(error or None),
    )
    db.add(row)
    await db.flush()
    await _trim(db, user_id)
    await db.commit()
    await db.refresh(row)
    return row


async def _trim(db: AsyncSession, user_id: int) -> int:
    """删除超出上限的最旧记录；返回删除条数。"""
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(db_models.ApiRequestHistory)
                .where(db_models.ApiRequestHistory.user_id == user_id)
            )
        ).scalar()
        or 0
    )
    if total <= MAX_HISTORY_PER_USER:
        return 0
    keep_ids = (
        select(db_models.ApiRequestHistory.id)
        .where(db_models.ApiRequestHistory.user_id == user_id)
        .order_by(db_models.ApiRequestHistory.id.desc())
        .limit(MAX_HISTORY_PER_USER)
    )
    result = await db.execute(
        delete(db_models.ApiRequestHistory).where(
            db_models.ApiRequestHistory.user_id == user_id,
            db_models.ApiRequestHistory.id.not_in(keep_ids),
        )
    )
    return int(result.rowcount or 0)


async def list_history(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    before_id: Optional[int] = None,
) -> list[dict]:
    """倒序返回历史（最新在前）；before_id 作为游标翻页。"""
    stmt = select(db_models.ApiRequestHistory).where(
        db_models.ApiRequestHistory.user_id == user_id
    )
    if project_id is not None:
        stmt = stmt.where(db_models.ApiRequestHistory.project_id == project_id)
    if before_id is not None:
        stmt = stmt.where(db_models.ApiRequestHistory.id < before_id)
    stmt = stmt.order_by(db_models.ApiRequestHistory.id.desc()).limit(
        max(1, min(int(limit or DEFAULT_LIMIT), 200))
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [_to_dict(r) for r in rows]


def _to_dict(row: db_models.ApiRequestHistory) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "method": row.method,
        "url": row.url,
        "status_code": row.status_code,
        "duration_ms": row.duration_ms,
        "headers_masked": row.headers_masked or {},
        "body_preview": row.body_preview,
        "error": row.error,
        "created_at": row.created_at,
    }


async def clear_history(
    db: AsyncSession, *, user_id: int, project_id: Optional[int] = None
) -> int:
    """清空当前用户历史（可按项目限定）；返回删除条数。"""
    stmt = delete(db_models.ApiRequestHistory).where(
        db_models.ApiRequestHistory.user_id == user_id
    )
    if project_id is not None:
        stmt = stmt.where(db_models.ApiRequestHistory.project_id == project_id)
    result = await db.execute(stmt)
    await db.commit()
    return int(result.rowcount or 0)
