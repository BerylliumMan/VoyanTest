"""Tests for assertion parsing logic in core/assertions.py (no MCP needed)."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.assertions import (
    _parse_single_assertion,
    _looks_like_selector,
    ASSERTION_TYPE_MAP,
)


class TestAssertionTypeMap:
    """ASSERTION_TYPE_MAP has all 5 types mapped to correct handler names."""

    def test_all_five_types_present(self):
        assert set(ASSERTION_TYPE_MAP.keys()) == {
            "url_contains", "text_exists", "element_visible",
            "input_value", "element_count",
        }

    def test_handler_names_start_with_underscore_assert(self):
        for name in ASSERTION_TYPE_MAP.values():
            assert name.startswith("_assert_"), f"{name} is not a valid handler"


class TestLooksLikeSelector:
    def test_returns_false_for_empty(self):
        assert _looks_like_selector("") is False

    def test_returns_true_for_id_selector(self):
        assert _looks_like_selector("#login-btn") is True

    def test_returns_true_for_class_selector(self):
        assert _looks_like_selector(".submit-button") is True

    def test_returns_true_for_attribute_selector(self):
        assert _looks_like_selector('[data-test="btn"]') is True

    def test_returns_false_for_text(self):
        assert _looks_like_selector("普通文本") is False


class TestParseSingleAssertion:
    def test_url_contains_explicit(self):
        result = _parse_single_assertion("页面URL包含 /dashboard")
        assert result == {"type": "url_contains", "value": "/dashboard"}

    def test_url_contains_with_colon(self):
        result = _parse_single_assertion("URL：/login")
        assert result == {"type": "url_contains", "value": "/login"}

    def test_url_with_jump_and_colon(self):
        result = _parse_single_assertion("跳转: /home")
        assert result == {"type": "url_contains", "value": "/home"}

    def test_element_count(self):
        result = _parse_single_assertion("#items 数量 5")
        assert result == {"type": "element_count", "value": "#items=5"}

    def test_element_visible_with_hash(self):
        result = _parse_single_assertion("#submit 可见")
        assert result == {"type": "element_visible", "value": "#submit"}

    def test_element_visible_exists(self):
        result = _parse_single_assertion("元素 #header 存在")
        assert result == {"type": "element_visible", "value": "#header"}

    def test_input_value(self):
        result = _parse_single_assertion("#email 的值是 test@test.com")
        assert result == {"type": "input_value", "value": "#email=test@test.com"}

    def test_text_exists_with_quotes(self):
        result = _parse_single_assertion("存在文字'提交成功'")
        assert result == {"type": "text_exists", "value": "提交成功"}

    def test_text_exists_contains(self):
        result = _parse_single_assertion("包含：欢迎光临")
        assert result == {"type": "text_exists", "value": "欢迎光临"}

    def test_heuristic_url_fallback(self):
        result = _parse_single_assertion("/api/projects")
        assert result == {"type": "url_contains", "value": "/api/projects"}

    def test_heuristic_selector_fallback(self):
        result = _parse_single_assertion("#logout-button")
        assert result == {"type": "element_visible", "value": "#logout-button"}

    def test_unrecognized_returns_none(self):
        result = _parse_single_assertion("一些无法识别的文本")
        assert result is None

    def test_empty_returns_none(self):
        result = _parse_single_assertion("")
        assert result is None

    def test_display_text_without_quotes(self):
        result = _parse_single_assertion("显示 操作成功")
        assert result == {"type": "text_exists", "value": "操作成功"}


class TestExecuteAssertions:
    """Tests for execute_assertions dispatch logic (MCP mocked)."""

    @pytest.mark.asyncio
    async def test_unknown_type_returns_failed(self):
        from core.assertions import execute_assertions
        mcp = MagicMock()
        results = await execute_assertions(mcp, [{"type": "nonexistent", "value": ""}])
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert "Unknown" in results[0]["error"]

    @pytest.mark.asyncio
    async def test_handler_exception_caught(self):
        from core.assertions import execute_assertions
        mcp = MagicMock()
        results = await execute_assertions(mcp, [{"type": "url_contains", "value": "/"}])
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        from core.assertions import execute_assertions
        mcp = MagicMock()
        results = await execute_assertions(mcp, [])
        assert results == []

    @pytest.mark.asyncio
    async def test_multiple_assertions_all_work(self):
        from core.assertions import execute_assertions
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "https://example.com/dashboard"})
        results = await execute_assertions(mcp, [
            {"type": "url_contains", "value": "/dashboard"},
            {"type": "text_exists", "value": "hello"},
        ])
        assert len(results) == 2
