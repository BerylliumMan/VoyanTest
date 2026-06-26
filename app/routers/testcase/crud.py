from __future__ import annotations

import io
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, db_models
from app.auth import require_admin, require_project_access, get_user_project_filter, get_current_user
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/", response_model=models.TestCase)
async def create_test_case(case: models.TestCaseCreate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """
    创建带有步骤的新测试用例。
    """
    db_project = await crud.get_project(db, case.project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail=f"Project with id {case.project_id} not found")

    try:
        return await crud.create_test_case(db, case)
    except Exception as e:
        logger.exception("创建测试用例失败")
        raise HTTPException(status_code=400, detail="创建测试用例失败，请检查输入数据")


@router.get("/search", response_model=models.TestCasePage)
async def search_test_cases(project_id: int, q: str = "", page: int = 1, size: int = 20, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """
    搜索测试用例（按名称和描述）。
    """
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    result = await crud.search_test_cases(db, project_id, q, page, size)
    return {
        "items": result["items"],
        "total_items": result["total_items"],
        "page": page,
        "size": size,
    }


@router.get("/init-cases", response_model=List[models.TestCase])
async def list_init_test_cases(project_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> list[models.TestCase]:
    """获取项目下所有标记为初始化的测试用例"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    return await crud.get_init_test_cases(db, project_id)


@router.get("/{case_id}", response_model=models.TestCase)
async def get_test_case(case_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """
    通过其ID检索单个测试用例，包括其步骤。
    """
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    # 验证项目访问权限
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Test case not found")
    return db_case


@router.delete("/{case_id}")
async def delete_test_case(case_id: int, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> dict[str, str] | None:
    """
    删除测试用例及其步骤。
    """
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    try:
        return await crud.delete_test_case(db, case_id)
    except Exception as e:
        logger.exception("删除测试用例失败 case_id=%s", case_id)
        raise HTTPException(status_code=500, detail="删除测试用例时发生内部错误")


@router.put("/{case_id}", response_model=models.TestCase)
async def update_test_case(case_id: int, case: models.TestCaseUpdate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """
    更新测试用例，包括其步骤。
    """
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    try:
        return await crud.update_test_case(db, case_id, case)
    except Exception as e:
        logger.exception("更新测试用例失败 case_id=%s", case_id)
        raise HTTPException(status_code=500, detail="更新测试用例时发生内部错误")


@router.get("/module/{module_id}/testcases", response_model=models.TestCasePage)
async def get_module_test_cases(module_id: int, page: int = 1, size: int = 20, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """
    检索特定模块的所有测试用例，并分页。
    """
    db_module = await crud.get_module(db, module_id)
    if db_module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    # 验证项目访问权限
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_module.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Module not found")

    paginated_data = await crud.get_all_test_cases_for_module_paginated(db, module_id, page, size)
    return {
        "items": paginated_data["items"],
        "total_items": paginated_data["total_items"],
        "page": page,
        "size": size,
    }


@router.put("/{case_id}/toggle-init", response_model=models.TestCase)
async def toggle_test_case_init(case_id: int, body: Dict[str, Any], admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """切换测试用例的初始化标记"""
    is_init = body.get("is_init", False)
    db_case = await crud.update_test_case_is_init(db, case_id, is_init)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    return db_case


@router.get("/project/{project_id}/testcases", response_model=models.TestCasePage)
async def get_project_test_cases(project_id: int, page: int = 1, size: int = 20, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """
    检索特定项目的所有测试用例，并分页。
    """
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    paginated_data = await crud.get_all_test_cases_for_project_paginated(db, project_id, page, size)
    return {
        "items": paginated_data["items"],
        "total_items": paginated_data["total_items"],
        "page": page,
        "size": size,
    }


@router.get("/export")
async def export_test_cases(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """导出项目测试用例为 xlsx。"""
    from openpyxl import Workbook

    cases = await crud.get_all_test_cases_for_project(db, project_id)

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"
    ws.append(["用例名称", "模块", "步骤序号", "步骤描述", "预期结果", "优先级", "标签"])

    for tc in cases:
        mn = (await crud.get_module(db, tc.module_id)).name if tc.module_id else ""
        steps = await crud.get_steps_for_case(db, tc.id)
        if steps:
            for s in steps:
                ws.append([tc.name, mn, s.step_order, s.description, s.parsed_result, tc.priority or "medium", tc.tags or ""])
        else:
            ws.append([tc.name, mn, "", "", "", tc.priority or "medium", tc.tags or ""])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=testcases_project_{project_id}.xlsx"},
    )


@router.post("/import")
async def import_test_cases(
    project_id: int,
    file: UploadFile = File(...),
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """从 xlsx 文件导入测试用例。"""
    from openpyxl import load_workbook

    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx 文件")

    contents = await file.read()
    wb = load_workbook(io.BytesIO(contents))
    ws = wb.active
    if ws is None:
        raise HTTPException(status_code=400, detail="工作表为空")

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    created = 0
    errors = []

    for row in rows:
        name, _module, _so, desc, expected, priority, tags = [str(c or "") for c in row]
        if not name:
            continue
        try:
            steps_payload = []
            if desc:
                steps_payload.append(models.TestStepCreatePayload(
                    step_order=1, description=desc, expected_result=expected,
                ))
            await crud.create_test_case(db, models.TestCaseCreate(
                project_id=project_id, name=name,
                steps=steps_payload, priority=priority or "medium", tags=tags or "",
            ))
            created += 1
        except Exception as e:
            errors.append(f"创建用例「{name}」失败: {e}")

    return {"created": created, "errors": errors}
