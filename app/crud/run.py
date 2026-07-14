# app/crud/run.py - 测试运行 + 运行批次 CRUD
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.tz import now as tz_now

logger = logging.getLogger(__name__)


# ----------------------------
# 测试运行CRUD
# ----------------------------

async def create_test_run(db: AsyncSession, case_id: int, status: str, start_time, end_time, duration: float = None, report_path: str = None, log_path: str = None) -> db_models.TestRun:
    """创建测试运行记录"""
    db_run = db_models.TestRun(
        case_id=case_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        duration=duration,
        report_path=report_path,
        log_path=log_path
    )
    db.add(db_run)
    await db.commit()
    await db.refresh(db_run)
    return db_run

async def update_test_run_status(db: AsyncSession, run_id: int, status: str, end_time=None, duration: float = None, report_path: str = None) -> db_models.TestRun | None:
    """更新测试运行状态"""
    result = await db.execute(
        select(db_models.TestRun).where(db_models.TestRun.id == run_id)
    )
    db_run = result.scalar_one_or_none()
    if not db_run:
        return None
    db_run.status = status
    if end_time:
        db_run.end_time = end_time
    if duration is not None:
        db_run.duration = duration
    if report_path:
        db_run.report_path = report_path
    await db.commit()
    await db.refresh(db_run)
    return db_run

async def create_run_log(db: AsyncSession, run_id: int, level: str, message: str, step_id: int = None, screenshot_path: str = None) -> db_models.RunLog:
    """创建运行日志"""
    db_log = db_models.RunLog(
        run_id=run_id,
        step_id=step_id,
        level=level,
        message=message,
        screenshot_path=screenshot_path
    )
    db.add(db_log)
    await db.commit()
    await db.refresh(db_log)
    return db_log


# ----------------------------
# 运行批次 CRUD
# ----------------------------

async def create_run_batch(db: AsyncSession, project_id: int, name: str = "", total_cases: int = 0, triggered_by: str | None = None) -> db_models.RunBatch:
    """创建运行批次"""
    batch = db_models.RunBatch(
        project_id=project_id,
        name=name,
        status="running",
        total_cases=total_cases,
        passed=0,
        failed=0,
        started_at=tz_now(),
        triggered_by=triggered_by,
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch


async def get_run_batch(db: AsyncSession, batch_id: int) -> db_models.RunBatch | None:
    """获取运行批次"""
    result = await db.execute(
        select(db_models.RunBatch).where(db_models.RunBatch.id == batch_id)
    )
    return result.scalar_one_or_none()


async def list_run_batches(db: AsyncSession, project_id: int = None, status: str = None, page: int = 1, size: int = 20, project_ids: list[int] = None) -> dict[str, Any]:
    """分页获取运行批次列表"""
    stmt = select(db_models.RunBatch)

    if project_ids:
        stmt = stmt.where(db_models.RunBatch.project_id.in_(project_ids))
    elif project_id:
        stmt = stmt.where(db_models.RunBatch.project_id == project_id)
    if status:
        stmt = stmt.where(db_models.RunBatch.status == status)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * size
    items_stmt = (
        stmt.order_by(db_models.RunBatch.created_at.desc())
        .offset(offset)
        .limit(size)
    )
    items = (await db.execute(items_stmt)).scalars().all()

    # 批量预加载所有批次下的 runs，避免 N+1
    batch_ids = [b.id for b in items]
    all_runs: dict[int, list[db_models.TestRun]] = {}
    if batch_ids:
        runs_result = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.batch_id.in_(batch_ids))
        )
        for run in runs_result.scalars().all():
            all_runs.setdefault(run.batch_id, []).append(run)

    # 动态计算批次状态（plan.md 决策4）
    for batch in items:
        await _compute_batch_status(db, batch, preloaded_runs=all_runs.get(batch.id))

    return {"total": total, "page": page, "size": size, "items": items}


async def update_run_batch(db: AsyncSession, batch_id: int, name: str = None) -> db_models.RunBatch | None:
    """更新运行批次"""
    batch = await get_run_batch(db, batch_id)
    if not batch:
        return None

    if name is not None:
        batch.name = name

    await db.commit()
    await db.refresh(batch)
    return batch


async def delete_run_batch(db: AsyncSession, batch_id: int) -> bool:
    """删除运行批次及其关联的 TestRun 和 RunLog"""
    batch = await get_run_batch(db, batch_id)
    if not batch:
        return False

    # 显式加载 runs，避免 AsyncSession 下的隐式懒加载
    runs_result = await db.execute(
        select(db_models.TestRun).where(db_models.TestRun.batch_id == batch_id)
    )
    runs = runs_result.scalars().all()

    for run in runs:
        await db.execute(
            delete(db_models.RunLog).where(db_models.RunLog.run_id == run.id)
        )
        await db.delete(run)
    await db.delete(batch)
    await db.commit()
    return True


