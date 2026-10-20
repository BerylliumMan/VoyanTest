# core/playwright_manager.py
"""
Playwright MCP 服务器子进程管理器。

通过本地已安装的 @playwright/mcp（node cli.js）启动 MCP 服务，使用 MCP
Python SDK 的 stdio_client + ClientSession 通信。LLM 生成工具调用，通过 MCP
客户端执行浏览器操作。

启动策略（离线优先）：
1. ``VT_MCP_CLI`` 环境变量指向 cli.js（最高优先级）；
2. 常见全局 node_modules 根 / 项目内 node_modules；
3. 都找不到时才回退 ``npx -y @playwright/mcp``（需要网络，仅开发机）。
"""

import asyncio
import logging
import os
import time
from typing import Any, Optional

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

logger = logging.getLogger(__name__)

SUPPORTED_BROWSERS = {'chromium', 'firefox', 'webkit'}


def resolve_mcp_cli() -> str:
    """定位本地 @playwright/mcp 的 cli.js（离线部署必需）。

    优先级：``VT_MCP_CLI`` 环境变量 → 常见全局 node_modules 根 →
    项目内 node_modules。找不到返回空串（调用方回退 npx）。
    """
    explicit = os.environ.get("VT_MCP_CLI") or os.environ.get("PLAYWRIGHT_MCP_CLI")
    if explicit and os.path.isfile(explicit):
        return explicit
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [
        "/usr/lib/node_modules",                      # Debian/Ubuntu (nodesource)
        "/usr/local/lib/node_modules",                # macOS / 官方 Node 安装包
        os.path.expanduser("~/.npm-global/lib/node_modules"),
        os.path.expanduser("~/.local/lib/node_modules"),
        os.path.expanduser("~/AppData/Roaming/npm/node_modules"),  # Windows
        os.path.join(here, "node_modules"),           # 项目内安装
        os.path.join(here, "frontend", "node_modules"),
    ]
    for root in roots:
        candidate = os.path.join(root, "@playwright", "mcp", "cli.js")
        if os.path.isfile(candidate):
            return candidate
    return ""


def build_mcp_launch(cli_path: str, mcp_args: list[str]) -> tuple[str, list[str]]:
    """返回 (command, args)：有本地 cli.js 时用 node 直启，否则 npx 回退。"""
    if cli_path:
        node = os.environ.get("VT_NODE_BIN") or "node"
        return node, [cli_path, *mcp_args]
    return "npx", ["-y", "@playwright/mcp", *mcp_args]


# MCP 工具名映射（LLM 输出 action → MCP 工具名）
from core.mcp_args import TOOL_MAP as ACTION_TOOL_MAP


