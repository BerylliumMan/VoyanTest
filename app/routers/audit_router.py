"""审计日志路由 — 查看审计日志."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app import crud, models
from app.database import get_async_db
from app.auth import require_admin
from app.rate_limiter import limiter
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])


@router.get("/", response_model=models.AuditLogPage)
@limiter.limit("30/minute")
async def list_audit_logs(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin=Depends(require_admin),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user_id: int = Query(None),
    action: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
) -> models.AuditLogPage:
    # 解析 date_from / date_to，解析失败记 warning 但不阻塞查询
    parsed_date_from = None
    parsed_date_to = None
    if date_from:
        try:
            parsed_date_from = datetime.fromisoformat(date_from)
        except ValueError:
            logger.warning("无效的 date_from 参数: %s", date_from)
    if date_to:
        try:
            parsed_date_to = datetime.fromisoformat(date_to)
        except ValueError:
            logger.warning("无效的 date_to 参数: %s", date_to)

    result = await crud.list_audit_logs(
        db,
        user_id=user_id,
        action=action,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        page=page,
        size=size,
    )
    total = result["total"]
    logs = result["items"]

    # 关联用户名
    user_ids = {l.user_id for l in logs if l.user_id}
    users = {u.id: u.username for u in await crud.get_users_by_ids(db, list(user_ids))} if user_ids else {}

    items = []
    for l in logs:
        items.append(models.AuditLogResponse(
            id=l.id,
            user_id=l.user_id,
            username=users.get(l.user_id),
            action=l.action,
            details=l.details,
            ip_address=l.ip_address,
            created_at=l.created_at,
        ))

    return models.AuditLogPage(items=items, total=total, page=page, size=size)
