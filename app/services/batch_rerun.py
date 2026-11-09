"""049：「仅失败重跑」下沉为服务，供手动路由与自动重试共用。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _collect_failed(runs: Iterable[Any], kinds: set[str] | None) -> tuple[list[int], dict[int, list[int]]]:
    case_ids: list[int] = []
    groups: dict[int, list[int]] = {}
    for run in runs or ():
        if str(getattr(run, "status", "") or "") != "failed":
            continue
        case_id = getattr(run, "case_id", None)
        if not case_id:
            continue
        if kinds is not None and (getattr(run, "failure_kind", None) or "unknown") not in kinds:
            continue
        if case_id not in case_ids:
            case_ids.append(int(case_id))
        group = getattr(run, "dataset_group", None)
        if group is not None:
            groups.setdefault(int(case_id), []).append(int(group))
    return case_ids, groups


async def create_rerun_batch(
    db: AsyncSession,
    batch,
    *,
    kinds: set[str] | None = None,
    max_cases: int | None = None,
    name_suffix: str = "仅失败重跑",
    auto_retry: bool = False,
    triggered_by: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """按原批次的失败用例创建子批次并异步执行。

    :param kinds: 只重跑这些失败类别；``None`` 表示全部失败用例（031 语义）
    :param max_cases: 按用例数截断（自动重试用于限流）
    """
    from app import db_models
    from app.crud import run as crud_run
    from core.runner._persistence import (
        build_batch_execution_queue,
        build_dataset_execution_queue,
    )

    rows = list(
        (
            await db.execute(
                select(db_models.TestRun).where(db_models.TestRun.batch_id == batch.id)
            )
        )
        .scalars()
        .all()
    )
    case_ids, groups = _collect_failed(rows, kinds)
    truncated = 0
    if max_cases is not None and len(case_ids) > max_cases:
        truncated = len(case_ids) - max_cases
        dropped = set(case_ids[max_cases:])
        case_ids = case_ids[:max_cases]
        groups = {cid: gs for cid, gs in groups.items() if cid not in dropped}
    if not case_ids:
        return {"batch": None, "total": 0, "case_ids": [], "truncated": 0}

    if groups:
        groups_map = {cid: list(dict.fromkeys(gs)) for cid, gs in groups.items()}
        total = len(
            build_dataset_execution_queue(
                case_ids, [], "before_each",
                {cid: [{} for _ in gs] for cid, gs in groups_map.items()},
            )
        )
    else:
        groups_map = None
        total = len(build_batch_execution_queue(case_ids, [], "before_each"))

    name = f"{(batch.name or f'批次 #{batch.id}')} · {name_suffix}"
    new_batch = await crud_run.create_run_batch(
        db,
        project_id=batch.project_id,
        name=name,
        total_cases=total,
        triggered_by=triggered_by,
        parent_batch_id=batch.id,
        auto_retry=auto_retry,
    )

    async def _run() -> None:
        from app.routers.testcase import execution as _exec

        try:
            await _exec.run_batch_test_cases(
                case_ids,
                batch.project_id,
                batch_id=new_batch.id,
                dataset_group_filter=groups_map,
            )
        except Exception:  # noqa: BLE001 - 重跑失败不应影响已创建批次
            logger.exception("重跑批次执行失败 batch=%s", new_batch.id)
        finally:
            if user_id:
                try:
                    from app.services.notifications import notify_batch_completed

                    await notify_batch_completed(new_batch.id, user_id)
                except Exception:  # noqa: BLE001 - 通知失败不影响批次
                    logger.warning("重跑批次通知失败 batch=%s", new_batch.id, exc_info=True)

    asyncio.create_task(_run())
    return {
        "batch": new_batch,
        "total": total,
        "case_ids": case_ids,
        "truncated": truncated,
    }


__all__ = ["create_rerun_batch"]
