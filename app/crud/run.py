# app/crud/run.py - 测试运行 + 运行批次 CRUD
import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import db_models
from app.tz import now as tz_now

logger = logging.getLogger(__name__)


# ----------------------------
# 测试运行CRUD
# ----------------------------

def create_test_run(db: Session, case_id: int, status: str, start_time, end_time, duration: float = None, report_path: str = None, log_path: str = None) -> db_models.TestRun:
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
    db.commit()
    db.refresh(db_run)
    return db_run

def update_test_run_status(db: Session, run_id: int, status: str, end_time=None, duration: float = None, report_path: str = None) -> db_models.TestRun | None:
    """更新测试运行状态"""
    db_run = db.query(db_models.TestRun).filter(db_models.TestRun.id == run_id).first()
    if not db_run:
        return None
    db_run.status = status
    if end_time:
        db_run.end_time = end_time
    if duration is not None:
        db_run.duration = duration
    if report_path:
        db_run.report_path = report_path
    db.commit()
    db.refresh(db_run)
    return db_run

def create_run_log(db: Session, run_id: int, level: str, message: str, step_id: int = None, screenshot_path: str = None) -> db_models.RunLog:
    """创建运行日志"""
    db_log = db_models.RunLog(
        run_id=run_id,
        step_id=step_id,
        level=level,
        message=message,
        screenshot_path=screenshot_path
    )
    db.add(db_log)
    db.commit()
    db.refresh(db_log)
    return db_log


# ----------------------------
# 运行批次 CRUD
# ----------------------------

