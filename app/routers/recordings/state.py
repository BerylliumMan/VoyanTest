"""Shared in-memory state for the recordings (CDP session recording) routers.

The WebSocket endpoint stores a :class:`RecordingSessionState` in ``_sessions``
and the status/query endpoints read from it.  Keeping the store and its
``asyncio.Lock`` in a single module avoids the circular import that would arise
if the manager and the sub-routers both tried to own the state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace


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
    cdp_session_ref: object | None = None  # weak ref to CDPRecordingSession
    events_count: int = 0


# Max lifetime of a recording session in seconds (30 minutes = 1800s)
_SESSION_TTL_SECONDS: float = 1800.0

# Session is considered stale if no events recorded for this long (5 minutes = 300s)
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
    state = RecordingSessionState(
        session_id=session_id,
        user_id=user_id,
        url=url,
        page_title=page_title,
        status="recording",
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


async def stop_session(session_id: str) -> bool:
    """Mark a session as ``stopped``.  Returns ``True`` if it existed."""
    async with _lock:
        raw = _sessions.get(session_id)
        if raw is None:
            return False
        raw.status = "stopped"
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


async def cleanup_stale_sessions() -> int:
    """Remove recording sessions that have exceeded TTL or idle timeout.

    Scans all sessions in the store and removes those that have been
    running longer than ``_SESSION_TTL_SECONDS``, or that are
    in "recording" status but have been idle (no events, no status change)
    for longer than ``_SESSION_IDLE_TIMEOUT_SECONDS``.

    Returns the number of sessions removed.
    """
    now = time.time()
    removed = 0

    async with _lock:
        stale_ids = []
        for sid, state in list(_sessions.items()):
            age = now - (state.start_time or now)

            # Hard TTL exceeded
            if age > _SESSION_TTL_SECONDS:
                stale_ids.append(sid)
                continue

            # Idle timeout for recording sessions
            if state.status == "recording" and age > _SESSION_IDLE_TIMEOUT_SECONDS:
                stale_ids.append(sid)
                continue

        for sid in stale_ids:
            state = _sessions.pop(sid, None)
            if state and _user_sessions.get(state.user_id) == sid:
                del _user_sessions[state.user_id]
            removed += 1

    return removed
