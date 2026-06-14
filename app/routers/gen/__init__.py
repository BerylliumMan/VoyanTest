"""API router for AI test case generation.

Sub-routers (each owns a single concern):

* :mod:`.upload`        — ``POST /api/gen/upload`` (file upload + analysis)
* :mod:`.preview`       — ``GET  /api/gen/status/{session_id}`` and
                          ``GET  /api/gen/preview/{session_id}``
* :mod:`.import_routes` — ``POST /api/gen/import``
* :mod:`.history`       — all ``/api/gen/history*`` endpoints

Shared state (``_sessions`` dict and ``_lock``) lives in :mod:`.state`; the
Pydantic request/response models live in :mod:`.schemas`.  Both are imported
by whichever sub-router needs them, avoiding circular imports.
"""
from fastapi import APIRouter

from . import history, import_routes, preview, upload

router = APIRouter(prefix="/api/gen", tags=["用例生成"])

# Order is mostly cosmetic — each sub-router owns disjoint URL patterns — but
# we keep the original "lifecycle" order for predictability:
#   upload (creation) -> preview (read in-memory) -> import (consume) -> history (audit)
router.include_router(upload.router)
router.include_router(preview.router)
router.include_router(import_routes.router)
router.include_router(history.router)

__all__ = ["router"]
