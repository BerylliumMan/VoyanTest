# app/crud/audit.py - 审计日志 CRUD
#
# 提供对 AuditLog 表的纯数据库查询操作。
# 审计日志的写入（``log_audit``）已由 ``app.auth`` 内部封装，
# 本文件只暴露路由所需的列表 / 计数 / 用户名批量查询接口。
import logging
from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)


def _apply_audit_filters(stmt, user_id: int | None, action: str | None, date_from: datetime | None, date_to: datetime | None):
    """对 AuditLog 查询应用过滤条件（与原 audit_router 行为一致）。

    - ``action`` 做 LIKE 通配符转义，防止意外匹配过多记录
    - ``date_from`` / ``date_to`` 已是 ISO 解析后的 datetime 对象，
      调用方负责 fromisoformat 的 ValueError 捕获
    """
    if user_id is not None:
        stmt = stmt.where(db_models.AuditLog.user_id == user_id)
    if action:
        # 转义 LIKE 通配符防止意外匹配过多记录
        escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(db_models.AuditLog.action.like(f"%{escaped}%", escape="\\"))
    if date_from:
        stmt = stmt.where(db_models.AuditLog.created_at >= date_from)
    if date_to:
        stmt = stmt.where(db_models.AuditLog.created_at <= date_to)
    return stmt


async def list_audit_logs(
    db: AsyncSession,
    user_id: int | None = None,
    action: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """分页查询审计日志（按 created_at 倒序）。

    返回 ``{"total": int, "items": list[AuditLog]}``，由 router 负责关联 username。
    """
    # 计数：select(func.count()).select_from(AuditLog).where(...)
    count_stmt = select(func.count()).select_from(db_models.AuditLog)
    count_stmt = _apply_audit_filters(count_stmt, user_id, action, date_from, date_to)
    total = (await db.execute(count_stmt)).scalar()

    # 列表：select(AuditLog).where(...).order_by(...).offset(...).limit(...)
    items_stmt = select(db_models.AuditLog)
    items_stmt = _apply_audit_filters(items_stmt, user_id, action, date_from, date_to)
    items_stmt = (
        items_stmt
        .order_by(desc(db_models.AuditLog.created_at))
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(items_stmt)
    items = result.scalars().all()
    return {"total": total, "items": items}
