# core/runner/_persistence.py
"""测试运行结果持久化到数据库。"""
import logging
from datetime import datetime
from typing import Optional

from app import crud
from app.database import SessionLocal

logger = logging.getLogger(__name__)


def save_run_results(
    case_id: int,
    status: str,
    start_time: datetime,
    end_time: datetime,
    duration: float,
    report_path: Optional[str],
    log_path: Optional[str],
    logs: list[dict],
    batch_id: Optional[int] = None,
    run_id: Optional[int] = None,
    is_init: bool = False,
) -> int | None:
    db = SessionLocal()
    try:
        from app import db_models

        if run_id:
            db_run = db.query(db_models.TestRun).filter(db_models.TestRun.id == run_id).first()
            if db_run:
                db_run.status = status
                db_run.start_time = start_time
                db_run.end_time = end_time
                db_run.duration = duration
                db_run.report_path = report_path
                db_run.log_path = log_path
            else:
                run_id = None

        if not run_id:
            db_run = db_models.TestRun(
                case_id=case_id,
                batch_id=batch_id,
                status=status,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                report_path=report_path,
                log_path=log_path,
                is_init=is_init,
            )
            db.add(db_run)
            db.commit()
            db.refresh(db_run)

        for log_entry in logs:
            db_log = db_models.RunLog(
                run_id=db_run.id,
                step_id=log_entry.get('step_id'),
                level=log_entry['level'],
                message=log_entry['message'],
                screenshot_path=log_entry.get('screenshot_path'),
            )
            db.add(db_log)
        db.commit()
        logger.info(f"Test run results saved, run ID = {db_run.id}, batch_id = {batch_id}")

        if batch_id:
            crud.update_batch_counters(db, batch_id, status)
        return db_run.id
    except Exception:
        db.rollback()
        logger.exception("Failed to save run results")
    finally:
        db.close()
