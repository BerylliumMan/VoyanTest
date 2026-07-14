"""Tests for core/playwright_manager.py — static methods and constants."""
from core.playwright_manager import (
    PlaywrightMCPManager,
    SUPPORTED_BROWSERS,
    ACTION_TOOL_MAP,
)


class TestConstants:
    def test_supported_browsers(self):
        assert "chromium" in SUPPORTED_BROWSERS
        assert "firefox" in SUPPORTED_BROWSERS
        assert "webkit" in SUPPORTED_BROWSERS

    def test_action_tool_map(self):
        assert ACTION_TOOL_MAP["click"] == "browser_click"
        assert ACTION_TOOL_MAP["fill"] == "browser_type"
        assert ACTION_TOOL_MAP["goto"] == "browser_navigate"
        assert ACTION_TOOL_MAP["screenshot"] == "browser_take_screenshot"


class TestBuildMcpArgs:
    def test_goto(self):
        assert PlaywrightMCPManager._build_mcp_args("goto", None, "https://x.com") == {"url": "https://x.com"}

    def test_goto_default_url(self):
        args = PlaywrightMCPManager._build_mcp_args("goto", None, None)
        assert args["url"]

    def test_click(self):
        assert PlaywrightMCPManager._build_mcp_args("click", "#btn", None) == {"element": "#btn", "target": "#btn"}

    def test_fill(self):
        r = PlaywrightMCPManager._build_mcp_args("fill", "#email", "a@b.com")
        assert r["element"] == "#email" and r["text"] == "a@b.com"

    def test_select(self):
        r = PlaywrightMCPManager._build_mcp_args("select", "#sel", "opt1")
        assert r["values"] == ["opt1"]

    def test_wait_with_digit(self):
        r = PlaywrightMCPManager._build_mcp_args("wait", None, "3000")
        assert r["time"] == 3000

    def test_wait_with_text(self):
        r = PlaywrightMCPManager._build_mcp_args("wait", None, "loading")
        assert r["text"] == "loading"

    def test_screenshot(self):
        r = PlaywrightMCPManager._build_mcp_args("screenshot", None, "/tmp/ss.png")
        assert r["filename"] == "/tmp/ss.png"
        assert r["fullPage"] is True
        assert r["type"] == "png"

    def test_snapshot(self):
        assert PlaywrightMCPManager._build_mcp_args("snapshot", None, None) == {}

    def test_assert_text(self):
        assert PlaywrightMCPManager._build_mcp_args("assert_text", None, "hello") == {"text": "hello"}

    def test_unknown_action(self):
        assert PlaywrightMCPManager._build_mcp_args("unknown", None, None) == {}


class TestPlaywrightMCPManager:
    def test_default_constructor(self):
        mgr = PlaywrightMCPManager()
        assert mgr.browser_type == "chromium"
        assert mgr.headless is True
        assert mgr._session is None

    def test_firefox_constructor(self):
        mgr = PlaywrightMCPManager(browser_type="firefox", headless=False)
        assert mgr.browser_type == "firefox"
        assert mgr.headless is False

    def test_session_property_before_start_raises(self):
        mgr = PlaywrightMCPManager()
        import pytest
        with pytest.raises(RuntimeError):
            _ = mgr.session
