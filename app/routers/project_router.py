# app/routers/project_router.py
import logging
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from typing import List, Optional
from sqlalchemy.orm import Session
from .. import crud, models
from app.auth import require_admin
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects",
    tags=["项目"],
)

@router.post("/", response_model=models.Project)
def create_project(project: models.ProjectCreate, admin=Depends(require_admin), db: Session = Depends(get_db)):
    """
    创建新项目。
    """
    try:
        return crud.create_project(db, project)
    except Exception as e:
        logger.error("创建项目失败: %s", e)
        raise HTTPException(status_code=400, detail="Could not create project")

@router.get("/", response_model=List[models.Project])
def get_all_projects(db: Session = Depends(get_db)):
    """
    检索所有项目。
    """
    return crud.get_all_projects(db)

@router.get("/{project_id}", response_model=models.Project)
def get_project(project_id: int, db: Session = Depends(get_db)):
    """
    通过其ID检索单个项目。
    """
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return db_project

@router.put("/{project_id}", response_model=models.Project)
def update_project(project_id: int, project: models.ProjectUpdate, admin=Depends(require_admin), db: Session = Depends(get_db)):
    """
    更新项目。
    """
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return crud.update_project(db, project_id, project)
    except Exception as e:
        logger.error("更新项目失败 (id=%d): %s", project_id, e)
        raise HTTPException(status_code=500, detail="Error updating project")

@router.delete("/{project_id}")
def delete_project(project_id: int, admin=Depends(require_admin), db: Session = Depends(get_db)):
    """
    删除项目及其所有关联的测试用例和步骤。
    """
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    
    try:
        result = crud.delete_project(db, project_id)
        return result
    except Exception as e:
        logger.error("删除项目失败 (id=%d): %s", project_id, e)
        raise HTTPException(status_code=500, detail="Error deleting project")

@router.post("/{project_id}/run")
async def run_project_test_cases(
    project_id: int,
    background_tasks: BackgroundTasks,
    environment_id: Optional[int] = None,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Trigger sequential batch run of all test cases in a project.
    Uses a single browser instance; cases execute one at a time.
    """
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    test_cases = crud.get_all_test_cases_for_project(db, project_id)
    if not test_cases:
        return {"detail": "此项目中没有要运行的测试用例。"}

    case_ids = [c.id for c in test_cases]
    from core.runner import run_batch_test_cases

    background_tasks.add_task(run_batch_test_cases, case_ids, project_id, environment_id=environment_id)

    return {"detail": f"已为项目 {project_id} 中的 {len(test_cases)} 个测试用例触发顺序运行。"}



@router.get("/{project_id}/testcases", response_model=List[models.TestCase])
def get_project_test_cases(project_id: int, db: Session = Depends(get_db)):
    """
    检索特定项目的所有测试用例。
    """
    db_project = crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    
    return crud.get_all_test_cases_for_project(db, project_id)
