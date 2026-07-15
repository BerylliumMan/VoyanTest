"""
模块管理 API 路由
提供模块的增删改查、树形结构、层级管理等功能
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from ..database import get_async_db
from ..auth import require_admin, get_current_user, get_user_project_filter
from .. import crud, models

router = APIRouter(
    prefix="/api",
    tags=["模块管理"]
)


@router.get("/projects/{project_id}/modules", response_model=List[models.Module])
async def list_modules(project_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> list[models.Module]:
    """获取项目的所有模块（扁平列表）"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    project = await crud.get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    modules = await crud.get_modules_for_project(db, project_id)
    return modules


@router.get("/projects/{project_id}/modules/tree")
async def get_module_tree(project_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> list:
    """获取项目的模块树形结构"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    tree = await crud.get_module_tree(db, project_id)
    return tree


@router.get("/modules/{module_id}", response_model=models.Module)
async def get_module(module_id: int, db: AsyncSession = Depends(get_async_db)) -> models.Module:
    """获取单个模块详情"""
    db_module = await crud.get_module(db, module_id)
    if not db_module:
        raise HTTPException(status_code=404, detail="Module not found")
    return db_module


@router.get("/modules/{module_id}/descendants")
async def get_module_descendants(module_id: int, db: AsyncSession = Depends(get_async_db)) -> dict:
    """获取模块及所有下级模块 ID 列表（递归）"""
    module = await crud.get_module(db, module_id)
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    module_ids = await crud.get_module_descendants(db, module_id)
    return {"module_ids": module_ids}


@router.post("/projects/{project_id}/modules", response_model=models.Module)
async def create_module(
    project_id: int,
    module: models.ModuleCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> models.Module:
    """创建模块"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    # 验证项目存在
    project = await crud.get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 验证 body 中 project_id 与 URL 一致
    if module.project_id is not None and module.project_id != project_id:
        raise HTTPException(status_code=400, detail="URL 中的 project_id 与请求体中的不一致")

    # 验证 parent_id 不形成循环引用
    if module.parent_id is not None:
        parent = await crud.get_module(db, module.parent_id)
        if not parent:
            raise HTTPException(status_code=400, detail="父模块不存在")
        if parent.project_id != project_id:
            raise HTTPException(status_code=400, detail="父模块不属于该项目")

    db_module = await crud.create_module(db, project_id, module)
    return db_module


@router.put("/modules/{module_id}", response_model=models.Module)
async def update_module(
    module_id: int,
    module: models.ModuleUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> models.Module:
    """更新模块"""
    db_module = await crud.get_module(db, module_id)
    if not db_module:
        raise HTTPException(status_code=404, detail="Module not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_module.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    # 验证 parent_id 不形成循环引用
    if module.parent_id is not None:
        if not await crud.validate_module_parent(db, module_id, module.parent_id):
            raise HTTPException(status_code=400, detail="不能形成循环引用")

    db_module = await crud.update_module(db, module_id, module)
    if not db_module:
        raise HTTPException(status_code=404, detail="Module not found")
    return db_module


@router.delete("/modules/{module_id}", status_code=204)
async def delete_module(module_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> Response:
    """删除模块（含删除保护）"""
    db_module = await crud.get_module(db, module_id)
    if not db_module:
        raise Response(status_code=204)
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_module.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    try:
        result = await crud.delete_module(db, module_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="模块删除失败：存在冲突或约束限制")
    if result is None:
        raise HTTPException(status_code=404, detail="Module not found")
    return Response(status_code=204)
