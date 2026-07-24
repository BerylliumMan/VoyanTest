# app/routers/agent_run_router.py — AgentRun REST API
"""Agent 运行记录管理 API — AgentRun 的 CRUD 接口。"""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.database import get_async_db
from app.auth import require_admin, get_current_user
from app.models.schemas import (
    AgentRunCreate,
    AgentRunResponse,
    AgentMessageResponse,
)
from app.crud import agent_run

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/agent-runs",
    tags=["agent-runs"],
)


@router.get("")
async def list_runs(
    status: str | None = Query(None, description="按状态过滤"),
    agent_definition_id: int | None = Query(None, description="按 Agent 定义 ID 过滤"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """GET /api/agent-runs?status=&agent_definition_id=&page=&size="""
    items, total = await agent_run.list_agent_runs(
        db, status=status, agent_definition_id=agent_definition_id,
        page=page, size=size,
    )
    return {
        "items": [
            AgentRunResponse.model_validate(r).model_copy(update={
                "agent_definition_name": getattr(r, "_agent_name", "")
            })
            for r in items
        ],
        "total": total, "page": page, "size": size,
    }


@router.get("/{run_id}")
async def get_run(
    run_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """获取单个 Agent 运行详情"""
    obj = await agent_run.get_agent_run(db, run_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")
    return AgentRunResponse.model_validate(obj).model_copy(update={
        "agent_definition_name": getattr(obj, "_agent_name", "")
    })


@router.get("/{run_id}/messages", response_model=List[AgentMessageResponse])
async def get_run_messages(
    run_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """获取 Agent 运行的所有消息"""
    run = await agent_run.get_agent_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")
    return await agent_run.get_agent_run_messages(db, run_id)


@router.post("", response_model=AgentRunResponse, status_code=201)
async def create_run(
    data: AgentRunCreate,
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_async_db),
):
    """POST /api/agent-runs - 创建新的 Agent 运行"""
    return await agent_run.create_agent_run(db, data)