async def update_batch_counters(db: AsyncSession, batch_id: int, case_status: str) -> db_models.RunBatch | None:
    """用例完成后更新批次计数和状态"""
    batch = await get_run_batch(db, batch_id)
    if not batch:
        return None

    if case_status == "passed":
        batch.passed += 1
    elif case_status == "failed":
        batch.failed += 1

    # 动态计算状态
    await _compute_batch_status(db, batch)

    # 如果所有用例都完成，设置 finished_at
    completed = batch.passed + batch.failed
    if completed >= batch.total_cases:
        batch.finished_at = tz_now()

    await db.commit()
    await db.refresh(batch)
    return batch


async def _compute_batch_status(db: AsyncSession, batch, preloaded_runs: list = None) -> None:
    """动态计算批次状态，自动修复卡死的 pending 记录"""

    now = tz_now()

    runs = preloaded_runs
    if runs is None:
        runs_result = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.batch_id == batch.id)
        )
        runs = runs_result.scalars().all()
    if not runs:
        # 超过 30 秒仍无 TestRun 记录 → 后台任务已死
        created = batch.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)
        if created and (now - created).total_seconds() > 30:
            batch.status = "failed"
            batch.finished_at = now
        else:
            batch.status = "running"
        return

    counts = {"pending": 0, "running": 0, "passed": 0, "failed": 0}
    for r in runs:
        counts[r.status] = counts.get(r.status, 0) + 1

    # 只修复卡死的 pending 记录（超过 30 秒仍未进入 running）
    stuck_pending = False
    for r in runs:
        if r.status == "pending" and r.start_time:
            st = r.start_time
            if st.tzinfo is None:
                st = st.replace(tzinfo=now.tzinfo)
            if (now - st).total_seconds() > 30:
                r.status = "failed"
                r.end_time = now
                r.duration = 0.0
                r._stuck_marked = True
                stuck_pending = True

    if stuck_pending:
        await db.flush()
        counts["pending"] = 0
        stuck_failed = sum(1 for r in runs if getattr(r, "status", "") == "failed" and getattr(r, "_stuck_marked", False))
        counts["failed"] = counts.get("failed", 0) + stuck_failed

    # 实际已完成（passed + failed）的用例数
    completed = counts.get("passed", 0) + counts.get("failed", 0)
    running = counts.get("running", 0) + counts.get("pending", 0)

    # 同步批次计数器（仅用 confirmed 状态）
    batch.passed = counts.get("passed", 0)
    batch.failed = counts.get("failed", 0)

    if running > 0:
        batch.status = "running"
    elif completed >= batch.total_cases:
        batch.status = "passed" if counts.get("failed", 0) == 0 else "failed"
    else:
        batch.status = "partial"


# ----------------------------
# 报告查询（统计 / 趋势 / 详情 / 列表）
# ----------------------------

def _get_batch_project_filter(project_id: int | None, allowed_ids: list[int] | None):
    """返回 RunBatch 的项目权限过滤条件（可直接用于 select.where）。

    规则（与原 report_router 行为一致）：
    - allowed_ids 不为空：表示该用户被限制到部分项目
        - 若 project_id 指定：用 project_id 精确过滤（调用方需自行保证其属于 allowed_ids）
        - 否则：用 allowed_ids IN 过滤
    - allowed_ids 为空（None）：不受限
        - 若 project_id 指定：用 project_id 精确过滤
        - 否则：无过滤条件
    """
    if allowed_ids is not None:
        if project_id:
            return db_models.RunBatch.project_id == project_id
        return db_models.RunBatch.project_id.in_(allowed_ids)
    if project_id:
        return db_models.RunBatch.project_id == project_id
    return None


def _get_case_project_filter(project_id: int | None, allowed_ids: list[int] | None):
    """返回 TestCase 的项目权限过滤条件（规则同上，作用于 TestCase 表）。"""
    if allowed_ids is not None:
        if project_id:
            return db_models.TestCase.project_id == project_id
        return db_models.TestCase.project_id.in_(allowed_ids)
    if project_id:
        return db_models.TestCase.project_id == project_id
    return None


