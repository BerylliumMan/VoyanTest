"""046：变更回归计划的状态机（创建 → 待确认 → 确认/取消/超时执行）。

借鉴 zcode Automation 的调度内核：
- 定时扫描 + DB 原子认领（CAS）
- 僵尸回收、错过窗口宽限、失败退避重试、pending 上限合并
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.api_extract import ApiChangeTrigger
from app.models.quality import QualityRegressionPlan
from app.services.api_change_impact import AffectedApi
from app.tz import CST, now as tz_now

logger = logging.getLogger(__name__)

# 失败重试退避（分钟）；最多重试 len(RETRY_BACKOFF_MINUTES) 次
RETRY_BACKOFF_MINUTES = (5, 15)
ZOMBIE_TIMEOUT_MINUTES = 5

# 后台任务强引用集合（避免 asyncio 任务被垃圾回收）
_BACKGROUND_TASKS: set = set()


def spawn_background(coro):
    """创建后台任务并保持强引用，直到任务结束。"""
    import asyncio

    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired_skipped"


@dataclass
class CreatePlanResult:
    created: bool = False
    merged: bool = False
    plan_id: int | None = None
    trigger_id: int | None = None
    reason: str | None = None


def _plan_items(affected: list[AffectedApi]) -> list[dict]:
    items: list[dict] = []
    for api in affected:
        reasons = [{
            "source": "code_change",
            "method": api.method,
            "path": api.path,
            "files": list(api.changed_files),
        }]
        for case_id in api.case_ids:
            items.append({
                "case_id": int(case_id),
                "case_kind": "api",
                "reasons": reasons,
                "note": "",
            })
    return items


def _merge_items(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged: dict[int, dict] = {}
    for item in list(existing or []) + list(incoming or []):
        try:
            case_id = int(item.get("case_id"))
        except (TypeError, ValueError):
            continue
        if case_id in merged:
            current = merged[case_id]
            seen = {(r.get("method"), r.get("path")) for r in current.get("reasons") or []}
            for reason in item.get("reasons") or []:
                key = (reason.get("method"), reason.get("path"))
                if key not in seen:
                    current.setdefault("reasons", []).append(reason)
                    seen.add(key)
        else:
            merged[case_id] = dict(item)
            merged[case_id]["reasons"] = list(item.get("reasons") or [])
    return list(merged.values())


def _merge_affected(existing: list[dict], incoming: list[AffectedApi]) -> list[dict]:
    by_def: dict[int, dict] = {}
    for row in existing or []:
        try:
            key = int(row.get("definition_id"))
        except (TypeError, ValueError):
            continue
        by_def[key] = {
            "definition_id": key,
            "method": row.get("method") or "",
            "path": row.get("path") or "",
            "case_ids": list(row.get("case_ids") or []),
            "changed_files": list(row.get("changed_files") or []),
        }
    for api in incoming:
        key = int(api.definition_id)
        target = by_def.setdefault(
            key,
            {
                "definition_id": key,
                "method": api.method,
                "path": api.path,
                "case_ids": [],
                "changed_files": [],
            },
        )
        target["case_ids"] = sorted(set(target["case_ids"]) | set(api.case_ids))
        target["changed_files"] = sorted(
            set(target["changed_files"]) | set(api.changed_files)
        )
    return list(by_def.values())


def _case_count(affected: list[dict]) -> int:
    return len({int(c) for row in affected for c in (row.get("case_ids") or [])})


async def _pending_triggers(db: AsyncSession, project_id: int) -> list[ApiChangeTrigger]:
    return (
        await db.execute(
            select(ApiChangeTrigger)
            .where(
                ApiChangeTrigger.project_id == project_id,
                ApiChangeTrigger.status == STATUS_PENDING,
            )
            .order_by(ApiChangeTrigger.id.asc())
        )
    ).scalars().all()


async def create_change_plan(
    db: AsyncSession,
    *,
    project_id: int,
    affected: list[AffectedApi],
    from_commit: str | None,
    to_commit: str | None,
    now: datetime | None = None,
    window_minutes: int | None = None,
    grace_minutes: int | None = None,
    max_pending: int | None = None,
) -> CreatePlanResult:
    """创建或合并变更回归计划。"""
    settings = get_settings()
    now = now or tz_now()
    window = window_minutes if window_minutes is not None else settings.api_sync_confirm_window_minutes
    limit = max_pending if max_pending is not None else settings.api_change_plan_max_pending

    affected = [a for a in affected if a.case_ids]
    if not affected:
        return CreatePlanResult(reason="no_cases")

    pending = await _pending_triggers(db, project_id)

    # 1) 同一 from_commit 的 pending 计划：并入，deadline 不重置
    for trigger in pending:
        if from_commit and trigger.from_commit == from_commit:
            await _merge_into(db, trigger, affected, to_commit=to_commit)
            await db.flush()
            return CreatePlanResult(
                created=False,
                merged=True,
                plan_id=trigger.plan_id,
                trigger_id=trigger.id,
                reason="same_from_commit",
            )

    # 2) pending 上限：并入最新一条
    if len(pending) >= limit:
        trigger = max(pending, key=lambda t: t.id)
        await _merge_into(db, trigger, affected, to_commit=to_commit)
        await db.flush()
        return CreatePlanResult(
            created=False,
            merged=True,
            plan_id=trigger.plan_id,
            trigger_id=trigger.id,
            reason="pending_limit",
        )

    # 3) 新建计划
    items = _plan_items(affected)
    plan = QualityRegressionPlan(
        project_id=project_id,
        name=f"代码变更回归 {str(from_commit or '')[:8]}..{str(to_commit or '')[:8]}",
        description="由代码变更自动生成的接口回归计划",
        items=items,
    )
    db.add(plan)
    await db.flush()

    trigger = ApiChangeTrigger(
        project_id=project_id,
        from_commit=from_commit,
        to_commit=to_commit,
        plan_id=plan.id,
        status=STATUS_PENDING,
        deadline_at=now + timedelta(minutes=window),
        case_count=_case_count(_merge_affected([], affected)),
        affected=_merge_affected([], affected),
    )
    db.add(trigger)
    await db.flush()
    return CreatePlanResult(
        created=True,
        plan_id=plan.id,
        trigger_id=trigger.id,
    )


async def _merge_into(
    db: AsyncSession,
    trigger: ApiChangeTrigger,
    affected: list[AffectedApi],
    *,
    to_commit: str | None,
) -> None:
    merged_affected = _merge_affected(trigger.affected or [], affected)
    trigger.affected = merged_affected
    trigger.case_count = _case_count(merged_affected)
    if to_commit:
        trigger.to_commit = to_commit

    plan = await db.get(QualityRegressionPlan, trigger.plan_id) if trigger.plan_id else None
    if plan is not None:
        plan.items = _merge_items(plan.items or [], _plan_items(affected))


async def claim_trigger(
    db: AsyncSession,
    trigger_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    """原子认领到期的 pending 触发（CAS，rowcount==1 才算成功）。"""
    now = now or tz_now()
    allowed = or_(
        and_(ApiChangeTrigger.retry_at.isnot(None), ApiChangeTrigger.retry_at <= now),
        and_(
            ApiChangeTrigger.retry_at.is_(None),
            ApiChangeTrigger.deadline_at <= now,
        ),
    )
    result = await db.execute(
        update(ApiChangeTrigger)
        .execution_options(synchronize_session=False)
        .where(
            ApiChangeTrigger.id == trigger_id,
            ApiChangeTrigger.status == STATUS_PENDING,
            allowed,
        )
        .values(status=STATUS_RUNNING, claimed_at=now)
    )
    return (result.rowcount or 0) == 1


async def scan_due_triggers(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    grace_minutes: int | None = None,
) -> list[int]:
    """回收僵尸 + 认领到期触发；返回成功认领的 trigger id。"""
    settings = get_settings()
    now = now or tz_now()
    grace = grace_minutes if grace_minutes is not None else settings.api_sync_misfire_grace_minutes

    # 1) 僵尸回收：running 太久未完成
    zombie_cutoff = now - timedelta(minutes=ZOMBIE_TIMEOUT_MINUTES)
    await db.execute(
        update(ApiChangeTrigger)
        .execution_options(synchronize_session=False)
        .where(
            ApiChangeTrigger.status == STATUS_RUNNING,
            ApiChangeTrigger.claimed_at.isnot(None),
            ApiChangeTrigger.claimed_at < zombie_cutoff,
        )
        .values(
            status=STATUS_FAILED,
            decision="failed",
            reason="执行进程丢失（僵尸回收）",
            decided_at=now,
        )
    )

    # 2) 到期判断（含重试锚点）
    due = (
        await db.execute(
            select(ApiChangeTrigger).where(
                ApiChangeTrigger.status == STATUS_PENDING,
                or_(
                    and_(ApiChangeTrigger.retry_at.isnot(None), ApiChangeTrigger.retry_at <= now),
                    and_(
                        ApiChangeTrigger.retry_at.is_(None),
                        ApiChangeTrigger.deadline_at <= now,
                    ),
                ),
            )
        )
    ).scalars().all()

    claimed: list[int] = []
    for trigger in due:
        anchor = _aware(trigger.retry_at or trigger.deadline_at)
        if anchor is not None and now > anchor + timedelta(minutes=grace):
            await db.execute(
                update(ApiChangeTrigger)
                .where(
                    ApiChangeTrigger.id == trigger.id,
                    ApiChangeTrigger.status == STATUS_PENDING,
                )
                .values(
                    status=STATUS_EXPIRED,
                    decision="expired_skipped",
                    decided_at=now,
                    reason=(
                        f"错过执行窗口超过 {grace} 分钟，"
                        "按一次性任务约定跳过，不补跑"
                    ),
                )
            )
            continue
        if await claim_trigger(db, trigger.id, now=now):
            claimed.append(int(trigger.id))
    await db.flush()
    return claimed


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读出的 datetime 无时区；按应用时区补齐后再比较。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=CST)


def next_retry_at(
    trigger: ApiChangeTrigger,
    *,
    now: datetime | None = None,
    retry_count: int | None = None,
) -> datetime | None:
    """按退避策略计算下次重试时间；超过次数返回 None。"""
    now = now or tz_now()
    count = trigger.retry_count if retry_count is None else retry_count
    if count >= len(RETRY_BACKOFF_MINUTES):
        return None
    return now + timedelta(minutes=RETRY_BACKOFF_MINUTES[count])


async def cancel_trigger(
    db: AsyncSession,
    trigger_id: int,
    *,
    user_id: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> bool:
    """取消仍处于 pending 的计划。"""
    now = now or tz_now()
    result = await db.execute(
        update(ApiChangeTrigger)
        .execution_options(synchronize_session=False)
        .where(
            ApiChangeTrigger.id == trigger_id,
            ApiChangeTrigger.status == STATUS_PENDING,
        )
        .values(
            status=STATUS_CANCELLED,
            decision="cancelled",
            decided_at=now,
            decided_by=user_id,
            reason=reason,
        )
    )
    return (result.rowcount or 0) == 1


async def mark_trigger_finished(
    db: AsyncSession,
    trigger: ApiChangeTrigger,
    *,
    batch_id: int | None,
    error: str | None = None,
    decision: str = "auto_ran",
    now: datetime | None = None,
) -> None:
    """执行结束后更新触发状态；失败时按退避回到 pending 或终态 failed。"""
    now = now or tz_now()
    trigger.batch_id = batch_id
    trigger.decided_at = now
    if not error:
        trigger.status = STATUS_DONE
        trigger.decision = decision
        trigger.reason = None
        return

    retry_at = next_retry_at(trigger, now=now)
    if retry_at is not None:
        trigger.retry_count = int(trigger.retry_count or 0) + 1
        trigger.status = STATUS_PENDING
        trigger.retry_at = retry_at
        trigger.claimed_at = None
        trigger.reason = f"执行失败，将在 {retry_at.isoformat()} 重试：{error[:500]}"
    else:
        trigger.status = STATUS_FAILED
        trigger.decision = "failed"
        trigger.reason = f"执行失败且已用尽重试：{error[:500]}"


async def confirm_trigger(
    db: AsyncSession,
    trigger_id: int,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
) -> bool:
    """人工确认 → 立即可执行（把 deadline 提前到当前时间再认领）。"""
    now = now or tz_now()
    result = await db.execute(
        update(ApiChangeTrigger)
        .execution_options(synchronize_session=False)
        .where(
            ApiChangeTrigger.id == trigger_id,
            ApiChangeTrigger.status == STATUS_PENDING,
        )
        .values(deadline_at=now, decided_by=user_id)
    )
    if (result.rowcount or 0) != 1:
        return False
    return await claim_trigger(db, trigger_id, now=now)


# ── 执行与调度 ────────────────────────────────────────────────────────────────


def _plan_case_ids(trigger: ApiChangeTrigger, plan: QualityRegressionPlan | None) -> list[int]:
    """计划里的用例；plan 缺失时回退到 trigger.affected。"""
    if plan is not None:
        ids = []
        for item in plan.items or []:
            try:
                ids.append(int(item.get("case_id")))
            except (TypeError, ValueError):
                continue
        return sorted(set(ids))
    ids = []
    for row in trigger.affected or []:
        ids.extend(int(c) for c in row.get("case_ids") or [])
    return sorted(set(ids))


async def execute_claimed_trigger(trigger_id: int) -> int | None:
    """执行一个已认领（running）的触发：创建批次 → 运行 → 回写状态。"""
    from app.crud import run as crud_run
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        trigger = await db.get(ApiChangeTrigger, trigger_id)
        if trigger is None or trigger.status != STATUS_RUNNING:
            return None
        plan = (
            await db.get(QualityRegressionPlan, trigger.plan_id)
            if trigger.plan_id
            else None
        )
        case_ids = _plan_case_ids(trigger, plan)
        project_id = trigger.project_id
        if not case_ids:
            await mark_trigger_finished(
                db, trigger, batch_id=None, error="计划中没有可执行用例"
            )
            await db.commit()
            return None

        batch = await crud_run.create_run_batch(
            db,
            project_id=project_id,
            name=f"代码变更回归 #{trigger.id}",
            total_cases=len(case_ids),
            triggered_by="api_change_trigger",
            source="api_change_trigger",
            source_id=trigger.id,
        )
        batch_id = int(batch.id)
        trigger.batch_id = batch_id
        await db.commit()

    error: str | None = None
    try:
        from core.runner import run_batch_test_cases

        await run_batch_test_cases(case_ids, project_id, batch_id=batch_id)
    except Exception as exc:  # noqa: BLE001 - 失败按退避重试
        logger.warning("变更计划执行失败 trigger=%s: %s", trigger_id, exc, exc_info=True)
        error = str(exc)

    async with AsyncSessionLocal() as db:
        trigger = await db.get(ApiChangeTrigger, trigger_id)
        if trigger is not None:
            await mark_trigger_finished(db, trigger, batch_id=batch_id, error=error)
            await db.commit()
            # 046 v2：自动执行审计与通知
            try:
                from app.auth import log_audit

                await log_audit(
                    db,
                    None,
                    "api_change_plan.auto_run",
                    {
                        "project_id": project_id,
                        "trigger_id": trigger_id,
                        "batch_id": batch_id,
                        "case_count": len(case_ids),
                        "status": trigger.status,
                        "error": (error or "")[:200] or None,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning("计划审计写入失败 trigger=%s", trigger_id, exc_info=True)
            await _notify_plan_finished(
                db, project_id, trigger_id, batch_id=batch_id, error=error
            )
    return batch_id


async def run_due_once() -> list[int]:
    """扫描并启动到期的计划（供调度循环调用一次）。"""
    import asyncio

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ids = await scan_due_triggers(db)
        await db.commit()
    for trigger_id in ids:
        spawn_background(execute_claimed_trigger(trigger_id))
    return ids


async def plan_after_sync(db, binding, outcome) -> dict:
    """把一次同步结果转成影响清单与待确认计划（巡检与手动检查共用）。"""
    from asyncio import to_thread

    from app.services.api_change_impact import compute_impact
    from app.services.api_sync import repo_workdir
    from core.git_sync import list_changed_files

    if outcome.status != "success":
        return {"status": outcome.status, "error": outcome.error}
    if not outcome.base_commit or outcome.base_commit == outcome.head_commit:
        return {"status": "no_change"}

    workdir = repo_workdir(binding.project_id)
    changed = list(outcome.changed_files or [])
    if not changed:
        try:
            changed = await to_thread(
                list_changed_files, workdir, outcome.base_commit, outcome.head_commit
            )
        except Exception as exc:  # noqa: BLE001 - diff 失败不影响同步本身
            logger.warning("计算变更文件失败 project=%s: %s", binding.project_id, exc)
            return {"status": "diff_failed", "error": str(exc)}
    if not changed:
        return {"status": "no_change"}

    affected = await compute_impact(
        db, project_id=binding.project_id, changed_files=changed
    )
    if not affected:
        return {"status": "no_affected", "changed_files": len(changed)}

    result = await create_change_plan(
        db,
        project_id=binding.project_id,
        affected=affected,
        from_commit=outcome.base_commit,
        to_commit=outcome.head_commit,
        window_minutes=binding.confirm_window_minutes or None,
    )
    await db.commit()
    if result.trigger_id:
        await _notify_plan_created(
            db, binding.project_id, result.trigger_id, created=result.created
        )
    return {
        "status": "planned",
        "changed_files": len(changed),
        "affected": len(affected),
        "created": result.created,
        "merged": result.merged,
        "trigger_id": result.trigger_id,
    }


async def _admin_user_ids(db) -> list[int]:
    from app.models.auth import User

    rows = await db.execute(
        select(User.id).where(User.role == "admin", User.status == "active")
    )
    return [int(x) for x in rows.scalars().all()]


async def _notify_plan_created(
    db, project_id: int, trigger_id: int, *, created: bool
) -> None:
    """计划生成/合并时通知管理员。"""
    try:
        from app.models.notification import Notification

        title = (
            "代码变更回归计划已生成"
            if created
            else "新的代码变更已并入待确认计划"
        )
        for uid in await _admin_user_ids(db):
            db.add(
                Notification(
                    user_id=uid,
                    type="warning",
                    title=title,
                    message=(
                        f"项目 #{project_id} 的变更计划 #{trigger_id} 等待确认；"
                        "确认窗口到期后将自动执行"
                    ),
                )
            )
        await db.commit()
    except Exception:  # noqa: BLE001 - 通知失败不影响计划
        logger.warning("计划通知写入失败 trigger=%s", trigger_id, exc_info=True)


async def _notify_plan_finished(
    db, project_id: int, trigger_id: int, *, batch_id: int | None, error: str | None
) -> None:
    try:
        from app.models.notification import Notification

        title = "变更回归计划执行失败" if error else "变更回归计划已自动执行"
        message = (
            f"项目 #{project_id} 的计划 #{trigger_id}：{error[:200]}"
            if error
            else f"项目 #{project_id} 的计划 #{trigger_id} 已执行，批次 #{batch_id}"
        )
        for uid in await _admin_user_ids(db):
            db.add(
                Notification(
                    user_id=uid,
                    type="error" if error else "success",
                    title=title,
                    message=message,
                    batch_id=batch_id,
                )
            )
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("计划完成通知写入失败 trigger=%s", trigger_id, exc_info=True)


async def sync_and_plan_once(binding_id: int, *, trigger: str = "schedule") -> dict | None:
    """同步一个绑定并（如有变更）生成变更计划。"""
    from app.database import AsyncSessionLocal
    from app.models.api_extract import CodeRepoBinding
    from app.services.api_sync import sync_binding

    async with AsyncSessionLocal() as db:
        binding = await db.get(CodeRepoBinding, binding_id)
        if binding is None:
            return None
        outcome = await sync_binding(db, binding, trigger=trigger)
        return await plan_after_sync(db, binding, outcome)


async def detect_remote_head(binding) -> str | None:
    """获取远端分支 HEAD（不克隆）；失败返回 None。"""
    from asyncio import to_thread

    from app.services.api_sync import binding_auth
    from core.git_sync import ls_remote

    try:
        auth = binding_auth(binding)
    except Exception:
        return None
    try:
        refs = await to_thread(ls_remote, binding.repo_url, auth=auth, timeout=30)
    except Exception:
        return None
    branch = binding.branch or ""
    if branch:
        return refs.get(f"refs/heads/{branch}")
    return refs.get("HEAD")


async def watch_repositories_once(*, now: datetime | None = None) -> list[int]:
    """按 sync_interval_minutes 巡检 scheduled 绑定；返回触发同步的绑定 id。"""
    from app.database import AsyncSessionLocal
    from app.models.api_extract import CodeRepoBinding
    from app.services.api_sync import binding_auth  # noqa: F401 - 保持依赖显式

    settings = get_settings()
    now = now or tz_now()
    started: list[int] = []
    async with AsyncSessionLocal() as db:
        bindings = (
            await db.execute(
                select(CodeRepoBinding).where(CodeRepoBinding.sync_mode == "scheduled")
            )
        ).scalars().all()
        for binding in bindings:
            interval = int(
                binding.sync_interval_minutes or settings.api_extract_sync_interval_minutes
            )
            last = _aware(binding.last_sync_at)
            if last is not None and now - last < timedelta(minutes=interval):
                continue
            started.append(int(binding.id))
    import asyncio

    for binding_id in started:
        spawn_background(sync_and_plan_once(binding_id))
    return started


async def change_plan_loop(interval_seconds: int = 30) -> None:
    """每 N 秒扫描到期计划（一次任务只执行一次）。"""
    import asyncio

    while True:
        try:
            await run_due_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 调度循环不能因单次失败退出
            logger.exception("变更计划扫描失败")
        await asyncio.sleep(interval_seconds)


async def repository_watch_loop(interval_seconds: int = 300) -> None:
    """每 N 秒巡检仓库 HEAD。"""
    import asyncio

    while True:
        try:
            await watch_repositories_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("仓库巡检失败")
        await asyncio.sleep(interval_seconds)
