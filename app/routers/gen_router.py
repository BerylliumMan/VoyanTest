"""API router for AI test case generation.

This module is now a thin re-export shim — the actual route definitions live
under :mod:`app.routers.gen` (one file per concern: ``upload``, ``preview``,
``import_routes``, ``history``).  The shared in-memory session store and
Pydantic models live in :mod:`app.routers.gen.state` and
:mod:`app.routers.gen.schemas` respectively.

The shim is kept so existing call sites (notably ``app.main``) can keep
importing ``from .routers import gen_router`` and then using
``gen_router.router`` without any change.
"""
from app.routers.gen import router

__all__ = ["router"]
