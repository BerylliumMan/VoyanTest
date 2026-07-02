"""Agent router — HTTP API for agent listing + WebSocket for agent communication."""

import json
import logging
from typing import List

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from .models import AgentInfo, AgentRegistration, WSMessage, WSMessageType
from .manager import agent_manager
from app.tz import now as tz_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["Agents"])


# ---- HTTP: Agent listing ----

@router.get("/", response_model=List[AgentInfo])
async def list_agents():
    return agent_manager.get_online_agents()


# ---- WebSocket: Agent communication ----

@router.websocket("/ws/{agent_name}")
async def agent_websocket(ws: WebSocket, agent_name: str):
    """WebSocket endpoint for agent clients. Each agent connects here with its name."""

    # Auth: require valid session_id cookie (mirrors app/websocket.py:89-106)
    # Support both Cookie header and ?token= query param
    session_id = ws.cookies.get("session_id") or ws.query_params.get("token")
    if not session_id:
        await ws.close(code=4001, reason="missing session_id")
        return
    from app.database import AsyncSessionLocal
    from app.auth import get_session
    async with AsyncSessionLocal() as _auth_db:
        _session = await get_session(_auth_db, session_id)
        if not _session:
            await ws.close(code=4003, reason="invalid session")
            return

    await ws.accept()
    agent_id = agent_name
    session = None

    async def _send(raw: str):
        await ws.send_text(raw)

    async def _sync_to_db(name: str, ip: str, hostname: str):
        """Create or update AgentDB record for this WebSocket agent."""
        try:
            from app.database import AsyncSessionLocal
            from app.db_models import Agent as AgentDBModel
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(AgentDBModel).where(AgentDBModel.name == name))
                existing = result.scalar_one_or_none()
                if existing:
                    existing.status = "online"
                    existing.last_heartbeat = tz_now()
                else:
                    agent = AgentDBModel(
                        name=name,
                        endpoint=f"ws://{ip}" if ip else "",
                        description=f"WebSocket Agent ({hostname})",
                        status="online",
                    )
                    db.add(agent)
                await db.commit()
        except Exception as exc:
            logger.warning(f"Failed to sync agent to DB: {exc}")

    try:
        while True:
            raw = await ws.receive_text()
            msg = WSMessage(**json.loads(raw))

            if msg.type == WSMessageType.REGISTERED:
                reg = AgentRegistration(**msg.payload)
                agent_id = agent_name
                agent = agent_manager.register(agent_id, reg, _send)
                session = agent_manager.get_session(agent_id)
                await _sync_to_db(agent_name, reg.ip_address, reg.hostname)
                ack = WSMessage(type=WSMessageType.REGISTERED, agent_id=agent_id,
                                payload={"status": "ok", "message": f"Registered as {agent_id}"})
                await session.send(ack) if session else None

            elif msg.type == WSMessageType.HEARTBEAT:
                agent_manager.heartbeat(agent_id)
                await _sync_to_db(agent_name, "", "")

            elif msg.type in (WSMessageType.STEP_RESULT, WSMessageType.SNAPSHOT_RESULT,
                              WSMessageType.SCREENSHOT_RESULT, WSMessageType.RUN_COMPLETE,
                              WSMessageType.RECORDING_READY, WSMessageType.RECORDING_EVENTS):
                if session:
                    session.resolve(msg)

            elif msg.type == WSMessageType.ERROR:
                logger.error(f"Agent {agent_id} error: {msg.payload.get('message')}")
                if session:
                    session.resolve(msg)

    except WebSocketDisconnect:
        logger.info(f"Agent {agent_id} disconnected")
    except Exception as e:
        logger.error(f"Agent {agent_id} error: {e}")
    finally:
        agent_manager.unregister(agent_id)
