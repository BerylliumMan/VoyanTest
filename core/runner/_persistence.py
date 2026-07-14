# core/runner/_persistence.py
"""测试运行结果持久化到数据库。

本模块职责：
    1. 公开 API：save_run_results（创建/更新单条 TestRun + 关联日志 + 批次计数）
    2. DB 辅助：mark_run_running / mark_run_failed / update_run_on_completion
       / precreate_pending_runs / append_run_logs
       — 把 _execution.py 和 _orchestrator.py 中重复的 SQL 收敛到一处

SessionLocal 使用说明
---------------------
save_run_results 是公共 API 入口，调用方不一定持有 db session（例如
从 HTTP handler 收到结果后直接落库），因此这里保留 `AsyncSessionLocal()`
自管理生命周期。其他辅助函数（mark_*/precreate_*/update_*/append_*）都
接收外部传入的 db session，由调用方负责 commit / rollback / close，
避免重复打开连接。
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, database as db_mod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 公开 API — 独立 session 生命周期（HTTP handler 友好）
# ---------------------------------------------------------------------------


async def save_run_results(
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
    async with db_mod.AsyncSessionLocal() as db:
        try:
            from app import db_models

            if run_id:
                result = await db.execute(
                    select(db_models.TestRun).where(db_models.TestRun.id == run_id)
                )
                db_run = result.scalar_one_or_none()
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
                await db.commit()
                await db.refresh(db_run)

            for log_entry in logs:
                db_log = db_models.RunLog(
                    run_id=db_run.id,
                    step_id=log_entry.get('step_id'),
                    level=log_entry['level'],
                    message=log_entry['message'],
                    screenshot_path=log_entry.get('screenshot_path'),
                )
                db.add(db_log)
            await db.commit()
            logger.info("Test run results saved, run ID = %s, batch_id = %s", db_run.id, batch_id)

            if batch_id:
                await crud.update_batch_counters(db, batch_id, status)
                await db.commit()
            return db_run.id
        except SQLAlchemyError:
            await db.rollback()
            logger.exception("Failed to save run results")
            return None


# ---------------------------------------------------------------------------
# DB 辅助函数 — 接受外部 session，统一收敛重复 SQL
# ---------------------------------------------------------------------------


async def mark_run_running(db: AsyncSession, run_id: int) -> bool:
    """将预创建的 pending TestRun 状态更新为 running。

    Returns True 当至少一行被更新；False 表示 run_id 不存在。
    """
    from app import db_models

    stmt = (
        update(db_models.TestRun)
        .where(db_models.TestRun.id == run_id)
        .values(status="running")
    )
    r = await db.execute(stmt)
    await db.commit()
    return r.rowcount > 0


async def mark_run_failed(
    db: AsyncSession,
    run_id: int,
    message: str,
    batch_id: int | None = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> None:
    """将 TestRun 标记为 failed 并追加一条 CRITICAL 日志。

    如果提供了 batch_id，还会调用 ``crud.update_batch_counters`` 更新
    批次计数。异常会 rollback 并记录日志，不向调用方抛出。
    """
    from app import db_models
    from app.tz import now as tz_now

    try:
        _now = end_time or tz_now()
        _start = start_time or _now
        stmt = (
            update(db_models.TestRun)
            .where(db_models.TestRun.id == run_id)
            .values(
                status="failed",
                start_time=_start,
                end_time=_now,
                duration=(_now - _start).total_seconds() if _start and _now else 0.0,
            )
        )
        await db.execute(stmt)
        _log = db_models.RunLog(
            run_id=run_id, step_id=None, level="CRITICAL",
            message=message, screenshot_path=None,
        )
        db.add(_log)
        await db.commit()
        if batch_id:
            try:
                await crud.update_batch_counters(db, batch_id, "failed")
                await db.commit()
            except SQLAlchemyError:
                logger.exception("Failed to update batch counters")
                await db.rollback()
    except SQLAlchemyError:
        logger.exception("Failed to mark run %s as failed", run_id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - rollback 自身已失败，避免再次抛错
            pass


async def update_run_on_completion(
    db: AsyncSession,
    run_id: int,
    status: str,
    start_time: datetime,
    end_time: datetime,
    duration: float,
    report_path: Optional[str],
    run_log_entries: list[dict],
    batch_id: Optional[int] = None,
) -> None:
    """用例完成时统一更新 TestRun + 追加 RunLog + 更新批次计数。

    失败会 rollback 但不抛出 — 调用方通常已经在异常路径上。
    """
    from app import db_models

    try:
        stmt = (
            update(db_models.TestRun)
            .where(db_models.TestRun.id == run_id)
            .values(
                status=status,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                report_path=report_path,
                log_path=None,
            )
        )
        await db.execute(stmt)
        await append_run_logs(db, run_id, run_log_entries)
        await db.commit()
        if batch_id:
            try:
                await crud.update_batch_counters(db, batch_id, status)
                await db.commit()
            except SQLAlchemyError:
                logger.exception("Failed to update batch counters")
                await db.rollback()
    except SQLAlchemyError:
        logger.exception("Failed to update TestRun %s", run_id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - rollback 自身已失败，避免再次抛错
            pass


async def append_run_logs(db: AsyncSession, run_id: int, log_entries: list[dict]) -> None:
    """批量追加 RunLog。失败时 rollback，但不抛出（供非关键路径调用）。"""
    from app import db_models

    try:
        for log_entry in log_entries:
            db_log = db_models.RunLog(
                run_id=run_id,
                step_id=log_entry.get('step_id'),
                level=log_entry['level'],
                message=log_entry['message'],
                screenshot_path=log_entry.get('screenshot_path'),
            )
            db.add(db_log)
        await db.commit()
    except SQLAlchemyError:
        logger.exception("Failed to append run logs for run %s", run_id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - rollback 自身已失败，避免再次抛错
            pass


async def precreate_pending_runs(
    db: AsyncSession,
    case_ids: list[int],
    batch_id: int,
    init_case_ids: list[int] | None = None,
) -> dict[int, int]:
    """在浏览器启动之前预创建 pending TestRun 记录。

    Returns ``{case_id: run_id}`` 映射。失败会 rollback 并返回已创建的
    部分映射（best-effort）。``init_case_ids`` 内的用例会被标记为
    ``is_init=True``。
    """
    from app import db_models

    precreated: dict[int, int] = {}
    try:
        for cid in (init_case_ids or []):
            pending_run = db_models.TestRun(
                case_id=cid, batch_id=batch_id, status="pending",
                start_time=None, end_time=None, is_init=True,
            )
            db.add(pending_run)
            await db.flush()
            precreated[cid] = pending_run.id
        for cid in case_ids:
            pending_run = db_models.TestRun(
                case_id=cid, batch_id=batch_id, status="pending",
                start_time=None, end_time=None,
            )
            db.add(pending_run)
            await db.flush()
            precreated[cid] = pending_run.id
        await db.commit()
    except SQLAlchemyError:
        logger.exception("Failed to pre-create TestRun records")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - rollback 自身已失败，避免再次抛错
            pass
    return precreated
