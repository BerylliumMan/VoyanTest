"""
Browser pool — lightweight singleton managing shared PlaywrightMCPManager per project.

Ensures batch runs and single-case runs reuse the same browser instance
instead of spawning independent processes.  Per-project locks serialize
execution so concurrent runs cannot interleave on the same browser.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BrowserPool:
    """Singleton that tracks the active PlaywrightMCPManager per project."""

    _instances: dict[int, object] = {}  # project_id -> PlaywrightMCPManager
    _lock: asyncio.Lock = asyncio.Lock()
    _project_locks: dict[int, asyncio.Lock] = {}
    _creating: dict[int, asyncio.Future] = {}

    @classmethod
    async def _get_project_lock(cls, project_id: int) -> asyncio.Lock:
        async with cls._lock:
            lock = cls._project_locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                cls._project_locks[project_id] = lock
            return lock

    @classmethod
    @asynccontextmanager
    async def project_lock(cls, project_id: int) -> AsyncIterator[None]:
        """Serialize all browser work for a project (single-case or batch)."""
        lock = await cls._get_project_lock(project_id)
        async with lock:
            yield

    @classmethod
    async def run_exclusive(
        cls,
        project_id: int,
        coro_factory: Callable[[], Awaitable[T]],
    ) -> T:
        """Run ``coro_factory()`` while holding the project lock."""
        async with cls.project_lock(project_id):
            return await coro_factory()

    @classmethod
    async def get_or_create(cls, project_id: int, factory) -> object:
        """Return the active manager for *project_id*, or create one.

        Concurrent creators wait on the same Future so only one browser is
        spawned per project.
        """
        async with cls._lock:
            if project_id in cls._instances:
                mgr = cls._instances[project_id]
                logger.info(
                    "Reusing existing browser for project %s "
                    "(pool has %s active)",
                    project_id, len(cls._instances),
                )
                return mgr
            fut = cls._creating.get(project_id)
            if fut is not None:
                creating = False
            else:
                fut = asyncio.get_running_loop().create_future()
                cls._creating[project_id] = fut
                creating = True

        if not creating:
            return await fut

        try:
            logger.info("Creating new browser for project %s", project_id)
            mgr = await factory()
            async with cls._lock:
                cls._instances[project_id] = mgr
                cls._creating.pop(project_id, None)
                if not fut.done():
                    fut.set_result(mgr)
            return mgr
        except Exception as exc:
            async with cls._lock:
                cls._creating.pop(project_id, None)
                if not fut.done():
                    fut.set_exception(exc)
            raise

    @classmethod
    async def register(cls, project_id: int, manager) -> None:
        """Register a newly created manager for *project_id*."""
        async with cls._lock:
            cls._instances[project_id] = manager

    @classmethod
    async def is_active(cls, project_id: int) -> bool:
        async with cls._lock:
            return project_id in cls._instances

    @classmethod
    async def get(cls, project_id: int):
        """Return active manager for *project_id*, or None under lock."""
        async with cls._lock:
            return cls._instances.get(project_id)

    @classmethod
    async def close(cls, project_id: int) -> None:
        """Stop the manager and remove it from the pool."""
        async with cls._lock:
            mgr = cls._instances.pop(project_id, None)
        if mgr is None:
            logger.warning(
                "BrowserPool.close(%s): no active manager", project_id,
            )
            return
        try:
            await mgr.stop()
            logger.info("Browser for project %s stopped", project_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Browser for project %s failed to stop cleanly",
                project_id,
                exc_info=True,
            )


browser_pool = BrowserPool