def create_run_batch(db: Session, project_id: int, name: str = "", total_cases: int = 0) -> db_models.RunBatch:
    """创建运行批次"""
    batch = db_models.RunBatch(
        project_id=project_id,
        name=name,
        status="running",
        total_cases=total_cases,
        passed=0,
        failed=0,
        started_at=tz_now(),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def get_run_batch(db: Session, batch_id: int) -> db_models.RunBatch | None:
    """获取运行批次"""
    return db.query(db_models.RunBatch).filter(db_models.RunBatch.id == batch_id).first()


def list_run_batches(db: Session, project_id: int = None, status: str = None, page: int = 1, size: int = 20, project_ids: list[int] = None) -> dict[str, Any]:
    """分页获取运行批次列表"""
    query = db.query(db_models.RunBatch)

    if project_ids:
        query = query.filter(db_models.RunBatch.project_id.in_(project_ids))
    elif project_id:
        query = query.filter(db_models.RunBatch.project_id == project_id)
    if status:
        query = query.filter(db_models.RunBatch.status == status)

    total = query.count()
    offset = (page - 1) * size
    items = query.order_by(db_models.RunBatch.created_at.desc()).offset(offset).limit(size).all()

    # 批量预加载所有批次下的 runs，避免 N+1
    batch_ids = [b.id for b in items]
    all_runs = {}
    if batch_ids:
        for run in db.query(db_models.TestRun).filter(db_models.TestRun.batch_id.in_(batch_ids)).all():
            all_runs.setdefault(run.batch_id, []).append(run)

    # 动态计算批次状态（plan.md 决策4）
    for batch in items:
        _compute_batch_status(db, batch, preloaded_runs=all_runs.get(batch.id))

    return {"total": total, "page": page, "size": size, "items": items}


def update_run_batch(db: Session, batch_id: int, name: str = None) -> db_models.RunBatch | None:
    """更新运行批次"""
    batch = get_run_batch(db, batch_id)
    if not batch:
        return None

    if name is not None:
        batch.name = name

    db.commit()
    db.refresh(batch)
    return batch


def delete_run_batch(db: Session, batch_id: int) -> bool:
    """删除运行批次及其关联的 TestRun 和 RunLog"""
    batch = get_run_batch(db, batch_id)
    if not batch:
        return False
    for run in batch.runs:
        db.query(db_models.RunLog).filter(db_models.RunLog.run_id == run.id).delete()
        db.delete(run)
    db.delete(batch)
    db.commit()
    return True


def update_batch_counters(db: Session, batch_id: int, case_status: str) -> db_models.RunBatch | None:
    """用例完成后更新批次计数和状态"""
    batch = get_run_batch(db, batch_id)
    if not batch:
        return None

    if case_status == "passed":
        batch.passed += 1
    elif case_status == "failed":
        batch.failed += 1

    # 动态计算状态
    _compute_batch_status(db, batch)

    # 如果所有用例都完成，设置 finished_at
    completed = batch.passed + batch.failed
    if completed >= batch.total_cases:
        batch.finished_at = tz_now()

    db.commit()
    db.refresh(batch)
    return batch


def _compute_batch_status(db: Session, batch, preloaded_runs: list = None) -> None:
    """动态计算批次状态，自动修复卡死的 pending 记录"""

    now = tz_now()

    runs = preloaded_runs
    if runs is None:
        runs = db.query(db_models.TestRun).filter(db_models.TestRun.batch_id == batch.id).all()
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
        db.flush()
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

def _apply_batch_project_filter(query, project_id: int | None, allowed_ids: list[int] | None):
    """对 RunBatch 查询应用项目权限过滤。

    规则（与原 report_router 行为一致）：
    - allowed_ids 不为空：表示该用户被限制到部分项目
        - 若 project_id 指定：用 project_id 精确过滤（调用方需自行保证其属于 allowed_ids）
        - 否则：用 allowed_ids IN 过滤
    - allowed_ids 为空（None）：不受限
        - 若 project_id 指定：用 project_id 精确过滤
        - 否则：不过滤
    """
    if allowed_ids is not None:
        if project_id:
            return query.filter(db_models.RunBatch.project_id == project_id)
        return query.filter(db_models.RunBatch.project_id.in_(allowed_ids))
    if project_id:
        return query.filter(db_models.RunBatch.project_id == project_id)
    return query


def _apply_case_project_filter(query, project_id: int | None, allowed_ids: list[int] | None):
    """对 TestCase 关联查询应用项目权限过滤（规则同上，作用于 TestCase 表）。"""
    if allowed_ids is not None:
        if project_id:
            return query.filter(db_models.TestCase.project_id == project_id)
        return query.filter(db_models.TestCase.project_id.in_(allowed_ids))
    if project_id:
        return query.filter(db_models.TestCase.project_id == project_id)
    return query


def get_run_statistics(
    db: Session,
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
    query = _apply_batch_project_filter(db.query(db_models.RunBatch), project_id, allowed_ids)
    query = query.filter(
        db_models.RunBatch.created_at >= start_date,
        db_models.RunBatch.created_at <= end_date,
    )

    total_batches = query.count()

    result = query.with_entities(
        func.sum(db_models.RunBatch.total_cases),
        func.sum(db_models.RunBatch.passed),
        func.sum(db_models.RunBatch.failed),
    ).first()

    total_cases_in_batches = result[0] or 0
    total_passed = result[1] or 0
    total_failed = result[2] or 0

    today_start = tz_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_batches = query.filter(db_models.RunBatch.created_at >= today_start).count()

    total_cases_query = db.query(db_models.TestCase)
    if project_id:
        total_cases_query = total_cases_query.filter(db_models.TestCase.project_id == project_id)
    total_cases = total_cases_query.count()

    return {
        "total_batches": total_batches,
        "today_batches": today_batches,
        "total_cases_in_batches": total_cases_in_batches,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "total_cases": total_cases,
    }


def get_run_trends(
    db: Session,
    start_date,
    end_date,
    project_id: int | None = None,
    allowed_ids: list[int] | None = None,
) -> list[tuple]:
    """按日期聚合批次趋势原始行。

    返回 [(date, passed, failed, total_cases), ...]，调用方负责按日聚合与空白日期填充。
    """
    query = db.query(
        func.date(db_models.RunBatch.created_at).label("date"),
        db_models.RunBatch.passed,
        db_models.RunBatch.failed,
        db_models.RunBatch.total_cases,
    ).filter(
        db_models.RunBatch.created_at >= start_date,
        db_models.RunBatch.created_at <= end_date,
    )

    query = _apply_batch_project_filter(query, project_id, allowed_ids)

    return query.all()


def list_recent_runs(
    db: Session,
    limit: int = 10,
    project_id: int | None = None,
    allowed_ids: list[int] | None = None,
) -> list[tuple]:
    """获取最近执行记录（含用例名）。

    返回 [(TestRun, case_name), ...]，按 start_time 倒序。
    """
    query = db.query(
        db_models.TestRun,
        db_models.TestCase.name,
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id,
    ).order_by(
        db_models.TestRun.start_time.desc(),
    ).limit(limit)

    query = _apply_case_project_filter(query, project_id, allowed_ids)

    return query.all()


def get_run_detail_with_case(db: Session, run_id: int):
    """获取单次执行详情（含所属用例名与项目ID），用于权限校验。

    返回 (TestRun, case_name, case_project_id) 或 None。
    """
    return db.query(
        db_models.TestRun,
        db_models.TestCase.name,
        db_models.TestCase.project_id,
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id,
    ).filter(
        db_models.TestRun.id == run_id,
    ).first()


def list_runs_with_case(
    db: Session,
    project_id: int | None = None,
    status: str | None = None,
    allowed_ids: list[int] | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页获取执行记录列表（含用例名），按 start_time 倒序。

    返回 {"total": int, "items": [(TestRun, case_name), ...]}。
    """
    query = db.query(
        db_models.TestRun,
        db_models.TestCase.name,
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id,
    )

    query = _apply_case_project_filter(query, project_id, allowed_ids)

    if status:
        query = query.filter(db_models.TestRun.status == status)

    total = query.count()
    offset = (page - 1) * size
    items = (
        query.order_by(db_models.TestRun.start_time.desc())
        .offset(offset)
        .limit(size)
        .all()
    )

    return {"total": total, "items": items}


def get_batch_detail_with_related(db: Session, batch_id: int) -> dict[str, Any]:
    """获取批次详情所需的所有关联数据（runs 与 cases），消除 N+1。

    返回 {"runs": [TestRun, ...], "cases": {case_id: TestCase}}。
    """
    runs = (
        db.query(db_models.TestRun)
        .filter(db_models.TestRun.batch_id == batch_id)
        .all()
    )

    case_ids = [r.case_id for r in runs]
    cases: dict[int, db_models.TestCase] = {}
    if case_ids:
        for c in db.query(db_models.TestCase).filter(db_models.TestCase.id.in_(case_ids)).all():
            cases[c.id] = c

    return {"runs": runs, "cases": cases}