_TEXT_RESULT_ACTIONS = frozenset({
    # B5：读取类动作（结果值在 MCP 文本里，需透传给模型/日志）
    "cookie_list", "cookie_get",
    "localstorage_list", "localstorage_get",
    "sessionstorage_list", "sessionstorage_get",
    "verify_visible", "verify_text", "verify_value", "verify_list",
    "route_list", "route", "unroute", "network_state",
    "storage_state_save", "storage_state_restore",
})


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
            browser_arg,
            '--isolated',
            '--allow-unrestricted-file-access',
            # B1：vision（截图/鼠标系）+ devtools（highlight/视频/追踪）+ pdf
            '--caps=vision,pdf,devtools,network,storage,testing',
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

        cli_path = resolve_mcp_cli()
        command, launch_args = build_mcp_launch(cli_path, args)
        if cli_path:
            logger.info("Using local @playwright/mcp CLI: %s", cli_path)
        else:
            logger.warning(
                "Local @playwright/mcp CLI not found; falling back to npx "
                "(requires network). Set VT_MCP_CLI to cli.js for offline installs."
            )
        server_params = StdioServerParameters(
            command=command,
            args=launch_args,
        )

        self._context = stdio_client(server_params)
        self._read, self._write = await self._context.__aenter__()

        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        try:
            await self._session.initialize()
        except Exception as exc:  # noqa: BLE001 - 启动失败统一转可读错误
            launcher = cli_path or "@playwright/mcp (npx)"
            raise RuntimeError(
                f"@playwright/mcp 启动失败（{launcher}）：{exc}。"
                "离线部署请确认镜像内已安装 @playwright/mcp，"
                "或设置 VT_MCP_CLI 指向其 cli.js。"
            ) from exc

        logger.info("@playwright/mcp session initialized (shared_cdp=%s).", self.shared_cdp)
        return self._session

    async def ping(self) -> bool:
        """会话还能响应就返回 True。死掉的 stdio 不能再复用。"""
        if self._session is None:
            return False
        try:
            await asyncio.wait_for(self._session.list_tools(), timeout=8)
            return True
        except Exception:
            logger.warning("Browser session ping failed", exc_info=True)
            return False

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
        try:
            await self._stop_shared_chrome()
        except Exception as exc:
            logger.warning("Error stopping shared CDP Chrome: %s", exc, exc_info=True)
        logger.info("@playwright/mcp session closed.")

    async def refresh_snapshot_for_hints(
        self,
        hints: list[str],
        *,
        current: str | None = None,
    ) -> str:
        """If label hints missing from snapshot, PageDown a few times and re-snapshot."""
        snap = current if current is not None else await self.get_dom_snapshot()
        if not hints:
            return snap
        joined = snap or ""
        if any(h and h in joined for h in hints):
            return snap
        logger.info(
            "Snapshot missing hints %s — scrolling and re-snapshot",
            [h[:40] for h in hints[:3]],
        )
        for _ in range(3):
            try:
                await self.call_tool('browser_press_key', {'key': 'PageDown'})
            except Exception as exc:
                logger.debug("PageDown for snapshot refresh failed: %s", exc)
                break
            await __import__('asyncio').sleep(0.35)
            snap = await self.get_dom_snapshot()
            if any(h and h in (snap or "") for h in hints):
                return snap
        return snap

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
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments), timeout=45
            )
            content = result.content if hasattr(result, 'content') else []
            text = ""
            for c in content:
                if hasattr(c, 'text'):
                    text += c.text
            return {'success': not result.isError, 'text': text}
        except (RuntimeError, ConnectionError, OSError, AttributeError, TypeError, asyncio.TimeoutError) as exc:
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
        element_desc = tool_call.get('element_desc') or tool_call.get('element')
        step_description = tool_call.get('step_description') or tool_call.get('description')

        # B1：配置门控动作（run_code）在 MCP 派发前拦截
        from core.mcp_args import action_disabled_reason

        _disabled = action_disabled_reason(action)
        if _disabled:
            return {'success': False, 'error': _disabled}

        if action == 'error':
            return {'success': False, 'error': f"LLM error: {value}"}

        # 036：fetch 动作（页面 API 调用）——页面上下文请求 + 分块回传；
        # 证据字节由调用方（bridge/runner）落盘到运行证据目录。
        if str(action).lower() == 'fetch':
            from core.fetch_action import run_fetch_action

            out = await run_fetch_action(self.call_tool, value or "")
            payload: dict = {"api": out.get("api"), "token": out.get("token")}
            if out.get("evidence"):
                payload["evidence"] = out["evidence"]
                payload["filename"] = out.get("filename")
            if out.get("evidence_error"):
                payload["evidence_error"] = out["evidence_error"]
            if not out.get("success"):
                return {
                    'success': False,
                    'error': out.get("error") or "fetch 执行失败",
                    'fetch': payload,
                }
            return {'success': True, 'error': None, 'fetch': payload}

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

        if action in ('upload', 'browser_file_upload') and selector:
            open_res = await self.execute_tool_call({
                'action': 'click',
                'selector': selector,
                'element_desc': element_desc,
            })
            if not open_res.get('success'):
                return open_res

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

            args = self._build_mcp_args(
                action, selector, value, tool_call.get('wait_mode'),
                tool_call.get('role'), tool_call.get('field_type'),
            )
            if element_desc and isinstance(args.get('element'), str) and not args['element']:
                args['element'] = element_desc
            # 上传路径 ASCII 安全化：MCP 对非 ASCII（中文）文件名静默失败（2026-10-16 实测）
            from core.upload_paths import cleanup_upload_temps, prepare_upload_args

            args, _up_temps = prepare_upload_args(args)
            # 025-ref-click: browser_wait_for 只扫主 frame——iframe 文本跨 frame 兜底
            from core.mcp_args import verify_text_cross_frame
            if mcp_tool == 'browser_wait_for' and (args.get('text') or '').strip():
                hit = await verify_text_cross_frame(self.call_tool, args['text'])
                if hit:
                    cleanup_upload_temps(_up_temps)
                    return {'success': True, 'error': None}
            try:
                result = await self.call_tool(mcp_tool, args)
            finally:
                cleanup_upload_temps(_up_temps)
            if not result['success'] and mcp_tool == 'browser_wait_for':
                hit = await verify_text_cross_frame(self.call_tool, args.get('text') or '')
                if hit:
                    return {'success': True, 'error': None}

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

            # B5：读取类动作成功时透传 text（cookie/localstorage/verify/route_list 等
            # 的结果在文本里；此前成功路径一律丢弃 text，导致"读不到值"）
            out = {'success': True, 'error': None}
            if str(action).lower() in _TEXT_RESULT_ACTIONS:
                out['text'] = result.get('text') or ''
            return out
        except (RuntimeError, ConnectionError, OSError, ValueError, TypeError, KeyError) as exc:
            # 参数构建 / MCP 调用 / 字段缺失等失败统一返回结构化错误
            return {'success': False, 'error': str(exc)}

    @staticmethod
    def _build_mcp_args(action: str, selector: str | None, value: str | None,
                        wait_mode: str | None = None,
                        role: str | None = None, field_type: str | None = None) -> dict:
        """兼容包装——实际映射见 core.mcp_args.build_mcp_args（契约 C2）。"""
        from core.mcp_args import legacy_build_mcp_args
        return legacy_build_mcp_args(action, selector, value, wait_mode=wait_mode,
                                     role=role, field_type=field_type)

    # ------------------------------------------------------------------
    # DOM snapshot for LLM context
    # ------------------------------------------------------------------

    async def get_dom_snapshot(self) -> str:
        """Take accessibility snapshot via MCP for LLM context."""
        try:
            result = await self.call_tool('browser_snapshot', {})
            text = result.get('text', '')
            from core.snapshot_compress import compress_snapshot
            snapshot = compress_snapshot(text) or '(empty page)'
            from core.locator_candidates import snapshot_has_visible_overlay
            return (
                f"{snapshot}\n[LOCATOR_STATE] visible_overlay="
                f"{snapshot_has_visible_overlay(snapshot)}"
            )
        except (RuntimeError, ConnectionError, OSError) as exc:
            logger.warning("DOM snapshot failed: %s", exc, exc_info=True)
            return '(page snapshot unavailable)'

    # ------------------------------------------------------------------
    # Cookie management
    # ------------------------------------------------------------------

    async def clear_cookies(self) -> bool:
        """Clear all browser cookies via MCP.

        2026-10-15 修复：正确工具名为 browser_cookie_clear（caps=storage）；
        browser_clear_cookies 在 MCP 中不存在（此前调用一直失败并告警）。
        旧客户端无该工具时回退尝试旧名（静默）。
        """
        try:
            result = await self.call_tool('browser_cookie_clear', {})
            if not result.get('success'):
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

    async def take_screenshot(self, path: str, element: str | None = None,
                              target: str | None = None) -> Optional[str]:
        """Take a screenshot and save to the given path.

        032 US3：element/target 提供时走元素级截图（与 fullPage 互斥）。
        """
        try:
            args: dict = {'filename': path, 'type': 'png'}
            if element or target:
                args['element'] = element or ''
                args['target'] = target or ''
            else:
                args['fullPage'] = True
            result = await self.call_tool('browser_take_screenshot', args)
            if result['success'] and os.path.exists(path):
                return path
            logger.warning("Screenshot failed: success=%s, path_exists=%s, error=%s", result.get('success'), os.path.exists(path), result.get('error', result.get('text', 'unknown')))
        except (RuntimeError, ConnectionError, OSError) as exc:
            logger.warning("Screenshot exception for %s: %s", path, exc, exc_info=True)
        return None
