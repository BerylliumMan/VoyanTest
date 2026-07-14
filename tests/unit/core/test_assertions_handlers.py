"""Tests for core/assertions.py MCP assertion handlers (5 types)."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.assertions import (
    _assert_url_contains,
    _assert_text_exists,
    _assert_element_visible,
    _assert_input_value,
    _assert_element_count,
    execute_assertions,
)


class TestAssertUrlContains:
    @pytest.mark.asyncio
    async def test_passes_when_url_contains(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "https://example.com/dashboard"})
        result = await _assert_url_contains(mcp, "/dashboard")
        assert result["passed"] is True
        assert result["actual"] == "https://example.com/dashboard"

    @pytest.mark.asyncio
    async def test_fails_when_url_does_not_contain(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "https://example.com/login"})
        result = await _assert_url_contains(mcp, "/dashboard")
        assert result["passed"] is False
        assert "/dashboard" not in result["actual"]

    @pytest.mark.asyncio
    async def test_fails_when_evaluate_unsuccessful(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": False, "error": "timeout"})
        result = await _assert_url_contains(mcp, "/x")
        assert result["passed"] is False


class TestAssertTextExists:
    @pytest.mark.asyncio
    async def test_passes_when_text_found(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "欢迎登录我们的网站"})
        result = await _assert_text_exists(mcp, "欢迎登录")
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_fails_when_text_not_found(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "page content here"})
        result = await _assert_text_exists(mcp, "nonexistent")
        assert result["passed"] is False


class TestAssertElementVisible:
    @pytest.mark.asyncio
    async def test_passes_when_element_visible(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "visible"})
        result = await _assert_element_visible(mcp, "#submit-btn")
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_fails_when_element_not_visible(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "hidden"})
        result = await _assert_element_visible(mcp, "#hidden")
        assert result["passed"] is False

    @pytest.mark.asyncio
    async def test_fails_when_element_not_found(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "not_found"})
        result = await _assert_element_visible(mcp, "#missing")
        assert result["passed"] is False


class TestAssertInputValue:
    @pytest.mark.asyncio
    async def test_passes_when_value_matches(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "test@example.com"})
        result = await _assert_input_value(mcp, "#email=test@example.com")
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_fails_when_value_does_not_match(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "wrong@value.com"})
        result = await _assert_input_value(mcp, "#email=expected@value.com")
        assert result["passed"] is False


class TestAssertElementCount:
    @pytest.mark.asyncio
    async def test_passes_when_count_matches(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "3"})
        result = await _assert_element_count(mcp, ".item=3")
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_fails_when_count_does_not_match(self):
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "5"})
        result = await _assert_element_count(mcp, ".item=3")
        assert result["passed"] is False
