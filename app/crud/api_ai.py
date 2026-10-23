# app/crud/api_ai.py
# 040 接口测试 AI 分析缓存 CRUD
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.tz import now as tz_now

MAX_RESULT_BYTES = 64 * 1024


def _result_bytes(result: Any) -> int:
    try:
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return MAX_RESULT_BYTES + 1


async def get_analysis(
    db: AsyncSession,
    *,
    project_id: int,
    kind: str,
    run_id: int | None = None,
    input_hash: str | None = None,
) -> db_models.ApiAiAnalysis | None:
    q = select(db_models.ApiAiAnalysis).where(
        db_models.ApiAiAnalysis.project_id == project_id,
        db_models.ApiAiAnalysis.kind == kind,
    )
    if run_id is not None:
        q = q.where(db_models.ApiAiAnalysis.run_id == run_id)
    if input_hash is not None:
        q = q.where(db_models.ApiAiAnalysis.input_hash == input_hash)
    q = q.order_by(db_models.ApiAiAnalysis.id.desc()).limit(1)
    return (await db.execute(q)).scalar_one_or_none()


async def upsert_analysis(
    db: AsyncSession,
    *,
    project_id: int,
    kind: str,
    result: dict,
    run_id: int | None = None,
    case_id: int | None = None,
    input_hash: str | None = None,
    model: str | None = None,
    created_by: int | None = None,
    status: str = "ready",
) -> db_models.ApiAiAnalysis:
    """按缓存键 upsert；result 超过 64KB 拒绝（调用方转 503/400）。"""
    if _result_bytes(result) > MAX_RESULT_BYTES:
        raise ValueError("AI 分析结果超过 64KB 上限")
    row = await get_analysis(
        db, project_id=project_id, kind=kind, run_id=run_id, input_hash=input_hash,
    )
    if row is None:
        row = db_models.ApiAiAnalysis(
            project_id=project_id, run_id=run_id, case_id=case_id, kind=kind,
            status=status, input_hash=input_hash, result=result, model=model,
            created_by=created_by,
        )
        db.add(row)
    else:
        row.status = status
        row.result = result
        row.model = model
        row.input_hash = input_hash
        row.case_id = case_id or row.case_id
        row.created_by = created_by or row.created_by
        row.stale_at = None
        row.updated_at = tz_now()
    await db.commit()
    await db.refresh(row)
    return row


async def mark_stale_for_case(db: AsyncSession, case_id: int) -> int:
    rows = (
        await db.execute(
            select(db_models.ApiAiAnalysis).where(
                db_models.ApiAiAnalysis.case_id == case_id,
                db_models.ApiAiAnalysis.stale_at.is_(None),
            )
        )
    ).scalars().all()
    for row in rows:
        row.stale_at = tz_now()
    if rows:
        await db.commit()
    return len(rows)


async def mark_stale_for_project(db: AsyncSession, project_id: int) -> int:
    rows = (
        await db.execute(
            select(db_models.ApiAiAnalysis).where(
                db_models.ApiAiAnalysis.project_id == project_id,
                db_models.ApiAiAnalysis.stale_at.is_(None),
            )
        )
    ).scalars().all()
    for row in rows:
        row.stale_at = tz_now()
    if rows:
        await db.commit()
    return len(rows)


def serialize_analysis(row: db_models.ApiAiAnalysis) -> dict:
    return {
        "analysis_id": row.id,
        "project_id": row.project_id,
        "run_id": row.run_id,
        "case_id": row.case_id,
        "kind": row.kind,
        "status": row.status,
        "stale": row.stale_at is not None,
        "stale_at": row.stale_at.isoformat() if row.stale_at else None,
        "result": row.result,
        "model": row.model,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
