from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app import models
from app.auth import require_admin
from app.crud.testcase import get_next_project_case_number
from app.database import get_async_db

router = APIRouter()


class BatchMoveRequest(BaseModel):
    case_ids: List[int]
    project_id: int
    module_id: Optional[int] = None


class BatchCopyRequest(BaseModel):
    case_ids: List[int]
    project_id: int
    module_id: Optional[int] = None


async def _validate_target_project(db: AsyncSession, project_id: int, module_id: Optional[int] = None) -> None:
    """验证目标项目和模块是否存在，否则抛出 404/400。"""
    project = await crud.get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"目标项目 {project_id} 不存在")
    if module_id is not None:
        module = await crud.get_module(db, module_id)
        if not module:
            raise HTTPException(status_code=404, detail=f"目标模块 {module_id} 不存在")
        if module.project_id != project_id:
            raise HTTPException(status_code=400, detail="目标模块不属于目标项目")


@router.post("/batch-move")
async def batch_move_cases(req: BatchMoveRequest, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> dict:
    """批量移动测试用例到指定项目/模块"""
    await _validate_target_project(db, req.project_id, req.module_id)
    moved = 0
    for case_id in req.case_ids:
        case = await crud.get_test_case(db, case_id)
        if not case:
            continue
        is_cross_project = case.project_id != req.project_id
        case.project_id = req.project_id
        case.module_id = req.module_id
        if is_cross_project:
            case.project_case_number = await get_next_project_case_number(db, req.project_id)
        moved += 1
    await db.commit()
    return {"message": f"已移动 {moved} 个用例", "moved": moved}


@router.post("/batch-copy")
async def batch_copy_cases(req: BatchCopyRequest, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> dict:
    """批量复制测试用例到指定项目/模块"""
    await _validate_target_project(db, req.project_id, req.module_id)
    copied = 0
    for case_id in req.case_ids:
        original = await crud.get_test_case(db, case_id)
        if not original:
            continue
        steps_data = [
            models.TestStepCreatePayload(
                step_order=s.step_order,
                description=s.description,
            )
            for s in original.steps
        ]
        new_case = models.TestCaseCreate(
            project_id=req.project_id,
            module_id=req.module_id,
            name=original.name + " (副本)",
            description=original.description,
            steps=steps_data,
        )
        await crud.create_test_case(db, new_case)
        copied += 1
    return {"message": f"已复制 {copied} 个用例", "copied": copied}
