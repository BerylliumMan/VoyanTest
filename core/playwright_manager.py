# core/playwright_manager.py
"""
Playwright MCP 服务器子进程管理器。

通过 npx @playwright/mcp@latest 启动 MCP 服务，使用 MCP Python SDK
的 stdio_client + ClientSession 通信。LLM 生成工具调用，通过 MCP
客户端执行浏览器操作。
"""

import logging
import os
import time
from typing import Any, Optional

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

logger = logging.getLogger(__name__)

SUPPORTED_BROWSERS = {'chromium', 'firefox', 'webkit'}

# MCP 工具名映射（LLM 输出 action → MCP 工具名）
ACTION_TOOL_MAP = {
    'goto': 'browser_navigate',
    'click': 'browser_click',
    'fill': 'browser_type',
    'select': 'browser_select_option',
    'wait': 'browser_wait_for',
    'screenshot': 'browser_take_screenshot',
    'snapshot': 'browser_snapshot',
    'assert_text': 'browser_wait_for',
}


class PlaywrightMCPManager:
    """管理 Playwright MCP 服务器子进程和 MCP 客户端会话。"""

    def __init__(self, browser_type: str = 'chromium', headless: bool = True):
        self.browser_type = browser_type
        self.headless = headless
        self._session: Optional[ClientSession] = None
        self._read = None
        self._write = None
        self._context = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> ClientSession:
        """启动 npx @playwright/mcp 子进程，建立 MCP 会话。"""
        headless_flag = '--headless' if self.headless else ''
        browser_arg = {
            'chromium': '--browser=chromium',
            'firefox': '--browser=firefox',
            'webkit': '--browser=webkit',
        }.get(self.browser_type, '--browser=chromium')

        logger.info(
            f"Starting @playwright/mcp: {browser_arg} headless={self.headless}"
        )

        # 只对 chromium 模式使用预装的 chrome 二进制（避免 firefox/webkit 错用 chrome）
        import glob as _glob
        import sys as _sys
        _executable_args = []
        if self.browser_type == 'chromium':
            if _sys.platform == 'win32':
                _chrome_pattern = os.path.expanduser(
                    '~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe'
                )
            else:
                _chrome_pattern = os.path.expanduser(
                    '~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome'
                )
            _chrome_bins = _glob.glob(_chrome_pattern)
            if _chrome_bins:
                _executable_args = ['--executable-path', _chrome_bins[-1]]

        args = [
            '-y',
            '@playwright/mcp@latest',
            browser_arg,
            '--isolated',
            *_executable_args,
        ]
        if headless_flag:
            args.append(headless_flag)
        else:
            args.extend(['--viewport-size', '1920x1080'])

        server_params = StdioServerParameters(
            command='npx',
            args=args,
        )

        self._context = stdio_client(server_params)
        self._read, self._write = await self._context.__aenter__()

        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        await self._session.initialize()

        logger.info("@playwright/mcp session initialized.")
        return self._session

    async def stop(self) -> None:
        """关闭 MCP 会话和子进程。"""
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(f"Error closing MCP session: {exc}")
            self._session = None
        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(f"Error closing MCP stdio context: {exc}")
            self._context = None
        self._read = None
        self._write = None
        logger.info("@playwright/mcp session closed.")

    async def __aenter__(self) -> "PlaywrightMCPManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    @property
    def session(self) -> ClientSession:
        if not self._session:
            raise RuntimeError("MCP session not initialized. Call start() first.")
        return self._session

    # ------------------------------------------------------------------
    # Tool call executor (via MCP)
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and return structured result."""
        try:
            result = await self.session.call_tool(tool_name, arguments)
            content = result.content if hasattr(result, 'content') else []
            text = ""
            for c in content:
                if hasattr(c, 'text'):
                    text += c.text
            return {'success': not result.isError, 'text': text}
        except Exception as exc:
            return {'success': False, 'text': str(exc), 'error': str(exc)}

    async def execute_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Execute a PlaywrightMCPToolCall via MCP.

        Maps LLM action names to MCP tool names and builds the correct arguments.
        """
        action = tool_call.get('action', '')
        selector = tool_call.get('selector')
        value = tool_call.get('value')

        if action == 'error':
            return {'success': False, 'error': f"LLM error: {value}"}

        mcp_tool = ACTION_TOOL_MAP.get(action)
        if not mcp_tool:
            return {'success': False, 'error': f"Unknown action: {action}"}

        try:
            args = self._build_mcp_args(action, selector, value)
            result = await self.call_tool(mcp_tool, args)

            if not result['success']:
                return {
                    'success': False,
                    'error': result.get('text') or result.get('error', 'MCP call failed'),
                }
            return {'success': True, 'error': None}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    @staticmethod
    def _build_mcp_args(action: str, selector: str | None, value: str | None) -> dict:
        """Build MCP tool arguments from LLM action."""
        if action == 'goto':
            return {'url': value or 'about:blank'}
        elif action == 'click':
            return {'element': selector or '', 'target': selector or ''}
        elif action == 'fill':
            return {'element': selector or '', 'target': selector or '', 'text': value or ''}
        elif action == 'select':
            return {'element': selector or '', 'target': selector or '', 'values': [value] if value else []}
        elif action == 'wait':
            if value and value.isdigit():
                return {'time': int(value)}
            return {'text': value or ''}
        elif action == 'screenshot':
            return {'filename': value or f'screenshot_{int(time.time())}.png', 'fullPage': True, 'type': 'png'}
        elif action == 'snapshot':
            return {}
        elif action == 'assert_text':
            return {'text': value or ''}
        return {}

    # ------------------------------------------------------------------
    # DOM snapshot for LLM context
    # ------------------------------------------------------------------

    async def get_dom_snapshot(self) -> str:
        """Take accessibility snapshot via MCP for LLM context."""
        try:
            result = await self.call_tool('browser_snapshot', {})
            text = result.get('text', '')
            if len(text) > 8000:
                text = text[:8000] + "\n\n[... TRUNCATED]"
            return text or '(empty page)'
        except Exception as exc:
            logger.warning(f"DOM snapshot failed: {exc}")
            return '(page snapshot unavailable)'

    # ------------------------------------------------------------------
    # Cookie management
    # ------------------------------------------------------------------

    async def clear_cookies(self) -> bool:
        """Clear all browser cookies via MCP.

        Returns True if successful, False otherwise.
        """
        try:
            result = await self.call_tool('browser_clear_cookies', {})
            if result['success']:
                logger.info("Browser cookies cleared")
            else:
                logger.warning(f"Failed to clear cookies: {result.get('text', result.get('error'))}")
            return result['success']
        except Exception as exc:
            logger.warning(f"Failed to clear cookies: {exc}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Screenshot (for failures)
    # ------------------------------------------------------------------

    async def take_screenshot(self, path: str) -> Optional[str]:
        """Take a screenshot and save to the given path."""
        try:
            result = await self.call_tool('browser_take_screenshot', {
                'filename': path,
                'fullPage': True,
                'type': 'png',
            })
            if result['success'] and os.path.exists(path):
                return path
            logger.warning(f"Screenshot failed: success={result.get('success')}, path_exists={os.path.exists(path)}, error={result.get('error', result.get('text', 'unknown'))}")
        except Exception as exc:
            logger.warning(f"Screenshot exception for {path}: {exc}")
        return None
