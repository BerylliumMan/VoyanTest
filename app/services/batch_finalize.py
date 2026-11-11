"""049：批次收尾 —— 等结束 → 汇总分类 → 自动重试 → 分级通知。

三个执行入口（接口执行、客户端桥接、仅失败重跑）都通过
``app.services.notifications.notify_batch_completed`` 进入这里，避免三套逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.config import get_settings
from app.services.failure_triage import decide_notification, summarize_runs

logger = logging.getLogger(__name__)


async def finalize_batch(
    batch_id: int,
    user_id: int | None,
    *,
    max_wait_seconds: int = 7200,
    poll_interval: float = 2.0,
) -> dict[str, Any] | None:
    """收尾一个批次：写分类摘要、必要时调度环境重试、按分类创建通知。"""
    from app import db_models
    from app.database import AsyncSessionLocal

    settings = get_settings()
    deadline = time.monotonic() + max_wait_seconds
    batch = None
    while time.monotonic() < deadline:
        async with AsyncSessionLocal() as db:
            batch = await db.get(db_models.RunBatch, batch_id)
            if batch is None:
                return None
            if batch.finished_at is not None or str(batch.status or "") not in ("running", "pending"):
                break
        batch = None
        await asyncio.sleep(poll_interval)
    if batch is None:
        logger.warning("批次 %s 在 %ss 内未完成，跳过收尾", batch_id, max_wait_seconds)
        return None

    # 1) 汇总分类并缓存
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        batch_row = await db.get(db_models.RunBatch, batch_id)
        if batch_row is None:
            return None
        runs = (
            await db.execute(
                select(db_models.TestRun).where(db_models.TestRun.batch_id == batch_id)
            )
        ).scalars().all()
        summary = summarize_runs(runs)
        batch_name = batch_row.name or f"#{batch_id}"
        is_retry_batch = bool(getattr(batch_row, "auto_retry", False))
        if settings.failure_triage_enabled:
            batch_row.failure_summary = summary
            await db.commit()
        # 1.5) 050：若批次属于测试轮次，回填轮次/计划项状态
        try:
            from app.services.test_cycle import sync_cycle_from_batch

            await sync_cycle_from_batch(db, batch_row)
        except Exception:  # noqa: BLE001 - 回填失败不应影响通知与重试
            logger.warning("批次 %s 轮次回填失败", batch_id, exc_info=True)

    # 2) 环境类失败自动重试（内部幂等）
    retry_info: dict[str, Any] | None = None
    if settings.failure_triage_enabled and summary["kinds"].get("env"):
        try:
            from app.services.failure_retry import maybe_schedule_retry

            retry_info = await maybe_schedule_retry(batch_id, user_id=user_id)
        except Exception:  # noqa: BLE001 - 重试调度失败不应影响通知
            logger.warning("批次 %s 自动重试调度失败", batch_id, exc_info=True)

    # 3) 按分类决定通知级别（标题沿用「批次「名称」运行完成」的既有口径）
    ntype, _, message = decide_notification(
        summary,
        notify_real_only=settings.failure_notify_real_only,
        triage_enabled=settings.failure_triage_enabled,
        auto_retry_enabled=settings.failure_auto_retry_enabled,
        retry_batch_id=(retry_info or {}).get("batch_id"),
        retried_cases=int((retry_info or {}).get("cases") or 0),
        is_retry_batch=is_retry_batch,
    )
    title = f"批次「{batch_name}」运行完成"

    if user_id:
        try:
            async with AsyncSessionLocal() as db:
                db.add(
                    db_models.Notification(
                        user_id=user_id,
                        type=ntype,
                        title=title,
                        message=message,
                        batch_id=batch_id,
                    )
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - 通知失败不影响执行
            logger.warning("批次 %s 通知创建失败", batch_id, exc_info=True)

    return {"summary": summary, "retry": retry_info, "notification": (ntype, title, message)}


__all__ = ["finalize_batch"]
