"""``/api/gen/history`` family of endpoints — they all read from (or mutate)
the persistent ``GenSession`` table.  In-memory cleanup of the matching
session is also done here for parity with the original behavior.
"""
from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ... import crud
from ...auth import get_current_user
from ...database import get_db
from .schemas import (
    GenHistoryItem,
    GenHistoryListResponse,
    GenPreviewItem,
    GenPreviewResponse,
    GenTestCaseUpdate,
)
from .state import _lock, _sessions

router = APIRouter()


@router.get("/history", response_model=GenHistoryListResponse)
async def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    project_id: Optional[int] = Query(None, description="按项目筛选"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> GenHistoryListResponse:
    """Get analysis history list."""

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: crud.gen.list_gen_sessions(db, page=page, page_size=page_size, project_id=project_id))
    items = result["items"]
    total = result["total"]

    return GenHistoryListResponse(
        items=[
            GenHistoryItem(
                id=item.id,
                filename=item.filename,
                filenames=json.loads(item.filenames) if item.filenames else [item.filename],
                project_id=item.project_id,
                project_name=item.project.name if item.project else "",
                project_description=item.project_description or "",
                status=item.status,
                error_message=item.error_message or "",
                functional_points_count=item.functional_points_count or 0,
                test_cases_count=item.test_cases_count or 0,
                imported_count=item.imported_count or 0,
                created_at=item.created_at,
                completed_at=item.completed_at,
            )
            for item in items
        ],
        total=total,
    )


@router.get("/history/{session_id}/export-xlsx")
async def export_gen_test_cases_xlsx(
    session_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Response:
    """Export generated test cases as xlsx file."""

    loop = asyncio.get_running_loop()
    record = await loop.run_in_executor(None, lambda: crud.gen.get_gen_session(db, session_id))
    if not record:
        raise HTTPException(404, "记录不存在")
    if record.status != "completed":
        raise HTTPException(400, f"分析未完成，状态: {record.status}")

    db_tcs = await loop.run_in_executor(None, lambda: crud.gen.list_gen_test_cases(db, session_id))

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"

    # Header style
    header_font = Font(name="微软雅黑", bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    headers = ["用例ID", "所属模块", "标题", "前置条件", "测试步骤", "预期结果", "优先级"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    cell_align = Alignment(vertical="top", wrap_text=True)
    for row_idx, tc in enumerate(db_tcs, 2):
        values = [
            tc.test_case_id,
            tc.module,
            tc.title,
            tc.preconditions or "",
            tc.test_steps or "",
            tc.expected_result or "",
            tc.priority,
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = cell_align
            cell.border = thin_border

    # Column widths
    widths = [14, 16, 30, 24, 40, 40, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w

    output = wb.active  # use tempfile
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"测试用例_{session_id[:8]}.xlsx"
    ascii_name = f"testcases_{session_id[:8]}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'},
    )


@router.get("/history/{session_id}", response_model=GenPreviewResponse)
async def get_history_detail(
    session_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> GenPreviewResponse:
    """Get analysis detail from DB."""

    loop = asyncio.get_running_loop()
    record = await loop.run_in_executor(None, lambda: crud.gen.get_gen_session(db, session_id))
    if not record:
        raise HTTPException(404, "记录不存在")
    if record.status != "completed":
        raise HTTPException(400, f"分析未完成，状态: {record.status}")

    db_fps = await loop.run_in_executor(None, lambda: crud.gen.list_gen_functional_points(db, session_id))
    db_tcs = await loop.run_in_executor(None, lambda: crud.gen.list_gen_test_cases(db, session_id))

    fps = [
        {"id": fp.fp_id, "module": fp.module, "name": fp.name, "category": fp.category, "description": fp.description}
        for fp in db_fps
    ]
    tcs = [
        GenPreviewItem(
            test_case_id=tc.test_case_id,
            module=tc.module,
            title=tc.title,
            preconditions=tc.preconditions or "",
            test_steps=tc.test_steps or "",
            expected_result=tc.expected_result or "",
            priority=tc.priority or "中",
        )
        for tc in db_tcs
    ]
    return GenPreviewResponse(
        session_id=session_id,
        functional_points=fps,
        test_cases=tcs,
    )


@router.delete("/history/{session_id}")
async def delete_history(
    session_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> dict:
    """Delete analysis history record."""

    loop = asyncio.get_running_loop()
    record = await loop.run_in_executor(None, lambda: crud.gen.get_gen_session(db, session_id))
    if not record:
        raise HTTPException(404, "记录不存在")

    # Also remove from in-memory if present
    async with _lock:
        _sessions.pop(session_id, None)

    await loop.run_in_executor(None, lambda: crud.gen.delete_gen_session(db, session_id))
    return {"message": "删除成功"}


@router.put("/history/{session_id}/test-cases/{test_case_id}")
async def update_gen_test_case(
    session_id: str,
    test_case_id: str,
    body: GenTestCaseUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> dict:
    """Update a test case in the analysis session."""

    loop = asyncio.get_running_loop()
    record = await loop.run_in_executor(None, lambda: crud.gen.get_gen_session(db, session_id))
    if not record:
        raise HTTPException(404, "记录不存在")

    tc = await loop.run_in_executor(None, lambda: crud.gen.get_gen_test_case(db, session_id, test_case_id))
    if not tc:
        raise HTTPException(404, "用例不存在")

    await loop.run_in_executor(None, lambda: crud.gen.update_gen_test_case(db, session_id, test_case_id, body))
    return {"message": "更新成功"}


@router.delete("/history/{session_id}/test-cases/{test_case_id}")
async def delete_gen_test_case(
    session_id: str,
    test_case_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> dict:
    """Delete a test case from the analysis session."""

    loop = asyncio.get_running_loop()
    record = await loop.run_in_executor(None, lambda: crud.gen.get_gen_session(db, session_id))
    if not record:
        raise HTTPException(404, "记录不存在")

    tc = await loop.run_in_executor(None, lambda: crud.gen.get_gen_test_case(db, session_id, test_case_id))
    if not tc:
        raise HTTPException(404, "用例不存在")

    await loop.run_in_executor(None, lambda: crud.gen.delete_gen_test_case(db, session_id, test_case_id))
    return {"message": "删除成功"}
