"""API router for CDP-based user action recording.

Thin re-export shim — the actual route definitions will live under
:mod:`app.routers.recordings` (one file per concern, added in Wave 2).
The shared in-memory session store and Pydantic models live in
:mod:`app.routers.recordings.state` and :mod:`app.routers.recordings.schemas`
respectively.

The shim is kept so existing call sites (notably ``app.main``) can keep
importing ``from .routers import recordings_router`` and then using
``recordings_router.router`` without any change.
"""
from app.routers.recordings import router

__all__ = ["router"]
