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

        Uses an asyncio.Lock to prevent concurrent creation of browsers
        for the same project (TOCTOU race condition).

        Parameters
        ----------
        project_id : int
            The project that owns the browser session.
        factory : callable
            Async callable returning a PlaywrightMCPManager instance.
            Called only when no active manager exists for the project.
        """
        async with cls._lock:
            if project_id in cls._instances:
                mgr = cls._instances[project_id]
                logger.info(
                    f"Reusing existing browser for project {project_id} "
                    f"(pool has {len(cls._instances)} active)"
                )
                return mgr

            logger.info(f"Creating new browser for project {project_id}")
            mgr = await factory()
            cls._instances[project_id] = mgr
            return mgr

    @classmethod
    async def register(cls, project_id: int, manager) -> None:
        """Register a newly created manager for *project_id*."""
        async with cls._lock:
            cls._instances[project_id] = manager

    @classmethod
    def is_active(cls, project_id: int) -> bool:
        return project_id in cls._instances

    @classmethod
    async def close(cls, project_id: int) -> None:
        """Stop the manager and remove it from the pool."""
        mgr = cls._instances.pop(project_id, None)
        if mgr is None:
            logger.warning(
                f"BrowserPool.close({project_id}): no active manager"
            )
            return
        try:
            await mgr.stop()
            logger.info(f"Browser for project {project_id} stopped")
        except Exception:
            logger.warning(
                f"Browser for project {project_id} failed to stop cleanly",
                exc_info=True,
            )


# Module-level convenience alias
browser_pool = BrowserPool
