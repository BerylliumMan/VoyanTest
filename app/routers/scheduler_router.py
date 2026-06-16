from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Depends
from typing import List
from sqlalchemy.orm import Session
from .. import models
from app.auth import require_admin, get_current_user, get_user_project_filter
from ..database import get_db
from ..db_models import ScheduledTask as ScheduledTaskDB
from .. import db_models
from datetime import datetime
from app.tz import now as tz_now
from croniter import croniter

logger = logging.getLogger(__name__)


def _resolve_task_project_id(db: Session, task_type: str, target_id: int) -> int | None:
    """根据任务类型解析目标的所属项目 ID。"""
    if task_type == "project":
        return target_id
    if task_type == "module":
        module = db.query(db_models.Module).filter(db_models.Module.id == target_id).first()
        return module.project_id if module else None
    if task_type == "testcase":
        case = db.query(db_models.TestCase).filter(db_models.TestCase.id == target_id).first()
        return case.project_id if case else None
    return None


router = APIRouter(
    prefix="/api",
    tags=["定时任务"],
)


@router.get("/schedules", response_model=List[models.Schedule])
def list_schedules(db: Session = Depends(get_db)) -> list[models.Schedule]:
    """获取所有定时任务"""
    return db.query(ScheduledTaskDB).order_by(ScheduledTaskDB.created_at.desc()).all()


@router.post("/schedules", response_model=models.Schedule)
def create_schedule(
    schedule: models.ScheduleCreate,
    user=Depends(get_current_user),
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> models.Schedule:
    """创建定时任务"""
    try:
        # 验证任务目标的项目访问权限
        allowed_ids = get_user_project_filter(user)
        if allowed_ids is not None:
            target_project_id = _resolve_task_project_id(db, schedule.task_type, schedule.target_id)
            if target_project_id is not None and target_project_id not in allowed_ids:
                raise HTTPException(status_code=403, detail="无权为该目标创建定时任务")

        if not croniter.is_valid(schedule.cron_expression):
            raise HTTPException(status_code=400, detail="Invalid cron expression")

        itr = croniter(schedule.cron_expression, tz_now())
        next_run = itr.get_next(datetime)

        db_schedule = ScheduledTaskDB(
            name=schedule.name,
            cron_expression=schedule.cron_expression,
            task_type=schedule.task_type,
            target_id=schedule.target_id,
            enabled=schedule.enabled,
            description=schedule.description or "",
            next_run_at=next_run,
        )
        db.add(db_schedule)
        db.commit()
        db.refresh(db_schedule)
        return db_schedule
    except HTTPException:
        raise
    except Exception as e:
        logger.error("创建定时任务失败: %s", e)
        raise HTTPException(status_code=400, detail="Could not create schedule")


@router.put("/schedules/{schedule_id}", response_model=models.Schedule)
def update_schedule(
    schedule_id: int,
    schedule: models.ScheduleUpdate,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> models.Schedule:
    """更新定时任务"""
    db_schedule = db.query(ScheduledTaskDB).filter(ScheduledTaskDB.id == schedule_id).first()
    if db_schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    try:
        update_data = schedule.model_dump(exclude_unset=True)
        if "cron_expression" in update_data:
            if not croniter.is_valid(update_data["cron_expression"]):
                raise HTTPException(status_code=400, detail="Invalid cron expression")
            itr = croniter(update_data["cron_expression"], tz_now())
            update_data["next_run_at"] = itr.get_next(datetime)

        for key, value in update_data.items():
            setattr(db_schedule, key, value)

        db_schedule.updated_at = tz_now()
        db.commit()
        db.refresh(db_schedule)
        return db_schedule
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新定时任务失败 (id=%d): %s", schedule_id, e)
        raise HTTPException(status_code=400, detail="Could not update schedule")


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """删除定时任务"""
    db_schedule = db.query(ScheduledTaskDB).filter(ScheduledTaskDB.id == schedule_id).first()
    if db_schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    try:
        db.delete(db_schedule)
        db.commit()
        return {"detail": "Schedule deleted"}
    except Exception as e:
        logger.error("删除定时任务失败 (id=%d): %s", schedule_id, e)
        raise HTTPException(status_code=400, detail="Could not delete schedule")


@router.put("/schedules/{schedule_id}/toggle", response_model=models.Schedule)
def toggle_schedule(
    schedule_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> models.Schedule:
    """启用/禁用定时任务"""
    db_schedule = db.query(ScheduledTaskDB).filter(ScheduledTaskDB.id == schedule_id).first()
    if db_schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    try:
        db_schedule.enabled = not db_schedule.enabled
        db_schedule.updated_at = tz_now()
        if db_schedule.enabled:
            itr = croniter(db_schedule.cron_expression, tz_now())
            db_schedule.next_run_at = itr.get_next(datetime)
        db.commit()
        db.refresh(db_schedule)
        return db_schedule
    except Exception as e:
        logger.error("切换定时任务状态失败 (id=%d): %s", schedule_id, e)
        raise HTTPException(status_code=400, detail="Could not toggle schedule")
