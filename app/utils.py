"""Shared utility functions for the application."""

from fastapi import Request


def client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
