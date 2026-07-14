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
