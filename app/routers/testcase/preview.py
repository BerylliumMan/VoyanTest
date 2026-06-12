from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db

router = APIRouter()


class PreviewPlanRequest(BaseModel):
    case_id: int = 0
    description: Optional[str] = None


class PreviewPlanItem(BaseModel):
    step_order: int
    step_description: str
    planned_action: str
    target_elements: list = []


class PreviewPlanResponse(BaseModel):
    case_id: int
    case_name: str
    plan: List[PreviewPlanItem]
    total_estimated_actions: int
    warning: Optional[str] = None


@router.post("/preview-plan", response_model=PreviewPlanResponse)
def preview_plan(req: PreviewPlanRequest, db: Session = Depends(get_db)):
    """
    预览 AI Agent 对测试用例的执行计划。
    返回每步的描述和预计操作。
    """
    if not req.case_id or req.case_id == 0:
        return PreviewPlanResponse(
            case_id=0,
            case_name="",
            plan=[],
            total_estimated_actions=0,
            warning="请先保存测试用例后再预览计划",
        )

    db_case = crud.get_test_case(db, req.case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    steps = crud.get_steps_for_case(db, req.case_id)
    plan_items = []
    for s in steps:
        plan_items.append(PreviewPlanItem(
            step_order=s.step_order,
            step_description=s.description,
            planned_action=s.description,
            target_elements=[],
        ))

    return PreviewPlanResponse(
        case_id=req.case_id,
        case_name=db_case.name,
        plan=plan_items,
        total_estimated_actions=len(plan_items),
        warning=None,
    )
