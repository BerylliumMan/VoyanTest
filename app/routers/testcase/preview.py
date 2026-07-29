from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.database import get_async_db

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


class StepIntentPreviewRequest(BaseModel):
    """Observe-only Intent bind (Stagehand-style) against a provided AX snapshot."""

    description: str = Field(..., min_length=1)
    snapshot: str = Field(..., min_length=1, description="Accessibility snapshot text")
    expected_result: Optional[str] = None


@router.post("/preview-plan", response_model=PreviewPlanResponse)
async def preview_plan(req: PreviewPlanRequest, db: AsyncSession = Depends(get_async_db)) -> PreviewPlanResponse:
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

    db_case = await crud.get_test_case(db, req.case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    steps = await crud.get_steps_for_case(db, req.case_id)
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


@router.post("/preview-step-intent")
async def preview_step_intent(req: StepIntentPreviewRequest) -> dict[str, Any]:
    """Dry-run: Intent + snapshot match without executing (no browser).

    Useful for debugging understanding drift — returns candidates and whether vision would be needed.
    """
    from core.step_intent import preview_step_resolution

    prev = await preview_step_resolution(
        req.description,
        req.snapshot,
        expected_result=req.expected_result,
    )
    return prev.model_dump()