async def get_run_statistics(
    db: AsyncSession,
    start_date,
    end_date,
    project_id: int | None = None,
    allowed_ids: list[int] | None = None,
) -> dict[str, Any]:
    """聚合指定时间窗口内的批次统计数据。

    返回字段：
    - total_batches: 时间窗口内的批次数
    - today_batches: 今日创建的批次数
    - total_cases_in_batches: 所有批次的 total_cases 累计
    - total_passed: 所有批次的 passed 累计
    - total_failed: 所有批次的 failed 累计
    - total_cases: TestCase 表的项目用例总数（独立计算）
    """
    base_filters = [
        db_models.RunBatch.created_at >= start_date,
        db_models.RunBatch.created_at <= end_date,
    ]
    project_filter = _get_batch_project_filter(project_id, allowed_ids)
    if project_filter is not None:
        base_filters.append(project_filter)

    total_batches_stmt = select(func.count()).select_from(db_models.RunBatch).where(*base_filters)
    total_batches = (await db.execute(total_batches_stmt)).scalar_one()

    sums_stmt = select(
        func.sum(db_models.RunBatch.total_cases),
        func.sum(db_models.RunBatch.passed),
        func.sum(db_models.RunBatch.failed),
    ).where(*base_filters)
    result = (await db.execute(sums_stmt)).first()
    total_cases_in_batches = result[0] or 0
    total_passed = result[1] or 0
    total_failed = result[2] or 0

    today_start = tz_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_filters = [*base_filters, db_models.RunBatch.created_at >= today_start]
    today_batches_stmt = select(func.count()).select_from(db_models.RunBatch).where(*today_filters)
    today_batches = (await db.execute(today_batches_stmt)).scalar_one()

    testcase_filters = []
    if project_id:
        testcase_filters.append(db_models.TestCase.project_id == project_id)
    testcase_count_stmt = select(func.count()).select_from(db_models.TestCase)
    if testcase_filters:
        testcase_count_stmt = testcase_count_stmt.where(*testcase_filters)
    total_cases = (await db.execute(testcase_count_stmt)).scalar_one()

    return {
        "total_batches": total_batches,
        "today_batches": today_batches,
        "total_cases_in_batches": total_cases_in_batches,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "total_cases": total_cases,
    }


async def get_run_trends(
    db: AsyncSession,
    start_date,
    end_date,
    project_id: int | None = None,
    allowed_ids: list[int] | None = None,
) -> list[tuple]:
    """按日期聚合批次趋势原始行。

    返回 [(date, passed, failed, total_cases), ...]，调用方负责按日聚合与空白日期填充。
    """
    stmt = select(
        func.date(db_models.RunBatch.created_at).label("date"),
        db_models.RunBatch.passed,
        db_models.RunBatch.failed,
        db_models.RunBatch.total_cases,
    ).where(
        db_models.RunBatch.created_at >= start_date,
        db_models.RunBatch.created_at <= end_date,
    )

    project_filter = _get_batch_project_filter(project_id, allowed_ids)
    if project_filter is not None:
        stmt = stmt.where(project_filter)

    result = await db.execute(stmt)
    return result.all()


async def list_recent_runs(
    db: AsyncSession,
    limit: int = 10,
    project_id: int | None = None,
    allowed_ids: list[int] | None = None,
) -> list[tuple]:
    """获取最近执行记录（含用例名）。

    返回 [(TestRun, case_name), ...]，按 start_time 倒序。
    """
    stmt = (
        select(db_models.TestRun, db_models.TestCase.name)
        .join(
            db_models.TestCase,
            db_models.TestRun.case_id == db_models.TestCase.id,
        )
        .order_by(db_models.TestRun.start_time.desc())
        .limit(limit)
    )

    project_filter = _get_case_project_filter(project_id, allowed_ids)
    if project_filter is not None:
        stmt = stmt.where(project_filter)

    result = await db.execute(stmt)
    return result.all()


async def get_run_detail_with_case(db: AsyncSession, run_id: int):
    """获取单次执行详情（含所属用例名与项目ID），用于权限校验。

    返回 (TestRun, case_name, case_project_id) 或 None。
    """
    stmt = (
        select(
            db_models.TestRun,
            db_models.TestCase.name,
            db_models.TestCase.project_id,
        )
        .join(
            db_models.TestCase,
            db_models.TestRun.case_id == db_models.TestCase.id,
        )
        .where(db_models.TestRun.id == run_id)
    )
    result = await db.execute(stmt)
    return result.first()


async def list_runs_with_case(
    db: AsyncSession,
    project_id: int | None = None,
    status: str | None = None,
    allowed_ids: list[int] | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页获取执行记录列表（含用例名），按 start_time 倒序。

    返回 {"total": int, "items": [(TestRun, case_name), ...]}。
    """
    stmt = select(
        db_models.TestRun,
        db_models.TestCase.name,
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id,
    )

    project_filter = _get_case_project_filter(project_id, allowed_ids)
    if project_filter is not None:
        stmt = stmt.where(project_filter)

    if status:
        stmt = stmt.where(db_models.TestRun.status == status)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * size
    items_stmt = (
        stmt.order_by(db_models.TestRun.start_time.desc())
        .offset(offset)
        .limit(size)
    )
    items = (await db.execute(items_stmt)).all()

    return {"total": total, "items": items}


async def get_batch_detail_with_related(db: AsyncSession, batch_id: int) -> dict[str, Any]:
    """获取批次详情所需的所有关联数据（runs 与 cases），消除 N+1。

    返回 {"runs": [TestRun, ...], "cases": {case_id: TestCase}}。
    """
    runs_result = await db.execute(
        select(db_models.TestRun).where(db_models.TestRun.batch_id == batch_id)
    )
    runs = runs_result.scalars().all()

    case_ids = [r.case_id for r in runs]
    cases: dict[int, db_models.TestCase] = {}
    if case_ids:
        cases_result = await db.execute(
            select(db_models.TestCase).where(db_models.TestCase.id.in_(case_ids))
        )
        for c in cases_result.scalars().all():
            cases[c.id] = c

    return {"runs": runs, "cases": cases}