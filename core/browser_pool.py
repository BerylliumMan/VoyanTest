"""
Browser pool — lightweight singleton managing shared PlaywrightMCPManager per project.

Ensures batch runs and single-case runs reuse the same browser instance
instead of spawning independent processes.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BrowserPool:
    """Singleton that tracks the active PlaywrightMCPManager per project.

    Thread-safety note: this module is used from FastAPI's async event loop
    (BackgroundTasks runs each task in its own thread via anyio).
    For the current use-case (single-process sequential) a plain dict is
    sufficient. If concurrent batch runs become a requirement, swap to
    an asyncio.Lock here.
    """

    _instances: dict[int, object] = {}  # project_id -> PlaywrightMCPManager
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_or_create(cls, project_id: int, factory) -> object:
        """Return the active manager for *project_id*, or create one.

        Uses double-checked locking to avoid holding the lock during
        the potentially slow factory() call (browser creation 3-10s).
        """
        # First check (fast path, no lock)
        if project_id in cls._instances:
            mgr = cls._instances[project_id]
            logger.info(
                f"Reusing existing browser for project {project_id} "
                f"(pool has {len(cls._instances)} active)"
            )
            return mgr

        async with cls._lock:
            # Second check (under lock, prevent TOCTOU)
            if project_id in cls._instances:
                return cls._instances[project_id]
            logger.info("Creating new browser for project %s", project_id)
            # 在锁外创建浏览器，避免阻塞整个 pool
        mgr = await factory()
        async with cls._lock:
            cls._instances[project_id] = mgr
        return mgr

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
                f"BrowserPool.close({project_id}): no active manager"
            )
            return
        try:
            await mgr.stop()
            logger.info("Browser for project %s stopped", project_id)
        except Exception:  # noqa: BLE001 - 浏览器关闭属清理阶段，需吞掉所有错误
            logger.warning(
                f"Browser for project {project_id} failed to stop cleanly",
                exc_info=True,
            )


# Module-level convenience alias
browser_pool = BrowserPool
