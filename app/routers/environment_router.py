import logging
from fastapi import APIRouter, HTTPException, Depends
from typing import List
from sqlalchemy.orm import Session
from .. import crud, models
from app.auth import require_admin
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["环境"],
)


@router.get("/projects/{project_id}/environments", response_model=List[models.Environment])
def list_environments(project_id: int, db: Session = Depends(get_db)):
    """获取项目下的所有环境，若无则自动迁移"""
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    crud.ensure_default_environment(db, project_id)

    return crud.get_environments(db, project_id)


@router.post("/projects/{project_id}/environments", response_model=models.Environment)
def create_environment(
    project_id: int,
    env: models.EnvironmentCreate,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """为项目创建新环境"""
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        return crud.create_environment(db, project_id, env)
    except Exception as e:
        logger.error("创建环境失败 (project_id=%d): %s", project_id, e)
        raise HTTPException(status_code=400, detail="Could not create environment")


@router.put("/environments/{env_id}", response_model=models.Environment)
def update_environment(
    env_id: int,
    env: models.EnvironmentUpdate,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """更新环境"""
    db_env = crud.get_environment(db, env_id)
    if db_env is None:
        raise HTTPException(status_code=404, detail="Environment not found")

    try:
        result = crud.update_environment(db, env_id, env)
        if result is None:
            raise HTTPException(status_code=404, detail="Environment not found")
        return result
    except Exception as e:
        logger.error("更新环境失败 (env_id=%d): %s", env_id, e)
        raise HTTPException(status_code=400, detail="Could not update environment")


@router.delete("/environments/{env_id}")
def delete_environment(
    env_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """删除环境"""
    db_env = crud.get_environment(db, env_id)
    if db_env is None:
        raise HTTPException(status_code=404, detail="Environment not found")

    try:
        result = crud.delete_environment(db, env_id)
        return result
    except Exception as e:
        logger.error("删除环境失败 (env_id=%d): %s", env_id, e)
        raise HTTPException(status_code=400, detail="Could not delete environment")


@router.put("/environments/{env_id}/default", response_model=models.Environment)
def set_default_environment(
    env_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """设为默认环境"""
    db_env = crud.get_environment(db, env_id)
    if db_env is None:
        raise HTTPException(status_code=404, detail="Environment not found")

    try:
        result = crud.set_default_environment(db, env_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Environment not found")
        return result
    except Exception as e:
        logger.error("设置默认环境失败 (env_id=%d): %s", env_id, e)
        raise HTTPException(status_code=400, detail="Could not set default environment")
