"""Shared in-memory state for the gen (AI test case generation) routers.

The upload endpoint stores an :class:`AnalysisSession` in ``_sessions`` and the
preview/import/history endpoints read from it.  Keeping the store and its
``asyncio.Lock`` in a single module avoids the circular import that would arise
if the aggregator and the sub-routers both tried to own the state.
"""
import asyncio

# In-memory session store (survives within process lifetime).
# Keyed by ``session_id`` (uuid4 string).  Values are
# :class:`app.gen.models.AnalysisSession` instances.
_sessions: dict = {}

# Guards every read/write of ``_sessions`` so that the background analysis
# thread and the request handlers can mutate it safely.
_lock = asyncio.Lock()

# Sessions the user requested to stop (checked cooperatively in the pipeline).
_cancelled_sessions: set[str] = set()

# Background ``asyncio.Task`` per session (same worker that started upload).
_gen_tasks: dict[str, asyncio.Task] = {}


def is_gen_cancelled(session_id: str) -> bool:
    return session_id in _cancelled_sessions


async def register_gen_task(session_id: str, task: asyncio.Task) -> None:
    async with _lock:
        _gen_tasks[session_id] = task


async def request_cancel_gen(session_id: str) -> None:
    """Mark session cancelled and cancel the local background task if any."""
    async with _lock:
        _cancelled_sessions.add(session_id)
        task = _gen_tasks.get(session_id)
    if task and not task.done():
        task.cancel()


async def clear_gen_runtime(session_id: str) -> None:
    async with _lock:
        _cancelled_sessions.discard(session_id)
        _gen_tasks.pop(session_id, None)


def make_cancel_checker(session_id: str):
    return lambda: is_gen_cancelled(session_id)
