"""审计日志路由 — 查看审计日志."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app import db_models, models
from app.database import get_db
from app.auth import require_admin
from app.rate_limiter import limiter
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])


@router.get("/", response_model=models.AuditLogPage)
@limiter.limit("30/minute")
def list_audit_logs(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user_id: int = Query(None),
    action: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
) -> models.AuditLogPage:
    q = db.query(db_models.AuditLog)
    if user_id is not None:
        q = q.filter(db_models.AuditLog.user_id == user_id)
    if action:
        # 转义 LIKE 通配符防止意外匹配过多记录
        escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.filter(db_models.AuditLog.action.like(f"%{escaped}%", escape="\\"))
    if date_from:
        try:
            dt = datetime.fromisoformat(date_from)
            q = q.filter(db_models.AuditLog.created_at >= dt)
        except ValueError:
            logger.warning("无效的 date_from 参数: %s", date_from)
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            q = q.filter(db_models.AuditLog.created_at <= dt)
        except ValueError:
            logger.warning("无效的 date_to 参数: %s", date_to)

    total = q.count()
    logs = q.order_by(desc(db_models.AuditLog.created_at)).offset((page - 1) * size).limit(size).all()

    # 关联用户名
    user_ids = {l.user_id for l in logs if l.user_id}
    users = {u.id: u.username for u in db.query(db_models.User).filter(db_models.User.id.in_(user_ids)).all()} if user_ids else {}

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
