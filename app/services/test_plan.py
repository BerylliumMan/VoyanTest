"""050 US1：测试计划服务（CRUD、批量加用例、进度统计）。

所有函数均为 async，调用方负责事务边界（本模块内部 commit）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.tz import now as tz_now

PLAN_STATUSES = {"draft", "active", "completed", "archived"}
ITEM_STATUSES = {"not_started", "in_progress", "passed", "failed", "blocked"}
PRIORITIES = {"low", "medium", "high", "critical"}

_EMPTY_COUNTS = {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "not_started": 0,
    "in_progress": 0,
    "blocked": 0,
    "progress": 0.0,
}


# ----------------------------
# 权限 / 校验
# ----------------------------

def ensure_project_access(user, project_id: int) -> None:
    """admin 放行；tester 必须在 project_ids 内。"""
    if getattr(user, "role", None) == "admin":
        return
    allowed = [int(x) for x in (getattr(user, "project_ids", None) or [])]
    if int(project_id) not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def list_project_members(db: AsyncSession, user, project_id: int) -> list[dict[str, Any]]:
    """可指派成员：admin 或属于该项目的 tester（前端选择负责人/评审人）。"""
    ensure_project_access(user, project_id)
    rows = (await db.execute(select(db_models.User))).scalars().all()
    out = []
    for u in rows:
        if getattr(u, "status", "active") == "disabled":
            continue
        if getattr(u, "role", None) == "admin" or int(project_id) in [
            int(x) for x in (getattr(u, "project_ids", None) or [])
        ]:
            out.append({
                "id": u.id,
                "username": u.username,
                "nickname": u.nickname,
                "role": u.role,
            })
    out.sort(key=lambda x: (x["role"] != "admin", x["username"]))
    return out


async def _validate_assignee(db: AsyncSession, user_id: int | None, project_id: int) -> None:
    if user_id is None:
        return
    target = await db.get(db_models.User, int(user_id))
    if target is None or getattr(target, "status", "active") == "disabled":
        raise HTTPException(status_code=400, detail="负责人不存在或已禁用")
    if getattr(target, "role", None) == "admin":
        return
    allowed = [int(x) for x in (getattr(target, "project_ids", None) or [])]
    if int(project_id) not in allowed:
        raise HTTPException(status_code=400, detail="负责人不是该项目成员")


def _plan_dict(plan, stats: dict[str, Any], *, cycle_count: int | None = None,
               last_cycle: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "id": plan.id,
        "project_id": plan.project_id,
        "name": plan.name,
        "description": plan.description,
        "requirement_id": plan.requirement_id,
        "owner_id": plan.owner_id,
        "status": plan.status,
        "start_date": plan.start_date.isoformat() if plan.start_date else None,
        "due_date": plan.due_date.isoformat() if plan.due_date else None,
        "created_by": plan.created_by,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "stats": stats,
    }
    if cycle_count is not None:
        data["cycle_count"] = cycle_count
    if last_cycle is not None:
        data["last_cycle"] = last_cycle
    return data


def _stats_from_counts(counts: dict[str, int], total: int) -> dict[str, Any]:
    stats = dict(_EMPTY_COUNTS)
    stats.update({k: int(counts.get(k, 0)) for k in ITEM_STATUSES})
    stats["total"] = total
    stats["progress"] = round(stats["passed"] * 100.0 / total, 1) if total else 0.0
    return stats


async def plan_stats(db: AsyncSession, plan_id: int) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(db_models.TestPlanItem.status, func.count())
            .where(db_models.TestPlanItem.plan_id == plan_id)
            .group_by(db_models.TestPlanItem.status)
        )
    ).all()
    counts = {str(status): int(cnt) for status, cnt in rows}
    return _stats_from_counts(counts, sum(counts.values()))


async def _stats_map(db: AsyncSession, plan_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = [int(i) for i in plan_ids]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                db_models.TestPlanItem.plan_id,
                db_models.TestPlanItem.status,
                func.count(),
            )
            .where(db_models.TestPlanItem.plan_id.in_(ids))
            .group_by(db_models.TestPlanItem.plan_id, db_models.TestPlanItem.status)
        )
    ).all()
    grouped: dict[int, dict[str, int]] = {}
    for plan_id, status, cnt in rows:
        grouped.setdefault(int(plan_id), {})[str(status)] = int(cnt)
    return {
        pid: _stats_from_counts(counts, sum(counts.values()))
        for pid, counts in grouped.items()
    }


# ----------------------------
# 计划 CRUD
# ----------------------------

async def list_plans(
    db: AsyncSession,
    user,
    project_id: int,
    *,
    status: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    ensure_project_access(user, project_id)
    stmt = select(db_models.TestPlan).where(db_models.TestPlan.project_id == project_id)
    if status:
        if status not in PLAN_STATUSES:
            raise HTTPException(status_code=422, detail="非法的计划状态")
        stmt = stmt.where(db_models.TestPlan.status == status)
    elif not include_archived:
        stmt = stmt.where(db_models.TestPlan.status != "archived")
    stmt = stmt.order_by(db_models.TestPlan.updated_at.desc())
    plans = (await db.execute(stmt)).scalars().all()

    stats_map = await _stats_map(db, [p.id for p in plans])
    cyc_rows = (
        await db.execute(
            select(db_models.TestCycle.status, db_models.TestCycle.plan_id, func.count())
            .where(db_models.TestCycle.plan_id.in_([p.id for p in plans] or [0]))
            .group_by(db_models.TestCycle.plan_id, db_models.TestCycle.status)
        )
    ).all()
    cyc_counts: dict[int, int] = {}
    for _status, plan_id, cnt in cyc_rows:
        cyc_counts[int(plan_id)] = cyc_counts.get(int(plan_id), 0) + int(cnt)

    latest: dict[int, db_models.TestCycle] = {}
    if plans:
        rows = (
            await db.execute(
                select(db_models.TestCycle)
                .where(db_models.TestCycle.plan_id.in_([p.id for p in plans]))
                .order_by(db_models.TestCycle.created_at.desc())
            )
        ).scalars().all()
        for cyc in rows:
            latest.setdefault(int(cyc.plan_id), cyc)

    out = []
    for plan in plans:
        last = latest.get(int(plan.id))
        last_cycle = (
            {"id": last.id, "name": last.name, "status": last.status}
            if last is not None else None
        )
        out.append(
            _plan_dict(
                plan,
                stats_map.get(int(plan.id), dict(_EMPTY_COUNTS)),
                cycle_count=cyc_counts.get(int(plan.id), 0),
                last_cycle=last_cycle,
            )
        )
    return out


async def get_plan_or_404(db: AsyncSession, plan_id: int) -> db_models.TestPlan:
    plan = await db.get(db_models.TestPlan, int(plan_id))
    if plan is None:
        raise HTTPException(status_code=404, detail="测试计划不存在")
    return plan


async def get_plan_detail(db: AsyncSession, plan_id: int) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    stats = await plan_stats(db, plan.id)
    cycle_count = (
        await db.execute(
            select(func.count()).select_from(db_models.TestCycle).where(
                db_models.TestCycle.plan_id == plan.id
            )
        )
    ).scalar_one()
    last = (
        await db.execute(
            select(db_models.TestCycle)
            .where(db_models.TestCycle.plan_id == plan.id)
            .order_by(db_models.TestCycle.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_cycle = (
        {
            "id": last.id,
            "name": last.name,
            "status": last.status,
        }
        if last is not None else None
    )
    return _plan_dict(plan, stats, cycle_count=int(cycle_count), last_cycle=last_cycle)


async def create_plan(db: AsyncSession, user, payload: dict[str, Any]) -> dict[str, Any]:
    project_id = int(payload["project_id"])
    ensure_project_access(user, project_id)
    project = await db.get(db_models.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="计划名称不能为空")
    owner_id = payload.get("owner_id")
    if owner_id is not None:
        await _validate_assignee(db, owner_id, project_id)
    plan = db_models.TestPlan(
        project_id=project_id,
        name=name[:200],
        description=payload.get("description"),
        requirement_id=payload.get("requirement_id"),
        owner_id=owner_id,
        status="draft",
        start_date=payload.get("start_date"),
        due_date=payload.get("due_date"),
        created_by=getattr(user, "id", None),
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return _plan_dict(plan, dict(_EMPTY_COUNTS), cycle_count=0, last_cycle=None)


async def update_plan(db: AsyncSession, user, plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    if plan.status == "archived" and payload.get("status") != "active":
        raise HTTPException(status_code=400, detail="已归档计划只读")
    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise HTTPException(status_code=422, detail="计划名称不能为空")
        plan.name = name[:200]
    if "description" in payload:
        plan.description = payload["description"]
    if "requirement_id" in payload:
        plan.requirement_id = payload["requirement_id"]
    if "owner_id" in payload:
        if payload["owner_id"] is not None:
            await _validate_assignee(db, payload["owner_id"], plan.project_id)
        plan.owner_id = payload["owner_id"]
    if "status" in payload and payload["status"] is not None:
        status = str(payload["status"])
        if status not in PLAN_STATUSES:
            raise HTTPException(status_code=422, detail="非法的计划状态")
        plan.status = status
    if "start_date" in payload:
        plan.start_date = payload["start_date"]
    if "due_date" in payload:
        plan.due_date = payload["due_date"]
    plan.updated_at = tz_now()
    await db.commit()
    await db.refresh(plan)
    return await get_plan_detail(db, plan.id)


async def delete_plan(db: AsyncSession, user, plan_id: int) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    cycle_count = (
        await db.execute(
            select(func.count()).select_from(db_models.TestCycle).where(
                db_models.TestCycle.plan_id == plan.id
            )
        )
    ).scalar_one()
    if cycle_count:
        raise HTTPException(status_code=409, detail="计划已有轮次，请归档而不是删除")
    await db.execute(
        delete(db_models.TestPlanItem).where(db_models.TestPlanItem.plan_id == plan.id)
    )
    await db.delete(plan)
    await db.commit()
    return {"ok": True}


# ----------------------------
# 计划项
# ----------------------------

def _item_dict(item, case_exists: bool = True, assignee_name: str | None = None) -> dict[str, Any]:
    return {
        "id": item.id,
        "plan_id": item.plan_id,
        "case_id": item.case_id,
        "case_name": item.case_name,
        "case_kind": item.case_kind,
        "assignee_id": item.assignee_id,
        "assignee_name": assignee_name,
        "priority": item.priority,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "status": item.status,
        "note": item.note,
        "sort_order": item.sort_order,
        "case_exists": case_exists,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


async def list_items(
    db: AsyncSession,
    user,
    plan_id: int,
    *,
    status: str | None = None,
    assignee_id: int | None = None,
    module_id: int | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    stmt = select(db_models.TestPlanItem).where(db_models.TestPlanItem.plan_id == plan.id)
    if status:
        if status not in ITEM_STATUSES:
            raise HTTPException(status_code=422, detail="非法的计划项状态")
        stmt = stmt.where(db_models.TestPlanItem.status == status)
    if assignee_id is not None:
        stmt = stmt.where(db_models.TestPlanItem.assignee_id == int(assignee_id))
    if keyword:
        stmt = stmt.where(db_models.TestPlanItem.case_name.ilike(f"%{keyword}%"))
    stmt = stmt.order_by(db_models.TestPlanItem.sort_order, db_models.TestPlanItem.id)
    items = (await db.execute(stmt)).scalars().all()

    case_ids = [i.case_id for i in items if i.case_id]
    cases = {}
    if case_ids:
        rows = (
            await db.execute(
                select(db_models.TestCase).where(db_models.TestCase.id.in_(case_ids))
            )
        ).scalars().all()
        cases = {int(c.id): c for c in rows}
    user_ids = [i.assignee_id for i in items if i.assignee_id]
    names = {}
    if user_ids:
        rows = (
            await db.execute(
                select(db_models.User).where(db_models.User.id.in_(set(user_ids)))
            )
        ).scalars().all()
        names = {int(u.id): (u.nickname or u.username) for u in rows}
    out = []
    for item in items:
        case = cases.get(int(item.case_id)) if item.case_id else None
        if module_id is not None and (case is None or case.module_id != int(module_id)):
            continue
        out.append(
            _item_dict(
                item,
                case_exists=bool(item.case_id and case is not None),
                assignee_name=names.get(int(item.assignee_id)) if item.assignee_id else None,
            )
        )
    return out


async def add_items(db: AsyncSession, user, plan_id: int, case_ids: list[int]) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    if plan.status == "archived":
        raise HTTPException(status_code=400, detail="已归档计划不能添加用例")
    wanted = list(dict.fromkeys(int(c) for c in case_ids))
    if not wanted:
        raise HTTPException(status_code=422, detail="case_ids 不能为空")

    rows = (
        await db.execute(select(db_models.TestCase).where(db_models.TestCase.id.in_(wanted)))
    ).scalars().all()
    by_id = {int(c.id): c for c in rows}
    existing = {
        int(cid)
        for cid in (
            await db.execute(
                select(db_models.TestPlanItem.case_id).where(
                    db_models.TestPlanItem.plan_id == plan.id,
                    db_models.TestPlanItem.case_id.is_not(None),
                )
            )
        ).scalars().all()
    }
    base = (
        await db.execute(
            select(func.coalesce(func.max(db_models.TestPlanItem.sort_order), -1)).where(
                db_models.TestPlanItem.plan_id == plan.id
            )
        )
    ).scalar_one()

    added = 0
    skipped = 0
    for cid in wanted:
        case = by_id.get(cid)
        if case is None or int(case.project_id) != int(plan.project_id) or cid in existing:
            skipped += 1
            continue
        base += 1
        db.add(
            db_models.TestPlanItem(
                plan_id=plan.id,
                case_id=cid,
                case_name=(case.name or "")[:255],
                case_kind=(getattr(case, "case_kind", None) or "functional")[:16],
                priority=getattr(case, "priority", None) or "medium",
                status="not_started",
                sort_order=base,
            )
        )
        existing.add(cid)
        added += 1
    plan.updated_at = tz_now()
    await db.commit()
    items = await list_items(db, user, plan.id)
    return {"added": added, "skipped": skipped, "items": items}


async def update_item(
    db: AsyncSession, user, plan_id: int, item_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    item = await db.get(db_models.TestPlanItem, int(item_id))
    if item is None or int(item.plan_id) != int(plan.id):
        raise HTTPException(status_code=404, detail="计划项不存在")
    if plan.status == "archived":
        raise HTTPException(status_code=400, detail="已归档计划只读")

    if "assignee_id" in payload:
        new_assignee = payload["assignee_id"]
        if new_assignee is not None:
            await _validate_assignee(db, new_assignee, plan.project_id)
        if new_assignee != item.assignee_id and new_assignee is not None:
            db.add(
                db_models.Notification(
                    user_id=int(new_assignee),
                    type="info",
                    title="你有一个新的测试任务",
                    message=f"计划「{plan.name}」中的用例「{item.case_name}」已指派给你",
                )
            )
        item.assignee_id = new_assignee
    if "status" in payload and payload["status"] is not None:
        status = str(payload["status"])
        if status not in ITEM_STATUSES:
            raise HTTPException(status_code=422, detail="非法的计划项状态")
        item.status = status
    if "priority" in payload and payload["priority"] is not None:
        priority = str(payload["priority"])
        if priority not in PRIORITIES:
            raise HTTPException(status_code=422, detail="非法的优先级")
        item.priority = priority
    if "due_date" in payload:
        item.due_date = payload["due_date"]
    if "note" in payload:
        item.note = payload["note"]
    if "sort_order" in payload and payload["sort_order"] is not None:
        item.sort_order = int(payload["sort_order"])
    item.updated_at = tz_now()
    plan.updated_at = tz_now()
    await db.commit()
    await db.refresh(item)
    await _refresh_plan_status(db, plan)
    return _item_dict(item)


async def delete_item(db: AsyncSession, user, plan_id: int, item_id: int) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    item = await db.get(db_models.TestPlanItem, int(item_id))
    if item is None or int(item.plan_id) != int(plan.id):
        raise HTTPException(status_code=404, detail="计划项不存在")
    await db.delete(item)
    plan.updated_at = tz_now()
    await db.commit()
    return {"ok": True}


async def _refresh_plan_status(db: AsyncSession, plan) -> None:
    """所有计划项都通过且计划处于 active 时自动完成。"""
    if plan.status != "active":
        return
    stats = await plan_stats(db, plan.id)
    if stats["total"] > 0 and stats["passed"] == stats["total"]:
        plan.status = "completed"
        plan.updated_at = tz_now()
        await db.commit()


__all__ = [
    "PLAN_STATUSES", "ITEM_STATUSES", "PRIORITIES", "ensure_project_access",
    "plan_stats", "list_plans", "get_plan_or_404", "get_plan_detail",
    "create_plan", "update_plan", "delete_plan", "list_items", "add_items",
    "update_item", "delete_item", "_refresh_plan_status",
]
