# app/routers/agent_definition_router.py — AgentDefinition REST API
"""Agent 定义管理 API — 服务端 AI Agent 定义的 CRUD 接口。"""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.database import get_async_db
from app.auth import require_admin
from app.models.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
    AgentDefinitionResponse,
)
from app.crud import agent_definition

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["Agent定义"],
)


@router.get(
    "/agent-definitions",
    response_model=List[AgentDefinitionResponse],
)
async def list_agent_definitions(
    type: str | None = Query(
        None,
        alias="type",
        description="按 agent_type 过滤: generation / execution / recording",
    ),
    db: AsyncSession = Depends(get_async_db),
    admin=Depends(require_admin),
) -> list:
    """获取 Agent 定义列表，可选按 agent_type 过滤"""
    return await agent_definition.list_agent_definitions(db, agent_type=type)


@router.post(
    "/agent-definitions",
    response_model=AgentDefinitionResponse,
    status_code=201,
)
async def create_agent_definition(
    body: AgentDefinitionCreate,
    db: AsyncSession = Depends(get_async_db),
    admin=Depends(require_admin),
):
    """创建 Agent 定义"""
    try:
        obj = await agent_definition.create_agent_definition(db, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from app.agent_resolver import invalidate_agent_cache
    invalidate_agent_cache(body.agent_type)
    return obj


@router.get(
    "/agent-definitions/{id}",
    response_model=AgentDefinitionResponse,
)
async def get_agent_definition(
    id: int,
    db: AsyncSession = Depends(get_async_db),
    admin=Depends(require_admin),
):
    """获取单个 Agent 定义详情"""
    obj = await agent_definition.get_agent_definition(db, id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Agent定义不存在")
    return obj


@router.put(
    "/agent-definitions/{id}",
    response_model=AgentDefinitionResponse,
)
async def update_agent_definition(
    id: int,
    body: AgentDefinitionUpdate,
    db: AsyncSession = Depends(get_async_db),
    admin=Depends(require_admin),
):
    """更新 Agent 定义"""
    try:
        obj = await agent_definition.update_agent_definition(db, id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if obj is None:
        raise HTTPException(status_code=404, detail="Agent定义不存在")
    from app.agent_resolver import invalidate_agent_cache
    invalidate_agent_cache(obj.agent_type)
    return obj


@router.delete("/agent-definitions/{id}")
async def delete_agent_definition(
    id: int,
    db: AsyncSession = Depends(get_async_db),
    admin=Depends(require_admin),
) -> dict:
    """删除 Agent 定义"""
    obj = await agent_definition.delete_agent_definition(db, id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Agent定义不存在")
    from app.agent_resolver import invalidate_agent_cache
    invalidate_agent_cache(obj.agent_type)
    return {"message": "Agent定义已删除"}
