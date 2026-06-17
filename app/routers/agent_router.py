from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List
from sqlalchemy.orm import Session
from .. import models
from ..database import get_db
from ..auth import require_admin
from .. import crud
from app.tz import now as tz_now

router = APIRouter(
    prefix="/api",
    tags=["Agent管理"],
)


@router.get("/agents", response_model=List[models.Agent])
def list_agents(db: Session = Depends(get_db)) -> list[models.Agent]:
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

    db_agents = crud.list_agents(db)
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
def register_agent(agent: models.AgentCreate, admin=Depends(require_admin), db: Session = Depends(get_db)) -> models.Agent:
    if crud.get_agent_by_name(db, agent.name) is not None:
        raise HTTPException(status_code=400, detail="Agent name already exists")
    return crud.create_agent(db, agent)


@router.put("/agents/{agent_id}", response_model=models.Agent)
def update_agent(agent_id: int, agent: models.AgentUpdate, admin=Depends(require_admin), db: Session = Depends(get_db)) -> models.Agent:
    db_agent = crud.update_agent(db, agent_id, agent)
    if db_agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return db_agent


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: int, admin=Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if crud.delete_agent(db, agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"message": "Agent deleted"}


@router.get("/agents/{agent_id}/logs", response_model=models.AgentLogPage)
def get_agent_logs(
    agent_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> models.AgentLogPage:
    if crud.get_agent(db, agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return crud.list_agent_logs(db, agent_id, page, size)


@router.post("/agents/{agent_id}/heartbeat", response_model=models.Agent)
def agent_heartbeat(agent_id: int, db: Session = Depends(get_db)) -> models.Agent:
    db_agent = crud.update_agent_heartbeat(db, agent_id)
    if db_agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return db_agent