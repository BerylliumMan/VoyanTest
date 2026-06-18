"""``POST /api/gen/import`` — take the (in-memory or DB-persisted) analysis
result for a session and import the user-selected test cases into a real
project via :mod:`app.gen.adapter`.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import db_models
from ...auth import get_current_user
from ...database import get_db
from .schemas import GenImportRequest, GenImportResponse
from .state import _lock, _sessions

router = APIRouter()


@router.post("/import", response_model=GenImportResponse)
async def import_test_cases(
    body: GenImportRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> GenImportResponse:
    """Import selected test cases into a project."""
    from app.gen.models import TestCase as GenTestCaseModel

    loop = asyncio.get_running_loop()
    test_cases_data = None

    async with _lock:
        session = _sessions.get(body.session_id)
    if session and session.status == "completed":
        test_cases_data = session.test_cases
    else:
        record = await loop.run_in_executor(None, lambda: db.query(db_models.GenSession).filter(db_models.GenSession.id == body.session_id).first())
        if not record:
            raise HTTPException(404, "记录不存在")
        if record.status != "completed":
            raise HTTPException(400, "分析尚未完成")
        db_tcs = await loop.run_in_executor(None, lambda: db.query(db_models.GenTestCase).filter(
            db_models.GenTestCase.session_id == body.session_id
        ).all())
        test_cases_data = [
            GenTestCaseModel(
                test_case_id=tc.test_case_id,
                session_id=body.session_id,
                module=tc.module or "",
                title=tc.title or "",
                preconditions=tc.preconditions or "",
                test_steps=tc.test_steps or "",
                expected_result=tc.expected_result or "",
                priority=tc.priority or "中",
            )
            for tc in db_tcs
        ]

    project = await loop.run_in_executor(None, lambda: db.query(db_models.Project).filter(db_models.Project.id == body.project_id).first())
    if not project:
        raise HTTPException(404, "项目不存在")

    from app.gen.adapter import import_test_cases as do_import
    created = await loop.run_in_executor(None, lambda: do_import(db, body.project_id, test_cases_data, body.selected_ids))

    record = await loop.run_in_executor(None, lambda: db.query(db_models.GenSession).filter(db_models.GenSession.id == body.session_id).first())
    if record:
        record.imported_count = (record.imported_count or 0) + len(created)
        if record.project_id is None:
            record.project_id = body.project_id
        await loop.run_in_executor(None, lambda: db.commit())

    return GenImportResponse(
        imported_count=len(created),
        test_case_ids=[tc.id for tc in created],
    )
