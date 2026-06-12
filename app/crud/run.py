# app/crud/run.py - 测试运行 + 运行批次 CRUD
import logging
from typing import Any

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


def list_run_batches(db: Session, project_id: int = None, status: str = None, page: int = 1, size: int = 20) -> dict[str, Any]:
    """分页获取运行批次列表"""
    query = db.query(db_models.RunBatch)

    if project_id:
        query = query.filter(db_models.RunBatch.project_id == project_id)
    if status:
        query = query.filter(db_models.RunBatch.status == status)

    total = query.count()
    offset = (page - 1) * size
    items = query.order_by(db_models.RunBatch.created_at.desc()).offset(offset).limit(size).all()

    # 动态计算批次状态（plan.md 决策4）
    for batch in items:
        _compute_batch_status(db, batch)

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


def _compute_batch_status(db: Session, batch) -> None:
    """动态计算批次状态，自动修复卡死的 pending 记录"""

    now = tz_now()

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