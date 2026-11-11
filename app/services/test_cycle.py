"""050 US2：测试轮次服务（快照、启动、取消、重跑、批次回填）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.services.test_plan import ensure_project_access, get_plan_or_404
from app.tz import now as tz_now

logger = logging.getLogger(__name__)

CYCLE_STATUSES = {"planned", "running", "completed", "cancelled"}

# 后台任务引用，避免被 GC；测试可 monkeypatch spawn_cycle_run 为空实现。
_RUNNING_TASKS: set[asyncio.Task] = set()


def _cycle_dict(cycle, *, passed: int = 0, failed: int = 0) -> dict[str, Any]:
    return {
        "id": cycle.id,
        "plan_id": cycle.plan_id,
        "project_id": cycle.project_id,
        "name": cycle.name,
        "environment_id": cycle.environment_id,
        "agent_id": cycle.agent_id,
        "status": cycle.status,
        "batch_id": cycle.batch_id,
        "case_count": cycle.case_count,
        "skipped_count": cycle.skipped_count,
        "passed": passed,
        "failed": failed,
        "created_by": cycle.created_by,
        "started_at": cycle.started_at,
        "finished_at": cycle.finished_at,
        "created_at": cycle.created_at,
        "updated_at": cycle.updated_at,
    }


async def get_cycle_or_404(db: AsyncSession, cycle_id: int) -> db_models.TestCycle:
    cycle = await db.get(db_models.TestCycle, int(cycle_id))
    if cycle is None:
        raise HTTPException(status_code=404, detail="测试轮次不存在")
    return cycle


async def _batch_counts(db: AsyncSession, batch_id: int | None) -> tuple[int, int]:
    if not batch_id:
        return 0, 0
    batch = await db.get(db_models.RunBatch, int(batch_id))
    if batch is None:
        return 0, 0
    return int(batch.passed or 0), int(batch.failed or 0)


async def _cycle_batch_chain(db: AsyncSession, batch_id: int | None) -> list[db_models.RunBatch]:
    """轮次批次链（原始批次 + 自动/手动重跑批次），按时间从早到晚。"""
    chain: list[db_models.RunBatch] = []
    seen: set[int] = set()
    bid = batch_id
    while bid and int(bid) not in seen:
        seen.add(int(bid))
        batch = await db.get(db_models.RunBatch, int(bid))
        if batch is None:
            break
        chain.append(batch)
        bid = getattr(batch, "parent_batch_id", None)
    chain.reverse()
    return chain


async def _cycle_snapshot(db: AsyncSession, cycle):
    """返回 (批次链, 每个用例最新一次 run, passed, failed)。重跑按用例后到者覆盖。"""
    batches = await _cycle_batch_chain(db, cycle.batch_id)
    ids = [b.id for b in batches]
    latest: dict[int, db_models.TestRun] = {}
    if ids:
        runs = (
            await db.execute(
                select(db_models.TestRun)
                .where(db_models.TestRun.batch_id.in_(ids))
                .order_by(db_models.TestRun.id)
            )
        ).scalars().all()
        for run in runs:
            if run.case_id:
                latest[int(run.case_id)] = run
    passed = sum(1 for r in latest.values() if r.status == "passed")
    failed = sum(1 for r in latest.values() if r.status in ("failed", "error"))
    return batches, latest, passed, failed


async def list_cycles(db: AsyncSession, user, plan_id: int) -> list[dict[str, Any]]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    cycles = (
        await db.execute(
            select(db_models.TestCycle)
            .where(db_models.TestCycle.plan_id == plan.id)
            .order_by(db_models.TestCycle.created_at.desc())
        )
    ).scalars().all()
    out = []
    for cycle in cycles:
        _, _, passed, failed = await _cycle_snapshot(db, cycle)
        out.append(_cycle_dict(cycle, passed=passed, failed=failed))
    return out


async def create_cycle(db: AsyncSession, user, plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    plan = await get_plan_or_404(db, plan_id)
    ensure_project_access(user, plan.project_id)
    if plan.status == "archived":
        raise HTTPException(status_code=400, detail="已归档计划不能创建轮次")

    items = (
        await db.execute(
            select(db_models.TestPlanItem)
            .where(db_models.TestPlanItem.plan_id == plan.id)
            .order_by(db_models.TestPlanItem.sort_order, db_models.TestPlanItem.id)
        )
    ).scalars().all()

    requested = payload.get("case_ids")
    if requested:
        wanted = {int(c) for c in requested}
        chosen = [it for it in items if it.case_id and int(it.case_id) in wanted]
        found = {int(it.case_id) for it in chosen}
        if wanted - found:
            raise HTTPException(status_code=400, detail="部分用例不属于该计划")
        skipped = 0
    else:
        chosen = [it for it in items if it.case_id]
        skipped = sum(1 for it in items if not it.case_id)
    if not chosen:
        raise HTTPException(status_code=400, detail="计划没有可执行的用例")

    n = (
        await db.execute(
            select(func.count()).select_from(db_models.TestCycle).where(
                db_models.TestCycle.plan_id == plan.id
            )
        )
    ).scalar_one()
    name = (payload.get("name") or "").strip() or f"第 {int(n) + 1} 轮"
    cycle = db_models.TestCycle(
        plan_id=plan.id,
        project_id=plan.project_id,
        name=name[:200],
        environment_id=payload.get("environment_id"),
        agent_id=payload.get("agent_id"),
        status="planned",
        case_count=len(chosen),
        skipped_count=skipped,
        created_by=getattr(user, "id", None),
    )
    db.add(cycle)
    await db.flush()
    for idx, it in enumerate(chosen):
        db.add(
            db_models.TestCycleItem(
                cycle_id=cycle.id,
                plan_item_id=it.id,
                case_id=it.case_id,
                case_name=it.case_name,
                case_kind=it.case_kind,
                assignee_id=it.assignee_id,
                sort_order=idx,
            )
        )
    await db.commit()
    await db.refresh(cycle)
    return _cycle_dict(cycle)


async def get_cycle_detail(db: AsyncSession, user, cycle_id: int) -> dict[str, Any]:
    cycle = await get_cycle_or_404(db, cycle_id)
    ensure_project_access(user, cycle.project_id)

    items = (
        await db.execute(
            select(db_models.TestCycleItem)
            .where(db_models.TestCycleItem.cycle_id == cycle.id)
            .order_by(db_models.TestCycleItem.sort_order, db_models.TestCycleItem.id)
        )
    ).scalars().all()

    batch_chain, runs_by_case, passed, failed = await _cycle_snapshot(db, cycle)

    data = _cycle_dict(cycle, passed=passed, failed=failed)
    data["items"] = [
        {
            "case_id": it.case_id,
            "case_name": it.case_name,
            "case_kind": it.case_kind,
            "plan_item_id": it.plan_item_id,
            "assignee_id": it.assignee_id,
            "run_status": runs_by_case[int(it.case_id)].status if it.case_id and int(it.case_id) in runs_by_case else None,
            "failure_kind": runs_by_case[int(it.case_id)].failure_kind if it.case_id and int(it.case_id) in runs_by_case else None,
            "run_id": runs_by_case[int(it.case_id)].id if it.case_id and int(it.case_id) in runs_by_case else None,
        }
        for it in items
    ]
    data["batches"] = [
        {
            "id": batch.id,
            "name": batch.name,
            "status": batch.status,
            "passed": int(batch.passed or 0),
            "failed": int(batch.failed or 0),
            "finished_at": batch.finished_at,
        }
        for batch in batch_chain
    ]
    return data


def spawn_cycle_run(
    *,
    cycle_id: int,
    case_ids: list[int],
    project_id: int,
    environment_id: int | None,
    batch_id: int,
    user_id: int | None,
    triggered_by: str | None,
) -> None:
    """后台启动轮次批次。测试中可替换为空实现。"""
    try:
        task = asyncio.create_task(
            _run_cycle_batch(
                cycle_id=cycle_id,
                case_ids=case_ids,
                project_id=project_id,
                environment_id=environment_id,
                batch_id=batch_id,
                user_id=user_id,
                triggered_by=triggered_by,
            )
        )
    except RuntimeError:  # 无运行中的事件循环
        logger.warning("轮次 %s 启动失败：没有事件循环", cycle_id)
        return
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)


async def _run_cycle_batch(
    *,
    cycle_id: int,
    case_ids: list[int],
    project_id: int,
    environment_id: int | None,
    batch_id: int,
    user_id: int | None,
    triggered_by: str | None,
) -> None:
    from app.routers.testcase import execution as _exec

    try:
        await _exec.run_batch_test_cases(
            case_ids,
            project_id,
            batch_id=batch_id,
            environment_id=environment_id,
            triggered_by=triggered_by,
        )
    except Exception:  # noqa: BLE001 - 执行失败不应影响批次记录
        logger.exception("轮次 %s 执行失败 batch=%s", cycle_id, batch_id)
    finally:
        if user_id:
            try:
                from app.services.notifications import notify_batch_completed

                await notify_batch_completed(batch_id, user_id)
            except Exception:  # noqa: BLE001
                logger.warning("轮次批次通知失败 batch=%s", batch_id, exc_info=True)


async def start_cycle(db: AsyncSession, user, cycle_id: int) -> dict[str, Any]:
    from app.crud import run as crud_run

    cycle = await get_cycle_or_404(db, cycle_id)
    plan = await get_plan_or_404(db, cycle.plan_id)
    ensure_project_access(user, cycle.project_id)
    if cycle.status != "planned":
        raise HTTPException(status_code=409, detail="仅 planned 状态可启动")
    if plan.status == "archived":
        raise HTTPException(status_code=400, detail="已归档计划不能启动轮次")
    if plan.status == "draft":
        raise HTTPException(status_code=400, detail="计划尚未开始，请先激活")

    running = (
        await db.execute(
            select(func.count()).select_from(db_models.TestCycle).where(
                db_models.TestCycle.plan_id == plan.id,
                db_models.TestCycle.status == "running",
            )
        )
    ).scalar_one()
    if running:
        raise HTTPException(status_code=409, detail="该计划已有正在执行的轮次")

    items = (
        await db.execute(
            select(db_models.TestCycleItem)
            .where(db_models.TestCycleItem.cycle_id == cycle.id)
            .order_by(db_models.TestCycleItem.sort_order, db_models.TestCycleItem.id)
        )
    ).scalars().all()
    case_ids = [int(it.case_id) for it in items if it.case_id]
    if not case_ids:
        raise HTTPException(status_code=400, detail="轮次没有可执行的用例")

    # 用例被删除的轮次项跳过
    existing = {
        int(cid)
        for cid in (
            await db.execute(
                select(db_models.TestCase.id).where(db_models.TestCase.id.in_(case_ids))
            )
        ).scalars().all()
    }
    case_ids = [cid for cid in case_ids if cid in existing]
    if not case_ids:
        raise HTTPException(status_code=400, detail="轮次用例均已删除")

    batch = await crud_run.create_run_batch(
        db,
        project_id=cycle.project_id,
        name=f"{plan.name} · {cycle.name}",
        total_cases=len(case_ids),
        triggered_by=getattr(user, "username", None),
        source="test_cycle",
        source_id=cycle.id,
    )
    cycle.batch_id = batch.id
    cycle.case_count = len(case_ids)
    cycle.status = "running"
    cycle.started_at = tz_now()
    cycle.updated_at = tz_now()
    await db.commit()

    spawn_cycle_run(
        cycle_id=cycle.id,
        case_ids=case_ids,
        project_id=cycle.project_id,
        environment_id=cycle.environment_id,
        batch_id=batch.id,
        user_id=getattr(user, "id", None),
        triggered_by=getattr(user, "username", None),
    )
    return {"batch_id": batch.id, "status": "running", "total": len(case_ids)}


async def cancel_cycle(db: AsyncSession, user, cycle_id: int) -> dict[str, Any]:
    from app import execution_control
    from app.crud import run as crud_run

    cycle = await get_cycle_or_404(db, cycle_id)
    ensure_project_access(user, cycle.project_id)
    if cycle.status not in ("planned", "running"):
        raise HTTPException(status_code=409, detail=f"仅 planned/running 可取消，当前为 {cycle.status}")

    if cycle.status == "running" and cycle.batch_id:
        await execution_control.request_stop(cycle.batch_id)
        await crud_run.cancel_remaining_batch_runs(
            db, cycle.batch_id, message="轮次已取消", include_running=True
        )
        batch = await db.get(db_models.RunBatch, cycle.batch_id)
        if batch is not None:
            batch.status = "cancelled"
            batch.finished_at = tz_now()
    cycle.status = "cancelled"
    cycle.finished_at = tz_now()
    cycle.updated_at = tz_now()
    await db.commit()
    return {"status": "cancelled"}


async def retry_failed(db: AsyncSession, user, cycle_id: int) -> dict[str, Any]:
    from app.services.batch_rerun import create_rerun_batch

    cycle = await get_cycle_or_404(db, cycle_id)
    ensure_project_access(user, cycle.project_id)
    if not cycle.batch_id:
        raise HTTPException(status_code=400, detail="轮次尚未启动")
    batch = await db.get(db_models.RunBatch, cycle.batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="轮次批次不存在")
    result = await create_rerun_batch(
        db,
        batch,
        environment_id=cycle.environment_id,
        name_suffix=f"{cycle.name} · 重跑失败",
        triggered_by=getattr(user, "username", None),
        user_id=getattr(user, "id", None),
    )
    new_batch = result.get("batch")
    if new_batch is None:
        raise HTTPException(status_code=400, detail="没有可重跑的失败用例")
    cycle.batch_id = new_batch.id
    cycle.status = "running"
    cycle.finished_at = None
    cycle.updated_at = tz_now()
    await db.commit()
    return {"batch_id": new_batch.id, "case_count": result.get("total", 0)}


async def _resolve_cycle(db: AsyncSession, batch) -> db_models.TestCycle | None:
    if getattr(batch, "source", None) == "test_cycle" and getattr(batch, "source_id", None):
        return await db.get(db_models.TestCycle, int(batch.source_id))
    parent_id = getattr(batch, "parent_batch_id", None)
    hops = 0
    while parent_id and hops < 20:
        parent = await db.get(db_models.RunBatch, int(parent_id))
        if parent is None:
            return None
        if getattr(parent, "source", None) == "test_cycle" and getattr(parent, "source_id", None):
            return await db.get(db_models.TestCycle, int(parent.source_id))
        parent_id = getattr(parent, "parent_batch_id", None)
        hops += 1
    return None


def _map_run_status(status: str | None) -> str | None:
    if status == "passed":
        return "passed"
    if status in ("failed", "error"):
        return "failed"
    if status in ("running", "pending"):
        return "in_progress"
    if status in ("skipped", "cancelled"):
        return "blocked"
    return None


async def sync_cycle_from_batch(db: AsyncSession, batch) -> None:
    """批次收尾后回填轮次与计划项状态；由 batch_finalize 调用。"""
    cycle = await _resolve_cycle(db, batch)
    if cycle is None:
        return

    runs = (
        await db.execute(
            select(db_models.TestRun)
            .where(db_models.TestRun.batch_id == batch.id)
            .order_by(db_models.TestRun.id)
        )
    ).scalars().all()
    latest: dict[int, db_models.TestRun] = {}
    for run in runs:
        if run.case_id:
            latest[int(run.case_id)] = run

    if batch.id and (cycle.batch_id is None or int(batch.id) >= int(cycle.batch_id)):
        cycle.batch_id = batch.id
    status = str(getattr(batch, "status", "") or "")
    if status in ("passed", "partial", "failed"):
        cycle.status = "completed"
        cycle.finished_at = batch.finished_at or tz_now()
    elif status == "cancelled":
        cycle.status = "cancelled"
        cycle.finished_at = batch.finished_at or tz_now()
    else:
        cycle.status = "running"
    cycle.updated_at = tz_now()

    plan = await db.get(db_models.TestPlan, cycle.plan_id)
    if plan is not None and latest:
        plan_items = (
            await db.execute(
                select(db_models.TestPlanItem).where(
                    db_models.TestPlanItem.plan_id == plan.id,
                    db_models.TestPlanItem.case_id.in_(list(latest.keys())),
                )
            )
        ).scalars().all()
        for item in plan_items:
            mapped = _map_run_status(latest[int(item.case_id)].status)
            if mapped:
                item.status = mapped
                item.updated_at = tz_now()
        plan.updated_at = tz_now()
    await db.commit()

    if plan is not None:
        from app.services.test_plan import _refresh_plan_status

        await _refresh_plan_status(db, plan)


__all__ = [
    "CYCLE_STATUSES", "list_cycles", "create_cycle", "get_cycle_detail",
    "start_cycle", "cancel_cycle", "retry_failed", "sync_cycle_from_batch",
    "spawn_cycle_run", "get_cycle_or_404",
]
