"""050 US3：我的待办聚合（跨项目，按负责人）。"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models


async def list_my_tasks(
    db: AsyncSession,
    user,
    *,
    filter: str = "todo",
    project_id: int | None = None,
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    if filter not in ("todo", "overdue", "done"):
        raise HTTPException(status_code=422, detail="filter 只能是 todo/overdue/done")
    page = max(1, int(page))
    size = min(200, max(1, int(size)))
    today = date.today()

    base = (
        select(db_models.TestPlanItem, db_models.TestPlan, db_models.Project)
        .join(db_models.TestPlan, db_models.TestPlan.id == db_models.TestPlanItem.plan_id)
        .join(db_models.Project, db_models.Project.id == db_models.TestPlan.project_id)
        .where(
            db_models.TestPlanItem.assignee_id == int(user.id),
            db_models.TestPlan.status != "archived",
        )
    )
    if project_id is not None:
        base = base.where(db_models.TestPlan.project_id == int(project_id))
    if filter == "done":
        base = base.where(db_models.TestPlanItem.status == "passed")
    elif filter == "overdue":
        base = base.where(
            db_models.TestPlanItem.status != "passed",
            db_models.TestPlanItem.due_date.is_not(None),
            db_models.TestPlanItem.due_date < today,
        )
    else:
        base = base.where(db_models.TestPlanItem.status != "passed")

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(
                db_models.TestPlanItem.due_date.is_(None),
                db_models.TestPlanItem.due_date,
                db_models.TestPlanItem.id,
            )
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()

    display = getattr(user, "nickname", None) or getattr(user, "username", "") or ""
    items = []
    for item, plan, project in rows:
        items.append(
            {
                "plan_item_id": item.id,
                "plan_id": plan.id,
                "plan_name": plan.name,
                "project_id": project.id,
                "project_name": project.name,
                "case_id": item.case_id,
                "case_name": item.case_name,
                "status": item.status,
                "priority": item.priority,
                "due_date": item.due_date.isoformat() if item.due_date else None,
                "overdue": bool(
                    item.due_date and item.due_date < today and item.status != "passed"
                ),
                "assignee_id": item.assignee_id,
                "assignee_name": display,
            }
        )
    return {"total": int(total), "items": items, "page": page, "size": size}


__all__ = ["list_my_tasks"]
