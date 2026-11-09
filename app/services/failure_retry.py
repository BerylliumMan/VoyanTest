"""049：环境类失败自动重试 —— 纯决策（plan_retry）+ 调度（maybe_schedule_retry）。"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from app.config import Settings, get_settings
from app.services.failure_triage import FAILURE_KINDS, RETRYABLE_KINDS

logger = logging.getLogger(__name__)

#: 永久禁止自动重试的类别（spec FR-007）；unknown 也无法安全重试
FORBIDDEN_RETRY_KINDS = frozenset({"assertion", "locator", "script", "unknown"})


def plan_retry(
    batch: Any,
    runs: Iterable[Any] | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """决定该批次是否需要自动重试（纯函数，便于单测）。

    返回 ``{"eligible": bool, "reason": str, "kinds": set, "case_ids": [...], "truncated": n}``。
    """
    settings = settings or get_settings()
    empty = {"eligible": False, "reason": "", "kinds": set(), "case_ids": [], "truncated": 0}

    if not settings.failure_auto_retry_enabled:
        return {**empty, "reason": "disabled"}
    if bool(getattr(batch, "auto_retry", False)):
        return {**empty, "reason": "already-auto-retry"}

    batch_status = str(getattr(batch, "status", "") or "")
    # 只有已结束的批次才允许派生重试；paused/cancelled/running 等一律跳过
    if batch_status not in ("passed", "partial", "failed"):
        return {**empty, "reason": f"batch-{batch_status or 'unknown'}"}

    configured = {
        str(kind)
        for kind in (settings.failure_auto_retry_kinds or list(RETRYABLE_KINDS))
        if str(kind) in FAILURE_KINDS
    }
    kinds = configured - FORBIDDEN_RETRY_KINDS
    if not kinds:
        return {**empty, "reason": "no-retryable-kind"}

    case_ids: list[int] = []
    for run in runs or ():
        if str(getattr(run, "status", "") or "") != "failed":
            continue
        if (getattr(run, "failure_kind", None) or "unknown") not in kinds:
            continue
        case_id = getattr(run, "case_id", None)
        if not case_id:
            continue
        case_id = int(case_id)
        if case_id not in case_ids:
            case_ids.append(case_id)

    if not case_ids:
        return {**empty, "reason": "no-retryable-failure", "kinds": kinds}

    limit = max(0, int(settings.failure_auto_retry_max_cases or 0))
    truncated = 0
    if limit and len(case_ids) > limit:
        truncated = len(case_ids) - limit
        case_ids = case_ids[:limit]

    return {
        "eligible": True,
        "reason": "ok",
        "kinds": kinds,
        "case_ids": case_ids,
        "truncated": truncated,
    }


async def _find_existing_retry(db, batch_id: int):
    """查该批次已派生的自动重试批次（幂等判定）。"""
    from sqlalchemy import select

    from app import db_models

    return (
        await db.execute(
            select(db_models.RunBatch)
            .where(
                db_models.RunBatch.parent_batch_id == batch_id,
                db_models.RunBatch.auto_retry.is_(True),
            )
            .order_by(db_models.RunBatch.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def maybe_schedule_retry(batch_id: int, *, user_id: int | None = None) -> dict[str, Any] | None:
    """批次结束后调度一次环境重试；不可重试或已存在重试批次时返回 None/既有批次。"""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app import db_models
    from app.database import AsyncSessionLocal
    from app.services.batch_rerun import create_rerun_batch

    settings = get_settings()
    async with AsyncSessionLocal() as db:
        batch = await db.get(db_models.RunBatch, batch_id)
        if batch is None:
            return None

        # 幂等：同一批次最多派生一个自动重试批次
        existing = await _find_existing_retry(db, batch_id)
        if existing is not None:
            return {
                "batch_id": existing.id,
                "cases": int(existing.total_cases or 0),
                "existing": True,
            }

        runs = (
            await db.execute(
                select(db_models.TestRun).where(db_models.TestRun.batch_id == batch_id)
            )
        ).scalars().all()
        plan = plan_retry(batch, runs, settings)
        if not plan["eligible"]:
            logger.debug("批次 %s 无需自动重试：%s", batch_id, plan["reason"])
            return None

        try:
            result = await create_rerun_batch(
                db,
                batch,
                kinds=set(plan["kinds"]),
                max_cases=int(settings.failure_auto_retry_max_cases or 0) or None,
                name_suffix="环境重试",
                auto_retry=True,
                user_id=user_id,
            )
        except IntegrityError:
            # 两个收尾协程可能同时通过上面的存在性检查；唯一索引
            # ux_run_batches_auto_retry_parent 兜底后回查已创建的批次。
            await db.rollback()
            existing = await _find_existing_retry(db, batch_id)
            if existing is None:
                logger.warning("批次 %s 自动重试唯一约束冲突但未找到既有批次", batch_id)
                return None
            return {
                "batch_id": existing.id,
                "cases": int(existing.total_cases or 0),
                "existing": True,
            }
        if result["batch"] is None:
            return None
        return {
            "batch_id": result["batch"].id,
            "cases": result["total"],
            "existing": False,
            "truncated": result["truncated"],
        }


__all__ = ["plan_retry", "maybe_schedule_retry"]
