from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from .. import models
from ..database import get_async_db
from ..auth import require_admin, get_current_user
from .. import crud
from app.tz import now as tz_now

router = APIRouter(
    prefix="/api",
    tags=["Agent管理"],
)


@router.get("/agents", response_model=List[models.Agent])
async def list_agents(user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> list[models.Agent]:
    """不直接用 DB 的 `status` 字段——它会"粘性 online"（一旦设过永远不重置）。

    status 按心跳时间动态算，看两个源头：DB.last_heartbeat（HTTP 路径）
    和 agent_manager.sessions[].last_seen（WebSocket 路径），任一 fresh 即 online。
    """
    ONLINE_TIMEOUT_SECONDS = 120
    now = tz_now()

    from agent.manager import agent_manager

    def _is_online(name: str, db_last_heartbeat) -> bool:
        if db_last_heartbeat is not None:
            hb = db_last_heartbeat if db_last_heartbeat.tzinfo else db_last_heartbeat.replace(tzinfo=now.tzinfo)
            if (now - hb).total_seconds() < ONLINE_TIMEOUT_SECONDS:
                return True

        session = agent_manager.sessions.get(name)
        if session is not None and session.agent.last_seen is not None:
            ls = session.agent.last_seen if session.agent.last_seen.tzinfo else session.agent.last_seen.replace(tzinfo=now.tzinfo)
            if (now - ls).total_seconds() < ONLINE_TIMEOUT_SECONDS:
                return True

        return False

    db_agents = await crud.list_agents(db)
    db_names = {a.name for a in db_agents}

    for a in db_agents:
        a.status = "online" if _is_online(a.name, a.last_heartbeat) else "offline"

    ws_agents = agent_manager.get_online_agents()
    for ws_a in ws_agents:
        if ws_a.name not in db_names:
            db_agents.append(models.Agent(
                id=0,
                name=ws_a.name,
                endpoint=ws_a.ip_address,
                description=f"WebSocket Agent ({ws_a.hostname})",
                status="online",
                last_heartbeat=ws_a.last_seen,
            ))

    return db_agents


@router.post("/agents/register", response_model=models.Agent)
async def register_agent(agent: models.AgentCreate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.Agent:
    if await crud.get_agent_by_name(db, agent.name) is not None:
        raise HTTPException(status_code=400, detail="Agent name already exists")
    return await crud.create_agent(db, agent)


@router.put("/agents/{agent_id}", response_model=models.Agent)
async def update_agent(agent_id: int, agent: models.AgentUpdate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> models.Agent:
    db_agent = await crud.update_agent(db, agent_id, agent)
    if db_agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return db_agent


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> dict:
    if await crud.delete_agent(db, agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"message": "Agent deleted"}


@router.get("/agents/{agent_id}/logs", response_model=models.AgentLogPage)
async def get_agent_logs(
    agent_id: int,
    user=Depends(get_current_user),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
) -> models.AgentLogPage:
    if await crud.get_agent(db, agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return await crud.list_agent_logs(db, agent_id, page, size)


@router.post("/agents/{agent_id}/heartbeat", response_model=models.Agent)
async def agent_heartbeat(agent_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.Agent:
    db_agent = await crud.update_agent_heartbeat(db, agent_id)
    if db_agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return db_agent


@router.get("/agents/{agent_id}/stats")
async def agent_stats(agent_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """获取 Agent 执行统计。"""
    from sqlalchemy import select, func
    from app import db_models

    agent = await crud.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    total = await db.execute(
        select(func.count()).select_from(db_models.TestRun).where(
            db_models.TestRun.execution_mode == "client"
        )
    )
    return {"total_runs": total.scalar() or 0}