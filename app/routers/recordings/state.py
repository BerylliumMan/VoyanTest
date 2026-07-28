"""Shared in-memory state for the recordings (CDP session recording) routers.

The WebSocket endpoint stores a :class:`RecordingSessionState` in ``_sessions``
and the status/query endpoints read from it.  Keeping the store and its
``asyncio.Lock`` in a single module avoids the circular import that would arise
if the manager and the sub-routers both tried to own the state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RecordingSessionState:
    """In-memory representation of an active CDP recording session."""

    session_id: str
    user_id: int
    url: str
    page_title: str = ""
    status: str = "recording"  # recording, stopped, completed
    start_time: float = 0.0
    last_activity_at: float = 0.0
    cdp_session_ref: object | None = None  # weak ref to CDPRecordingSession
    events_count: int = 0


# Max lifetime of a recording session in seconds (30 minutes = 1800s)
_SESSION_TTL_SECONDS: float = 1800.0

# Session is considered stale if no activity for this long (5 minutes = 300s)
_SESSION_IDLE_TIMEOUT_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

# session_id → RecordingSessionState
_sessions: dict[str, RecordingSessionState] = {}

# user_id → session_id  (only one active recording per user at a time)
_user_sessions: dict[int, str] = {}

# Guards every read/write of the dicts above so that the background CDP
# thread and the request handlers can mutate them safely.
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

async def create_session(
    session_id: str,
    user_id: int,
    url: str,
    page_title: str = "",
    cdp_session_ref: object | None = None,
) -> RecordingSessionState:
    """Create a new recording session and register it in the store.

    If the *user_id* already has an active session, it is **not** implicitly
    removed – the caller should check :func:`get_session_for_user` first.
    """
    now = time.time()
    state = RecordingSessionState(
        session_id=session_id,
        user_id=user_id,
        url=url,
        page_title=page_title,
        status="recording",
        start_time=now,
        last_activity_at=now,
        cdp_session_ref=cdp_session_ref,
    )
    async with _lock:
        _sessions[session_id] = state
        _user_sessions[user_id] = session_id
    return state


async def get_session(session_id: str) -> RecordingSessionState | None:
    """Return a **copy** of the session state, or ``None`` if missing."""
    async with _lock:
        raw = _sessions.get(session_id)
    if raw is None:
        return None
    return replace(raw)


async def touch_session_activity(
    session_id: str,
    *,
    events_count: int | None = None,
) -> None:
    """Bump ``last_activity_at`` (and optionally sync events_count)."""
    async with _lock:
        raw = _sessions.get(session_id)
        if raw is None:
            return
        raw.last_activity_at = time.time()
        if events_count is not None:
            raw.events_count = int(events_count)


def _activity_ts(state: RecordingSessionState) -> float:
    """Latest activity: store touch or CDP last_event_at."""
    activity = state.last_activity_at or state.start_time or 0.0
    cdp = state.cdp_session_ref
    if cdp is not None:
        cdp_ts = float(getattr(cdp, "last_event_at", 0) or 0)
        if cdp_ts > activity:
            activity = cdp_ts
    return activity


async def stop_session(session_id: str) -> bool:
    """Mark a session as ``stopped`` and release user. Returns ``True`` if it existed."""
    async with _lock:
        raw = _sessions.get(session_id)
        if raw is None:
            return False
        raw.status = "stopped"
        # Release the user → session mapping so the same user can start a new session
        if _user_sessions.get(raw.user_id) == session_id:
            del _user_sessions[raw.user_id]
    return True


async def get_session_for_user(user_id: int) -> RecordingSessionState | None:
    """Return the **active** session for a given user, or ``None``."""
    async with _lock:
        sid = _user_sessions.get(user_id)
        if sid is None:
            return None
        raw = _sessions.get(sid)
    if raw is None:
        return None
    return replace(raw)


async def list_sessions() -> list[RecordingSessionState]:
    """Return a shallow-copied list of all known sessions."""
    async with _lock:
        copies = [replace(s) for s in _sessions.values()]
    return copies


async def remove_session(session_id: str) -> bool:
    """Remove a session from the store.  Returns ``True`` if it existed."""
    async with _lock:
        raw = _sessions.pop(session_id, None)
        if raw is None:
            return False
        # Also clean up the user → session mapping if it points to this session.
        if _user_sessions.get(raw.user_id) == session_id:
            del _user_sessions[raw.user_id]
    return True


async def finalize_recording_session(
    state: RecordingSessionState,
    *,
    reason: str = "stopped",
) -> None:
    """Stop CDP/Agent, persist DB history, best-effort. Does not require auth."""
    session_id = state.session_id
    cdp_session = state.cdp_session_ref

    stop_fn = getattr(cdp_session, "stop_recording", None) if cdp_session is not None else None
    if stop_fn is not None:
        try:
            await stop_fn()
        except Exception as exc:
            logger.warning(
                "finalize stop CDP failed (session_id=%s reason=%s): %s",
                session_id, reason, exc,
            )

    is_agent = getattr(cdp_session, "_is_agent_recording", False) if cdp_session is not None else False
    if is_agent:
        agent_id = getattr(cdp_session, "_agent_id", None)
        if agent_id:
            try:
                from agent.manager import agent_manager
                await agent_manager.stop_agent_recording(agent_id)
            except Exception as exc:
                logger.warning(
                    "finalize stop agent failed (agent_id=%s): %s", agent_id, exc,
                )

    events_count = int(getattr(cdp_session, "events_count", 0) or 0) if cdp_session else 0
    events_json = None
    if cdp_session is not None:
        get_events = getattr(cdp_session, "get_events", None)
        if get_events:
            try:
                raw = get_events()
                if raw:
                    events_json = json.dumps(
                        [e.to_dict() for e in raw], ensure_ascii=False,
                    )
            except Exception:
                logger.debug("finalize get_events failed", exc_info=True)

    try:
        from app.database import AsyncSessionLocal
        from app import db_models
        from sqlalchemy import select

        async with AsyncSessionLocal() as _db:
            _rec = (
                await _db.execute(
                    select(db_models.RecordingSession).where(
                        db_models.RecordingSession.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if _rec:
                _rec.status = "stopped"
                _rec.ended_at = datetime.utcnow()
                _rec.events_count = events_count
                if events_json:
                    _rec.events_data = events_json
                await _db.commit()
    except Exception:
        logger.warning(
            "finalize DB update failed (session_id=%s reason=%s)",
            session_id, reason, exc_info=True,
        )


async def cleanup_stale_sessions() -> int:
    """Remove sessions past hard TTL or true idle (no activity).

    Idle uses ``last_activity_at`` / CDP ``last_event_at``, not start_time.
    Finalizes CDP/Agent/DB before dropping memory state.
    """
    now = time.time()
    stale: list[RecordingSessionState] = []

    async with _lock:
        for sid, state in list(_sessions.items()):
            started = state.start_time if state.start_time > 0 else now
            age = now - started

            if age > _SESSION_TTL_SECONDS:
                popped = _sessions.pop(sid, None)
                if popped and _user_sessions.get(popped.user_id) == sid:
                    del _user_sessions[popped.user_id]
                if popped:
                    stale.append(popped)
                continue

            if state.status == "recording":
                activity = _activity_ts(state)
                idle_for = now - (activity if activity > 0 else started)
                if idle_for > _SESSION_IDLE_TIMEOUT_SECONDS:
                    popped = _sessions.pop(sid, None)
                    if popped and _user_sessions.get(popped.user_id) == sid:
                        del _user_sessions[popped.user_id]
                    if popped:
                        stale.append(popped)

    for state in stale:
        try:
            await finalize_recording_session(state, reason="stale_cleanup")
        except Exception:
            logger.warning(
                "stale finalize failed session_id=%s", state.session_id, exc_info=True,
            )

    return len(stale)
