from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Depends
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
