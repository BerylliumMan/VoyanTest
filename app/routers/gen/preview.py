"""``GET /api/gen/status/{session_id}`` and ``GET /api/gen/preview/{session_id}``.

Both endpoints read from the in-memory session populated by
:mod:`app.routers.gen.upload`; they are kept together because the status
endpoint is a thin wrapper over the same ``AnalysisSession`` that the
preview endpoint serializes in full.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...auth import get_current_user
from ...database import get_async_db
from ...db_models import GenSession
from .schemas import GenPreviewItem, GenPreviewResponse, GenStatusResponse
from .state import _lock, _sessions

router = APIRouter()


@router.get("/status/{session_id}", response_model=GenStatusResponse)
async def get_status(session_id: str, user=Depends(get_current_user),
                     db=Depends(get_async_db)) -> GenStatusResponse:
    """Check analysis progress."""
    async with _lock:
        session = _sessions.get(session_id)
    if not session:
        try:
            stmt = select(GenSession).where(GenSession.id == session_id)
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            if not row:
                raise HTTPException(404, "Session not found")
            return GenStatusResponse(
                session_id=session_id,
                status=row.status or "analyzing",
                filename=row.filename or "",
                error_message=row.error_message or "",
                functional_points_count=row.functional_points_count or 0,
                test_cases_count=row.test_cases_count or 0,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("DB fallback failed")
            raise HTTPException(500, f"DB error: {e}") from e
    return GenStatusResponse(
        session_id=session.session_id,
        status=session.status,
        filename=session.filename,
        error_message=session.error_message,
        functional_points_count=len(session.functional_points),
        test_cases_count=len(session.test_cases),
    )


@router.get("/preview/{session_id}", response_model=GenPreviewResponse)
async def preview_results(session_id: str, user=Depends(get_current_user),
                          db=Depends(get_async_db)) -> GenPreviewResponse:
    """Preview generated functional points and test cases."""
    async with _lock:
        session = _sessions.get(session_id)
    if not session:
        stmt = (select(GenSession).where(GenSession.id == session_id)
                .options(selectinload(GenSession.functional_points),
                         selectinload(GenSession.test_cases)))
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        if not row or row.status != "completed":
            raise HTTPException(404, "Session not found or not yet completed")
        fps = [{"id": fp.id, "module": fp.module, "name": fp.name, "category": fp.category, "description": fp.description}
               for fp in row.functional_points] if row.functional_points else []
        tcs = [GenPreviewItem(
            test_case_id=tc.test_case_id, module=tc.module, title=tc.title,
            preconditions=tc.preconditions, test_steps=tc.test_steps,
            expected_result=tc.expected_result, priority=tc.priority,
        ) for tc in row.test_cases] if row.test_cases else []
        return GenPreviewResponse(session_id=session_id, functional_points=fps, test_cases=tcs)
    if session.status != "completed":
        raise HTTPException(400, f"分析尚未完成，当前状态: {session.status}")

    fps = [
        {"id": fp.id, "module": fp.module, "name": fp.name, "category": fp.category, "description": fp.description}
        for fp in session.functional_points
    ]
    tcs = [
        GenPreviewItem(
            test_case_id=tc.test_case_id,
            module=tc.module,
            title=tc.title,
            preconditions=tc.preconditions,
            test_steps=tc.test_steps,
            expected_result=tc.expected_result,
            priority=tc.priority,
        )
        for tc in session.test_cases
    ]
    return GenPreviewResponse(
        session_id=session_id,
        functional_points=fps,
        test_cases=tcs,
    )
