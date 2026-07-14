# app/crud/schedule.py - 定时任务（ScheduledTask）CRUD
#
# 提供对 ScheduledTask 表的纯数据库操作。
# 业务层面的 cron 解析（``croniter``）和 ``next_run_at`` 计算由 router 负责，
# 本文件只关心 SQLAlchemy 查询/写入/删除。
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.tz import now as tz_now

logger = logging.getLogger(__name__)


# ----------------------------
# ScheduledTask CRUD
# ----------------------------

async def list_scheduled_tasks(db: AsyncSession) -> list[db_models.ScheduledTask]:
    """获取所有定时任务（按 created_at 倒序）。"""
    result = await db.execute(
        select(db_models.ScheduledTask)
        .order_by(db_models.ScheduledTask.created_at.desc())
    )
    return result.scalars().all()


async def get_scheduled_task(db: AsyncSession, schedule_id: int) -> db_models.ScheduledTask | None:
    """通过 ID 获取定时任务，不存在返回 None。"""
    result = await db.execute(
        select(db_models.ScheduledTask)
        .where(db_models.ScheduledTask.id == schedule_id)
    )
    return result.scalar_one_or_none()


async def create_scheduled_task(
    db: AsyncSession,
    name: str,
    cron_expression: str,
    task_type: str,
    target_id: int,
    enabled: bool,
    description: str = "",
    next_run_at=None,
) -> db_models.ScheduledTask:
    """创建定时任务。

    ``next_run_at`` 由 router 通过 ``croniter`` 计算后传入。
    """
    db_schedule = db_models.ScheduledTask(
        name=name,
        cron_expression=cron_expression,
        task_type=task_type,
        target_id=target_id,
        enabled=enabled,
        description=description or "",
        next_run_at=next_run_at,
    )
    db.add(db_schedule)
    try:
        await db.commit()
        await db.refresh(db_schedule)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"创建定时任务失败: {e}") from e
    return db_schedule


async def update_scheduled_task(
    db: AsyncSession,
    schedule_id: int,
    name: str | None = None,
    cron_expression: str | None = None,
    task_type: str | None = None,
    target_id: int | None = None,
    enabled: bool | None = None,
    description: str | None = None,
    next_run_at=None,
) -> db_models.ScheduledTask | None:
    """部分更新定时任务（仅更新非 None 字段），自动刷新 updated_at。

    返回更新后的 ORM 对象；若 schedule 不存在返回 None，由 router 决定抛 404。
    业务层面的 cron 校验和 next_run_at 重新计算由 router 负责。
    """
    db_schedule = await get_scheduled_task(db, schedule_id)
    if db_schedule is None:
        return None

    update_fields = {
        "name": name,
        "cron_expression": cron_expression,
        "task_type": task_type,
        "target_id": target_id,
        "enabled": enabled,
        "description": description,
        "next_run_at": next_run_at,
    }
    for key, value in update_fields.items():
        if value is not None:
            setattr(db_schedule, key, value)

    db_schedule.updated_at = tz_now()
    try:
        await db.commit()
        await db.refresh(db_schedule)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"更新定时任务失败: {e}") from e
    return db_schedule


async def delete_scheduled_task(db: AsyncSession, schedule_id: int) -> bool:
    """删除定时任务。返回是否成功删除（schedule 存在并删除返回 True）。"""
    db_schedule = await get_scheduled_task(db, schedule_id)
    if db_schedule is None:
        return False
    await db.delete(db_schedule)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise ValueError(f"删除定时任务失败: {e}") from e
    return True


async def toggle_scheduled_task(
    db: AsyncSession,
    schedule_id: int,
    next_run_at=None,
) -> db_models.ScheduledTask | None:
    """切换定时任务的 enabled 状态（True ↔ False）。

    - 启用时：调用方负责把新的 next_run_at 传进来（``croniter`` 计算结果）
    - 禁用时：next_run_at 传 None，函数会跳过该字段的更新

    返回更新后的 ORM 对象；schedule 不存在返回 None。
    """
    db_schedule = await get_scheduled_task(db, schedule_id)
    if db_schedule is None:
        return None

    db_schedule.enabled = not db_schedule.enabled
    db_schedule.updated_at = tz_now()
    if db_schedule.enabled and next_run_at is not None:
        db_schedule.next_run_at = next_run_at

    try:
        await db.commit()
        await db.refresh(db_schedule)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"切换定时任务状态失败: {e}") from e
    return db_schedule
