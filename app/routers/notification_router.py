"""通知中心路由 — 列表/标记已读/全部已读。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user
from app.database import get_async_db

router = APIRouter(prefix="/api/notifications", tags=["通知"])


@router.get("/")
async def list_notifications(
    page: int = 1,
    size: int = 20,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """获取当前用户的通知列表（按时间倒序）。"""
    items = await db.execute(
        select(db_models.Notification)
        .where(db_models.Notification.user_id == user.id)
        .order_by(db_models.Notification.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    total = await db.execute(
        select(func.count()).select_from(db_models.Notification).where(db_models.Notification.user_id == user.id)
    )
    return {
        "items": [{
            "id": n.id, "type": n.type, "title": n.title,
            "message": n.message, "read": n.read,
            "batch_id": n.batch_id,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        } for n in items.scalars().all()],
        "total": total.scalar() or 0,
    }


@router.get("/unread-count")
async def unread_count(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """获取未读通知数。"""
    result = await db.execute(
        select(func.count()).select_from(db_models.Notification).where(
            db_models.Notification.user_id == user.id,
            db_models.Notification.read == False,
        )
    )
    return {"count": result.scalar() or 0}


@router.put("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """标记单条通知为已读。"""
    await db.execute(
        update(db_models.Notification)
        .where(db_models.Notification.id == notification_id, db_models.Notification.user_id == user.id)
        .values(read=True)
    )
    await db.commit()
    return {"ok": True}


@router.put("/read-all")
async def mark_all_read(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """标记所有通知为已读。"""
    await db.execute(
        update(db_models.Notification)
        .where(db_models.Notification.user_id == user.id, db_models.Notification.read == False)
        .values(read=True)
    )
    await db.commit()
    return {"ok": True}
