from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Depends
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from .. import crud, models
from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["环境"],
)


@router.get("/projects/{project_id}/environments", response_model=List[models.Environment])
async def list_environments(
    project_id: int, db: AsyncSession = Depends(get_async_db)
) -> list[models.Environment]:
    """获取项目下的所有环境，若无则自动迁移"""
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    await crud.ensure_default_environment(db, project_id)

    return await crud.get_environments(db, project_id)


@router.post("/projects/{project_id}/environments", response_model=models.Environment)
async def create_environment(
    project_id: int,
    env: models.EnvironmentCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> models.Environment:
    """为项目创建新环境"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        return await crud.create_environment(db, project_id, env)
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError, Exception):
        logger.exception("创建环境失败 (project_id=%d)", project_id)
        raise HTTPException(status_code=400, detail="Could not create environment")


@router.put("/environments/{env_id}", response_model=models.Environment)
async def update_environment(
    env_id: int,
    env: models.EnvironmentUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> models.Environment:
    """更新环境"""
    db_env = await crud.get_environment(db, env_id)
    if db_env is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_env.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    try:
        result = await crud.update_environment(db, env_id, env)
        if result is None:
            raise HTTPException(status_code=400, detail="Environment not found")
        return result
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError, Exception):
        logger.exception("更新环境失败 (env_id=%d)", env_id)
        raise HTTPException(status_code=400, detail="Could not update environment")


@router.delete("/environments/{env_id}")
async def delete_environment(
    env_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict[str, str] | None:
    """删除环境"""
    db_env = await crud.get_environment(db, env_id)
    if db_env is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_env.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    try:
        result = await crud.delete_environment(db, env_id)
        return result
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError, Exception):
        logger.exception("删除环境失败 (env_id=%d)", env_id)
        raise HTTPException(status_code=400, detail="Could not delete environment")


@router.put("/environments/{env_id}/default", response_model=models.Environment)
async def set_default_environment(
    env_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> models.Environment:
    """设为默认环境"""
    db_env = await crud.get_environment(db, env_id)
    if db_env is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_env.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    try:
        result = await crud.set_default_environment(db, env_id)
        if result is None:
            raise HTTPException(status_code=400, detail="Environment not found")
        return result
    except HTTPException:
        raise
    except (ValueError, SQLAlchemyError, Exception):
        logger.exception("设置默认环境失败 (env_id=%d)", env_id)
        raise HTTPException(status_code=400, detail="Could not set default environment")
