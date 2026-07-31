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
    'press_key': 'browser_press_key',
    'hover': 'browser_hover',
}


class PlaywrightMCPManager:
    """管理 Playwright MCP 服务器子进程和 MCP 客户端会话。"""

    def __init__(
        self,
        browser_type: str = 'chromium',
        headless: bool = True,
        *,
        shared_cdp: bool = False,
    ):
        self.browser_type = browser_type
        self.headless = headless
        self.shared_cdp = bool(shared_cdp) and browser_type == 'chromium'
        self.cdp_url: Optional[str] = None
        self._session: Optional[ClientSession] = None
        self._read = None
        self._write = None
        self._context = None
        self._mcp_config_path: Optional[str] = None
        self._chrome_process = None
        self._chrome_user_data: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _resolve_chrome_exe(self) -> Optional[str]:
        import glob as _glob
        import sys as _sys

        if _sys.platform == 'win32':
            pattern = os.path.expanduser(
                '~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe'
            )
        else:
            pattern = os.path.expanduser(
                '~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome'
            )
        bins = sorted(_glob.glob(pattern))
        return bins[-1] if bins else None

    async def _start_shared_chrome_cdp(self) -> str:
        """Launch Chromium with remote debugging; return http://127.0.0.1:PORT."""
        import asyncio
        import signal
        import sys
        import tempfile

        await self._stop_shared_chrome()
        chrome_exe = self._resolve_chrome_exe()
        if not chrome_exe:
            raise RuntimeError("Chrome binary not found for hybrid CDP")

        user_data_dir = tempfile.mkdtemp(prefix="voyantest_server_cdp_")
        proc_kwargs: dict = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        if sys.platform != "win32":
            proc_kwargs["preexec_fn"] = os.setsid

        chrome_args = [
            chrome_exe,
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-popup-blocking",
            "--disable-extensions",
        ]
        if self.headless:
            chrome_args.append("--headless=new")
        else:
            chrome_args.append("--start-maximized")

        self._chrome_user_data = user_data_dir
        self._chrome_process = await asyncio.create_subprocess_exec(
            *chrome_args, **proc_kwargs
        )
        active_port_file = os.path.join(user_data_dir, "DevToolsActivePort")
        actual_port = None
        for _ in range(40):
            await asyncio.sleep(0.25)
            try:
                with open(active_port_file) as f:
                    actual_port = int(f.readline().strip())
                break
            except (OSError, ValueError):
                continue
        if actual_port is None:
            raise RuntimeError("Server hybrid Chrome did not write DevToolsActivePort")
        if self._chrome_process.returncode is not None:
            raise RuntimeError(
                f"Server hybrid Chrome exited early code={self._chrome_process.returncode}"
            )
        self.cdp_url = f"http://127.0.0.1:{actual_port}"
        logger.info("Server hybrid CDP Chromium ready: %s", self.cdp_url)
        return self.cdp_url

    async def _stop_shared_chrome(self) -> None:
        import asyncio
        import signal
        import shutil
        import sys

        proc = self._chrome_process
        self._chrome_process = None
        self.cdp_url = None
        if proc is not None:
            try:
                if sys.platform != "win32" and proc.pid:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                else:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        ud = self._chrome_user_data
        self._chrome_user_data = None
        if ud:
            try:
                shutil.rmtree(ud, ignore_errors=True)
            except Exception:
                pass

    async def start(self) -> ClientSession:
        """启动 npx @playwright/mcp 子进程，建立 MCP 会话。

        ``shared_cdp=True`` 时先起带 remote-debugging 的 Chromium，再让 MCP
        通过 ``--cdp-endpoint`` 附着，供 hybrid browser-use 同浏览器救场。
        """
        headless_flag = '--headless' if self.headless else ''
        browser_arg = {
            'chromium': '--browser=chromium',
            'firefox': '--browser=firefox',
            'webkit': '--browser=webkit',
        }.get(self.browser_type, '--browser=chromium')

        cdp_endpoint = None
        if self.shared_cdp:
            cdp_endpoint = await self._start_shared_chrome_cdp()
            logger.info(
                "Starting @playwright/mcp attached to CDP %s headless=%s",
                cdp_endpoint, self.headless,
            )
        else:
            logger.info(
                f"Starting @playwright/mcp: {browser_arg} headless={self.headless}"
            )

        import glob as _glob
        import sys as _sys
        _executable_args = []
        if self.browser_type == 'chromium' and not cdp_endpoint:
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
        if cdp_endpoint:
            args.extend(['--cdp-endpoint', cdp_endpoint])
        if headless_flag and not cdp_endpoint:
            args.append(headless_flag)
        elif not self.headless and not cdp_endpoint:
            args.extend(['--viewport-size', '1920x1080'])

        import json as _json
        import tempfile as _tempfile

        _launch_args = ['--disable-popup-blocking']
        if not self.headless:
            _launch_args.append('--start-maximized')
        _cfg = {'browser': {'launchOptions': {'args': _launch_args}}}
        if not self.headless and not cdp_endpoint:
            _cfg['browser']['contextOptions'] = {'viewport': None}
        if cdp_endpoint:
            _cfg['browser']['cdpEndpoint'] = cdp_endpoint
        _cfg_path = os.path.join(
            _tempfile.gettempdir(), f'voyantest-server-mcp-{os.getpid()}.json'
        )
        with open(_cfg_path, 'w', encoding='utf-8') as _f:
            _json.dump(_cfg, _f)
        self._mcp_config_path = _cfg_path
        args.extend(['--config', _cfg_path])

        server_params = StdioServerParameters(
            command='npx',
            args=args,
        )

        self._context = stdio_client(server_params)
        self._read, self._write = await self._context.__aenter__()

        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        await self._session.initialize()

        logger.info("@playwright/mcp session initialized (shared_cdp=%s).", self.shared_cdp)
        return self._session

    async def stop(self) -> None:
        """关闭 MCP 会话和子进程。"""
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - 会话关闭属清理阶段
                logger.warning("Error closing MCP session: %s", exc, exc_info=True)
            self._session = None
        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - stdio context 关闭属清理阶段
                logger.warning("Error closing MCP stdio context: %s", exc, exc_info=True)
            self._context = None
        self._read = None
        self._write = None
        cfg = getattr(self, "_mcp_config_path", None)
        self._mcp_config_path = None
        if cfg:
            try:
                os.remove(cfg)
            except OSError:
                pass
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
        except (RuntimeError, ConnectionError, OSError, AttributeError, TypeError) as exc:
            # MCP 客户端/服务端错误 + 结果结构不符合预期时，统一返回失败 dict
            return {'success': False, 'text': str(exc), 'error': str(exc)}

    async def execute_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Execute a PlaywrightMCPToolCall via MCP.

        Maps LLM action names to MCP tool names and builds the correct arguments.
        After click, auto-focus a newly opened tab (target=_blank / window.open).
        """
        from core.mcp_tabs import (
            list_tab_count,
            should_watch_for_new_tab,
            switch_to_new_tab_if_opened,
        )

        action = tool_call.get('action', '')
        selector = tool_call.get('selector')
        value = tool_call.get('value')
        step_description = tool_call.get('step_description') or tool_call.get('description')

        if action == 'error':
            return {'success': False, 'error': f"LLM error: {value}"}

        from core.blank_click import execute_blank_click, should_use_blank_click

        if should_use_blank_click(
            action=action,
            selector=selector,
            step_description=step_description,
        ):
            try:
                result = await execute_blank_click(self.call_tool)
                if not result.get('success'):
                    return {
                        'success': False,
                        'error': result.get('text') or result.get('error', 'blank click failed'),
                    }
                return {'success': True, 'error': None}
            except (RuntimeError, ConnectionError, OSError, ValueError, TypeError, KeyError) as exc:
                return {'success': False, 'error': str(exc)}

        mcp_tool = ACTION_TOOL_MAP.get(action)
        if not mcp_tool:
            return {'success': False, 'error': f"Unknown action: {action}"}

        try:
            watch_tabs = should_watch_for_new_tab(action)
            count_before = 1
            if watch_tabs:
                try:
                    count_before = await list_tab_count(self.call_tool)
                except Exception as exc:
                    logger.warning("Pre-click tab list failed: %s", exc)

            args = self._build_mcp_args(action, selector, value)
            result = await self.call_tool(mcp_tool, args)

            if not result['success']:
                return {
                    'success': False,
                    'error': result.get('text') or result.get('error', 'MCP call failed'),
                }

            if watch_tabs:
                try:
                    await switch_to_new_tab_if_opened(
                        self.call_tool,
                        count_before=count_before,
                        result_text=result.get('text') or '',
                        settle_seconds=0.6,
                        retries=4,
                        retry_interval=0.5,
                    )
                except Exception as exc:
                    logger.warning("New-tab switch after click failed: %s", exc)

            return {'success': True, 'error': None}
        except (RuntimeError, ConnectionError, OSError, ValueError, TypeError, KeyError) as exc:
            # 参数构建 / MCP 调用 / 字段缺失等失败统一返回结构化错误
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
        elif action == 'press_key':
            return {'key': value or 'Escape'}
        elif action == 'hover':
            return {'element': selector or '', 'target': selector or ''}
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
        except (RuntimeError, ConnectionError, OSError) as exc:
            logger.warning("DOM snapshot failed: %s", exc, exc_info=True)
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
                logger.warning("Failed to clear cookies: %s", result.get('text', result.get('error')))
            return result['success']
        except (RuntimeError, ConnectionError, OSError) as exc:
            logger.warning("Failed to clear cookies: %s", exc, exc_info=True)
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
            logger.warning("Screenshot failed: success=%s, path_exists=%s, error=%s", result.get('success'), os.path.exists(path), result.get('error', result.get('text', 'unknown')))
        except (RuntimeError, ConnectionError, OSError) as exc:
            logger.warning("Screenshot exception for %s: %s", path, exc, exc_info=True)
        return None
