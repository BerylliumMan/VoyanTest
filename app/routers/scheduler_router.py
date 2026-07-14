from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Depends
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from .. import crud, models
from app.auth import require_admin, get_current_user, get_user_project_filter
from app.database import get_async_db
from datetime import datetime
from app.tz import now as tz_now
from croniter import croniter

logger = logging.getLogger(__name__)


async def _resolve_task_project_id(db: AsyncSession, task_type: str, target_id: int) -> int | None:
    """根据任务类型解析目标的所属项目 ID。"""
    if task_type == "project":
        return target_id
    if task_type == "module":
        module = await crud.get_module(db, target_id)
        return module.project_id if module else None
    if task_type == "testcase":
        case = await crud.get_test_case(db, target_id)
        return case.project_id if case else None
    return None


router = APIRouter(
    prefix="/api",
    tags=["定时任务"],
)


@router.get("/schedules", response_model=List[models.Schedule])
async def list_schedules(db: AsyncSession = Depends(get_async_db)) -> list[models.Schedule]:
    """获取所有定时任务"""
    return await crud.list_scheduled_tasks(db)


@router.post("/schedules", response_model=models.Schedule)
async def create_schedule(
    schedule: models.ScheduleCreate,
    user=Depends(get_current_user),
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
) -> models.Schedule:
    """创建定时任务"""
    try:
        # 验证任务目标的项目访问权限
        allowed_ids = get_user_project_filter(user)
        if allowed_ids is not None:
            target_project_id = await _resolve_task_project_id(db, schedule.task_type, schedule.target_id)
            if target_project_id is not None and target_project_id not in allowed_ids:
                raise HTTPException(status_code=403, detail="无权为该目标创建定时任务")

        if not croniter.is_valid(schedule.cron_expression):
            raise HTTPException(status_code=400, detail="Invalid cron expression")

        itr = croniter(schedule.cron_expression, tz_now())
        next_run = itr.get_next(datetime)

        return await crud.create_scheduled_task(
            db,
            name=schedule.name,
            cron_expression=schedule.cron_expression,
            task_type=schedule.task_type,
            target_id=schedule.target_id,
            enabled=schedule.enabled,
            description=schedule.description or "",
            next_run_at=next_run,
        )
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError):
        # ValueError: croniter.CroniterBadCronError；SQLAlchemyError: 数据库写入失败
        logger.exception("创建定时任务失败")
        raise HTTPException(status_code=400, detail="Could not create schedule")


@router.put("/schedules/{schedule_id}", response_model=models.Schedule)
async def update_schedule(
    schedule_id: int,
    schedule: models.ScheduleUpdate,
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
) -> models.Schedule:
    """更新定时任务"""
    # 先检查存在性
    if await crud.get_scheduled_task(db, schedule_id) is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    try:
        update_data = schedule.model_dump(exclude_unset=True)
        next_run_at = None
        if "cron_expression" in update_data:
            if not croniter.is_valid(update_data["cron_expression"]):
                raise HTTPException(status_code=400, detail="Invalid cron expression")
            itr = croniter(update_data["cron_expression"], tz_now())
            next_run_at = itr.get_next(datetime)

        result = await crud.update_scheduled_task(
            db,
            schedule_id,
            name=update_data.get("name"),
            cron_expression=update_data.get("cron_expression"),
            task_type=update_data.get("task_type"),
            target_id=update_data.get("target_id"),
            enabled=update_data.get("enabled"),
            description=update_data.get("description"),
            next_run_at=next_run_at,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return result
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError):
        # ValueError: croniter.CroniterBadCronError；SQLAlchemyError: 数据库写入失败
        logger.exception("更新定时任务失败 (id=%d)", schedule_id)
        raise HTTPException(status_code=400, detail="Could not update schedule")


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: int,
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """删除定时任务"""
    try:
        success = await crud.delete_scheduled_task(db, schedule_id)
        if not success:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return {"detail": "Schedule deleted"}
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError):
        # ValueError: CRUD 包装的错误；SQLAlchemyError: 数据库写入失败
        logger.exception("删除定时任务失败 (id=%d)", schedule_id)
        raise HTTPException(status_code=400, detail="Could not delete schedule")


@router.put("/schedules/{schedule_id}/toggle", response_model=models.Schedule)
async def toggle_schedule(
    schedule_id: int,
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
) -> models.Schedule:
    """启用/禁用定时任务"""
    try:
        # 先读出当前 cron 表达式以便重新计算 next_run_at
        current = await crud.get_scheduled_task(db, schedule_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Schedule not found")

        next_run_at = None
        # 即将切换为启用时需要重新计算 next_run_at
        if not current.enabled:
            itr = croniter(current.cron_expression, tz_now())
            next_run_at = itr.get_next(datetime)

        result = await crud.toggle_scheduled_task(db, schedule_id, next_run_at=next_run_at)
        if result is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return result
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError):
        # ValueError: croniter.CroniterBadCronError；SQLAlchemyError: 数据库写入失败
        logger.exception("切换定时任务状态失败 (id=%d)", schedule_id)
        raise HTTPException(status_code=400, detail="Could not toggle schedule")
