# app/routers/project_router.py
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from .. import crud, models, db_models
from app.auth import require_admin, get_current_user, get_user_project_filter
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects",
    tags=["项目"],
)

@router.post("/", response_model=models.Project)
async def create_project(project: models.ProjectCreate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.Project:
    """
    创建新项目。
    """
    try:
        return await crud.create_project(db, project)
    except HTTPException:
        raise
    except Exception:
        logger.exception("创建项目失败")
        raise HTTPException(status_code=400, detail="Could not create project")

@router.get("/", response_model=List[models.Project])
async def get_all_projects(user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> list[models.Project]:
    """
    检索所有项目（非管理员仅返回授权项目）。
    """
    allowed_ids = get_user_project_filter(user)
    return await crud.list_projects_for_user(db, allowed_ids)

@router.get("/{project_id}", response_model=models.Project)
async def get_project(project_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.Project:
    """
    通过其ID检索单个项目。
    """
    # 验证项目访问权限
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return db_project

@router.put("/{project_id}", response_model=models.Project)
async def update_project(project_id: int, project: models.ProjectUpdate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.Project:
    """
    更新项目。
    """
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return await crud.update_project(db, project_id, project)
    except HTTPException:
        raise
    except Exception:
        logger.exception("更新项目失败 (id=%d)", project_id)
        raise HTTPException(status_code=500, detail="Error updating project")


@router.delete("/{project_id}")
async def delete_project(project_id: int, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> dict[str, str] | None:
    """
    删除项目及其所有关联的测试用例和步骤。
    """
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    
    try:
        result = await crud.delete_project(db, project_id)
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("删除项目失败 (id=%d)", project_id)
        raise HTTPException(status_code=500, detail="Error deleting project")

@router.post("/{project_id}/run")
async def run_project_test_cases(
    project_id: int,
    background_tasks: BackgroundTasks,
    environment_id: Optional[int] = None,
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """
    Trigger sequential batch run of all test cases in a project.
    Uses a single browser instance; cases execute one at a time.
    """
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    test_cases = await crud.get_all_test_cases_for_project(db, project_id)
    if not test_cases:
        return {"detail": "此项目中没有要运行的测试用例。"}

    case_ids = [c.id for c in test_cases]
    from core.runner import run_batch_test_cases

    background_tasks.add_task(run_batch_test_cases, case_ids, project_id, environment_id=environment_id, triggered_by=getattr(admin, 'username', None))

    return {"detail": f"已为项目 {project_id} 中的 {len(test_cases)} 个测试用例触发顺序运行。"}



@router.get("/{project_id}/testcases", response_model=List[models.TestCase])
async def get_project_test_cases(project_id: int, db: AsyncSession = Depends(get_async_db)) -> list[models.TestCase]:
    """
    检索特定项目的所有测试用例。
    """
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    
    return await crud.get_all_test_cases_for_project(db, project_id)
