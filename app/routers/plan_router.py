# 050 P1：测试计划、轮次与我的待办
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_async_db
from app.services import my_tasks as my_tasks_service
from app.services import test_cycle as cycle_service
from app.services import test_plan as plan_service

router = APIRouter(prefix="/api")


class CreatePlanRequest(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    requirement_id: int | None = None
    owner_id: int | None = None
    start_date: date | None = None
    due_date: date | None = None


class UpdatePlanRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    requirement_id: int | None = None
    owner_id: int | None = None
    status: str | None = None
    start_date: date | None = None
    due_date: date | None = None


class AddItemsRequest(BaseModel):
    case_ids: list[int] = Field(min_length=1)


class UpdateItemRequest(BaseModel):
    assignee_id: int | None = None
    due_date: date | None = None
    priority: str | None = None
    status: str | None = None
    note: str | None = None
    sort_order: int | None = None


class CreateCycleRequest(BaseModel):
    name: str | None = None
    environment_id: int | None = None
    agent_id: int | None = None
    case_ids: list[int] | None = None


# ----------------------------
# 计划
# ----------------------------
@router.get("/plans")
async def list_plans(
    project_id: int = Query(...),
    status: str | None = None,
    include_archived: bool = False,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    items = await plan_service.list_plans(
        db, user, project_id, status=status, include_archived=include_archived
    )
    return {"items": items}


@router.get("/plans/members")
async def list_members(
    project_id: int = Query(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return {"items": await plan_service.list_project_members(db, user, project_id)}


@router.post("/plans", status_code=201)
async def create_plan(
    payload: CreatePlanRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.create_plan(db, user, payload.model_dump())


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.get_plan_detail(db, plan_id)


@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: int,
    payload: UpdatePlanRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.update_plan(
        db, user, plan_id, payload.model_dump(exclude_unset=True)
    )


@router.delete("/plans/{plan_id}")
async def delete_plan(
    plan_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.delete_plan(db, user, plan_id)


# ----------------------------
# 计划项
# ----------------------------
@router.get("/plans/{plan_id}/items")
async def list_items(
    plan_id: int,
    status: str | None = None,
    assignee_id: int | None = None,
    module_id: int | None = None,
    keyword: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    items = await plan_service.list_items(
        db, user, plan_id, status=status, assignee_id=assignee_id,
        module_id=module_id, keyword=keyword,
    )
    return {"items": items}


@router.post("/plans/{plan_id}/items")
async def add_items(
    plan_id: int,
    payload: AddItemsRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.add_items(db, user, plan_id, payload.case_ids)


@router.patch("/plans/{plan_id}/items/{item_id}")
async def update_item(
    plan_id: int,
    item_id: int,
    payload: UpdateItemRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.update_item(
        db, user, plan_id, item_id, payload.model_dump(exclude_unset=True)
    )


@router.delete("/plans/{plan_id}/items/{item_id}")
async def delete_item(
    plan_id: int,
    item_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await plan_service.delete_item(db, user, plan_id, item_id)


# ----------------------------
# 轮次
# ----------------------------
@router.get("/plans/{plan_id}/cycles")
async def list_cycles(
    plan_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    items = await cycle_service.list_cycles(db, user, plan_id)
    return {"items": items}


@router.post("/plans/{plan_id}/cycles", status_code=201)
async def create_cycle(
    plan_id: int,
    payload: CreateCycleRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await cycle_service.create_cycle(db, user, plan_id, payload.model_dump())


@router.get("/cycles/{cycle_id}")
async def get_cycle(
    cycle_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await cycle_service.get_cycle_detail(db, user, cycle_id)


@router.post("/cycles/{cycle_id}/start")
async def start_cycle(
    cycle_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await cycle_service.start_cycle(db, user, cycle_id)


@router.post("/cycles/{cycle_id}/cancel")
async def cancel_cycle(
    cycle_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await cycle_service.cancel_cycle(db, user, cycle_id)


@router.post("/cycles/{cycle_id}/retry-failed")
async def retry_failed(
    cycle_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await cycle_service.retry_failed(db, user, cycle_id)


# ----------------------------
# 我的待办
# ----------------------------
@router.get("/my/tasks")
async def my_tasks(
    filter: str = "todo",
    project_id: int | None = None,
    page: int = 1,
    size: int = 50,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    return await my_tasks_service.list_my_tasks(
        db, user, filter=filter, project_id=project_id, page=page, size=size
    )
