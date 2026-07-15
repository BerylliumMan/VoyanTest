"""``POST /api/gen/import`` — take the (in-memory or DB-persisted) analysis
result for a session and import the user-selected test cases into a real
project via :mod:`app.gen.adapter`.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import crud
from ...auth import get_current_user, get_user_project_filter
from ...database import get_async_db
from .schemas import GenImportRequest, GenImportResponse
from .state import _lock, _sessions

router = APIRouter()


@router.post("/import", response_model=GenImportResponse)
async def import_test_cases(
    body: GenImportRequest,
    db: AsyncSession = Depends(get_async_db),
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
        record = await crud.get_gen_session(db, body.session_id)
        if not record:
            raise HTTPException(404, "记录不存在")
        if record.status != "completed":
            raise HTTPException(400, "分析尚未完成")
        db_tcs = await crud.list_gen_test_cases(db, body.session_id)
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

    project = await crud.get_project(db, body.project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    # 项目权限检查
    allowed_project_ids = get_user_project_filter(user)
    if allowed_project_ids is not None and body.project_id not in allowed_project_ids:
        raise HTTPException(403, "无权限操作该项目")

    from app.gen.adapter import import_test_cases as do_import
    created = await do_import(db, body.project_id, test_cases_data, body.selected_ids)

    await crud.increment_imported_count(db, body.session_id, body.project_id, len(created))

    return GenImportResponse(
        imported_count=len(created),
        test_case_ids=[tc.id for tc in created],
    )
