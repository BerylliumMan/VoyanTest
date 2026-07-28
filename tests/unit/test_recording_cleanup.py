"""Unit tests for recording session idle/TTL cleanup."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.routers.recordings import state as rec_state


def _reset_store():
    rec_state._sessions.clear()
    rec_state._user_sessions.clear()


@pytest.mark.asyncio
async def test_cleanup_keeps_active_recording_past_5_minutes():
    """Idle must use last_activity_at, not start_time — active sessions survive."""
    _reset_store()
    now = time.time()
    await rec_state.create_session("rec-active", user_id=1, url="https://x")
    async with rec_state._lock:
        raw = rec_state._sessions["rec-active"]
        raw.start_time = now - 600
        raw.last_activity_at = now - 30

    with patch.object(rec_state, "finalize_recording_session", new_callable=AsyncMock) as fin:
        removed = await rec_state.cleanup_stale_sessions()
        assert removed == 0
        fin.assert_not_called()

    assert await rec_state.get_session("rec-active") is not None
    _reset_store()


@pytest.mark.asyncio
async def test_cleanup_removes_truly_idle_and_finalizes():
    _reset_store()
    now = time.time()
    await rec_state.create_session("rec-idle", user_id=2, url="https://y")
    async with rec_state._lock:
        raw = rec_state._sessions["rec-idle"]
        raw.start_time = now - 400
        raw.last_activity_at = now - 400

    with patch.object(rec_state, "finalize_recording_session", new_callable=AsyncMock) as fin:
        removed = await rec_state.cleanup_stale_sessions()
        assert removed == 1
        fin.assert_awaited_once()
        assert fin.await_args.args[0].session_id == "rec-idle"

    assert await rec_state.get_session("rec-idle") is None
    _reset_store()


@pytest.mark.asyncio
async def test_cleanup_respects_cdp_last_event_at():
    _reset_store()
    now = time.time()
    await rec_state.create_session("rec-cdp", user_id=3, url="https://z")

    class FakeCdp:
        last_event_at = now - 10
        events_count = 3

    async with rec_state._lock:
        raw = rec_state._sessions["rec-cdp"]
        raw.start_time = now - 600
        raw.last_activity_at = now - 600
        raw.cdp_session_ref = FakeCdp()

    with patch.object(rec_state, "finalize_recording_session", new_callable=AsyncMock) as fin:
        removed = await rec_state.cleanup_stale_sessions()
        assert removed == 0
        fin.assert_not_called()
    _reset_store()
