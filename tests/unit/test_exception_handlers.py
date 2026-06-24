"""Tests for app/exception_handlers.py — global exception handling."""
from unittest.mock import MagicMock
import pytest
from app.exception_handlers import unhandled_exception_handler


class TestUnhandledExceptionHandler:
    @pytest.mark.asyncio
    async def test_returns_500(self):
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/api/test"
        response = await unhandled_exception_handler(request, ValueError("boom"))
        assert response.status_code == 500
        assert response.body is not None

    @pytest.mark.asyncio
    async def test_returns_json_with_detail(self):
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/api/unknown"
        response = await unhandled_exception_handler(request, RuntimeError())
        body = response.body.decode()
        assert "Internal server error" in body
