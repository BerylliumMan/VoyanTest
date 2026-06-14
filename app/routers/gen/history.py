"""``/api/gen/history`` family of endpoints — they all read from (or mutate)
the persistent ``GenSession`` table.  In-memory cleanup of the matching
session is also done here for parity with the original behavior.
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ... import db_models
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
    query = db.query(db_models.GenSession).order_by(db_models.GenSession.created_at.desc())
    if project_id is not None:
        query = query.filter(db_models.GenSession.project_id == project_id)
    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()

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
    record = db.query(db_models.GenSession).filter(db_models.GenSession.id == session_id).first()
    if not record:
        raise HTTPException(404, "记录不存在")
    if record.status != "completed":
        raise HTTPException(400, f"分析未完成，状态: {record.status}")

    db_tcs = db.query(db_models.GenTestCase).filter(
        db_models.GenTestCase.session_id == session_id
    ).order_by(db_models.GenTestCase.id).all()

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
    record = db.query(db_models.GenSession).filter(db_models.GenSession.id == session_id).first()
    if not record:
        raise HTTPException(404, "记录不存在")
    if record.status != "completed":
        raise HTTPException(400, f"分析未完成，状态: {record.status}")

    db_fps = db.query(db_models.GenFunctionalPoint).filter(
        db_models.GenFunctionalPoint.session_id == session_id
    ).order_by(db_models.GenFunctionalPoint.fp_id).all()

    db_tcs = db.query(db_models.GenTestCase).filter(
        db_models.GenTestCase.session_id == session_id
    ).order_by(db_models.GenTestCase.id).all()

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
    record = db.query(db_models.GenSession).filter(db_models.GenSession.id == session_id).first()
    if not record:
        raise HTTPException(404, "记录不存在")

    # Also remove from in-memory if present
    async with _lock:
        _sessions.pop(session_id, None)

    db.delete(record)
    db.commit()
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
    record = db.query(db_models.GenSession).filter(db_models.GenSession.id == session_id).first()
    if not record:
        raise HTTPException(404, "记录不存在")

    tc = db.query(db_models.GenTestCase).filter(
        db_models.GenTestCase.session_id == session_id,
        db_models.GenTestCase.test_case_id == test_case_id,
    ).first()
    if not tc:
        raise HTTPException(404, "用例不存在")

    if body.module is not None:
        tc.module = body.module
    if body.title is not None:
        tc.title = body.title
    if body.preconditions is not None:
        tc.preconditions = body.preconditions
    if body.test_steps is not None:
        tc.test_steps = body.test_steps
    if body.expected_result is not None:
        tc.expected_result = body.expected_result
    if body.priority is not None:
        tc.priority = body.priority

    db.commit()
    return {"message": "更新成功"}


@router.delete("/history/{session_id}/test-cases/{test_case_id}")
async def delete_gen_test_case(
    session_id: str,
    test_case_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> dict:
    """Delete a test case from the analysis session."""
    record = db.query(db_models.GenSession).filter(db_models.GenSession.id == session_id).first()
    if not record:
        raise HTTPException(404, "记录不存在")

    tc = db.query(db_models.GenTestCase).filter(
        db_models.GenTestCase.session_id == session_id,
        db_models.GenTestCase.test_case_id == test_case_id,
    ).first()
    if not tc:
        raise HTTPException(404, "用例不存在")

    db.delete(tc)
    record.test_cases_count = max(0, (record.test_cases_count or 1) - 1)
    db.commit()
    return {"message": "删除成功"}
