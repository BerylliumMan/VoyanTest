"""In-process control plane for batch run pause / resume / stop.

Mirrors the gen-cancel pattern (cooperative flags + optional Task registry).
Multi-worker: only the worker that owns the batch Task sees the flags.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

# batch_id -> flags
_paused: set[int] = set()
_stopped: set[int] = set()

# When paused, waiters block on this Event until resume/stop sets it.
_resume_events: dict[int, asyncio.Event] = {}

# Background asyncio.Task that owns the batch (same worker).
_batch_tasks: dict[int, asyncio.Task] = {}


def is_paused(batch_id: int) -> bool:
    return batch_id in _paused and batch_id not in _stopped


def is_stopped(batch_id: int) -> bool:
    return batch_id in _stopped


async def register_batch_task(batch_id: int, task: asyncio.Task) -> None:
    async with _lock:
        _batch_tasks[batch_id] = task
        _stopped.discard(batch_id)
        _paused.discard(batch_id)
        ev = _resume_events.get(batch_id)
        if ev is None:
            _resume_events[batch_id] = asyncio.Event()
            _resume_events[batch_id].set()
        else:
            ev.set()


async def clear_batch(batch_id: int) -> None:
    async with _lock:
        _paused.discard(batch_id)
        _stopped.discard(batch_id)
        _batch_tasks.pop(batch_id, None)
        _resume_events.pop(batch_id, None)


async def request_pause(batch_id: int) -> bool:
    """Mark batch paused. Returns False if already stopped."""
    async with _lock:
        if batch_id in _stopped:
            return False
        _paused.add(batch_id)
        ev = _resume_events.get(batch_id)
        if ev is None:
            _resume_events[batch_id] = asyncio.Event()
        else:
            ev.clear()
    logger.info("Batch %s pause requested", batch_id)
    return True


async def request_resume(batch_id: int) -> bool:
    """Clear pause and wake waiters. Returns False if stopped."""
    async with _lock:
        if batch_id in _stopped:
            return False
        _paused.discard(batch_id)
        ev = _resume_events.get(batch_id)
        if ev is None:
            _resume_events[batch_id] = asyncio.Event()
            ev = _resume_events[batch_id]
        ev.set()
    logger.info("Batch %s resume requested", batch_id)
    return True


async def request_stop(batch_id: int) -> None:
    """Mark stopped, clear pause, wake waiters (cooperative; no Task.cancel)."""
    async with _lock:
        _stopped.add(batch_id)
        _paused.discard(batch_id)
        ev = _resume_events.get(batch_id)
        if ev is None:
            _resume_events[batch_id] = asyncio.Event()
            ev = _resume_events[batch_id]
        ev.set()
    logger.info("Batch %s stop requested", batch_id)


async def wait_if_paused(batch_id: int) -> None:
    """Block while paused; return immediately if stopped or not paused."""
    while True:
        if is_stopped(batch_id):
            return
        if not is_paused(batch_id):
            return
        async with _lock:
            ev = _resume_events.get(batch_id)
            if ev is None:
                _resume_events[batch_id] = asyncio.Event()
                ev = _resume_events[batch_id]
                if not is_paused(batch_id):
                    ev.set()
        await ev.wait()


def get_registered_task(batch_id: int) -> Optional[asyncio.Task]:
    return _batch_tasks.get(batch_id)
