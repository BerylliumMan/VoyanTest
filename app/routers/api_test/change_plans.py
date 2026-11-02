"""046：变更回归计划列表 / 确认 / 取消。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db
from app.models.api_extract import ApiChangeTrigger
from app.models.quality import QualityRegressionPlan
from app.auth import log_audit
from app.services.api_change_plans import (
    cancel_trigger,
    confirm_trigger,
    execute_claimed_trigger,
    spawn_background,
)
from app.tz import now as tz_now

logger = logging.getLogger(__name__)

router = APIRouter()


class CancelBody(BaseModel):
    reason: str | None = None


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


def _serialize(trigger: ApiChangeTrigger, plan: QualityRegressionPlan | None) -> dict:
    now = tz_now()
    deadline = trigger.deadline_at
    if deadline is not None and deadline.tzinfo is None:
        from app.tz import CST

        deadline = deadline.replace(tzinfo=CST)
    return {
        "id": trigger.id,
        "project_id": trigger.project_id,
        "plan_id": trigger.plan_id,
        "plan_name": plan.name if plan is not None else None,
        "status": trigger.status,
        "decision": trigger.decision,
        "from_commit": trigger.from_commit,
        "to_commit": trigger.to_commit,
        "case_count": trigger.case_count,
        "affected": trigger.affected or [],
        "deadline_at": deadline.isoformat() if deadline else None,
        "seconds_left": max(0, int((deadline - now).total_seconds())) if deadline else None,
        "retry_count": trigger.retry_count,
        "retry_at": trigger.retry_at.isoformat() if trigger.retry_at else None,
        "batch_id": trigger.batch_id,
        "reason": trigger.reason,
        "created_at": trigger.created_at.isoformat() if trigger.created_at else None,
        "decided_at": trigger.decided_at.isoformat() if trigger.decided_at else None,
        "decided_by": trigger.decided_by,
    }


async def _load_plans(
    db: AsyncSession, triggers: list[ApiChangeTrigger]
) -> dict[int, QualityRegressionPlan]:
    ids = {int(t.plan_id) for t in triggers if t.plan_id}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(QualityRegressionPlan).where(QualityRegressionPlan.id.in_(ids))
        )
    ).scalars().all()
    return {int(r.id): r for r in rows}


async def _get_trigger(db: AsyncSession, trigger_id: int) -> ApiChangeTrigger:
    trigger = await db.get(ApiChangeTrigger, trigger_id)
    if trigger is None:
        raise HTTPException(status_code=404, detail="变更计划不存在")
    return trigger


@router.get("/change-plans")
async def list_change_plans(
    project_id: int,
    status: str | None = None,
    limit: int = 50,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    _ensure_project_access(user, project_id)
    limit = max(1, min(limit, 200))
    stmt = select(ApiChangeTrigger).where(ApiChangeTrigger.project_id == project_id)
    if status:
        stmt = stmt.where(ApiChangeTrigger.status == status)
    triggers = (
        await db.execute(stmt.order_by(ApiChangeTrigger.id.desc()).limit(limit))
    ).scalars().all()
    plans = await _load_plans(db, list(triggers))
    return {
        "items": [_serialize(t, plans.get(int(t.plan_id)) if t.plan_id else None) for t in triggers]
    }


@router.post("/change-plans/{trigger_id}/confirm")
async def confirm_change_plan(
    trigger_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    trigger = await _get_trigger(db, trigger_id)
    _ensure_project_access(user, trigger.project_id)
    if trigger.status != "pending":
        raise HTTPException(status_code=409, detail=f"当前状态不可确认：{trigger.status}")

    ok = await confirm_trigger(db, trigger_id, user_id=getattr(user, "id", None))
    await db.commit()
    if not ok:
        raise HTTPException(status_code=409, detail="计划已被其他操作处理")

    spawn_background(execute_claimed_trigger(trigger_id))
    await log_audit(
        db,
        getattr(user, "id", None),
        "api_change_plan.confirm",
        {"project_id": trigger.project_id, "trigger_id": trigger_id},
    )
    return {"id": trigger_id, "status": "running", "decision": "confirmed"}


@router.post("/change-plans/{trigger_id}/cancel")
async def cancel_change_plan(
    trigger_id: int,
    body: CancelBody | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    trigger = await _get_trigger(db, trigger_id)
    _ensure_project_access(user, trigger.project_id)

    ok = await cancel_trigger(
        db,
        trigger_id,
        user_id=getattr(user, "id", None),
        reason=(body.reason if body else None),
    )
    await db.commit()
    if not ok:
        raise HTTPException(status_code=409, detail=f"当前状态不可取消：{trigger.status}")
    await log_audit(
        db,
        getattr(user, "id", None),
        "api_change_plan.cancel",
        {
            "project_id": trigger.project_id,
            "trigger_id": trigger_id,
            "reason": (body.reason if body else None),
        },
    )
    return {"id": trigger_id, "status": "cancelled"}
