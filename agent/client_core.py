"""Agent client core — AgentClient class with callback hooks, without GUI/CLI entry point."""

import asyncio
import base64
import json
import logging
import os
import platform
import re
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import websockets

_base_dir = os.path.dirname(os.path.dirname(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable)
project_root = os.path.abspath(_base_dir)
sys.path.insert(0, project_root)

from agent.models import (
    AgentRegistration, WSMessage, WSMessageType,
    StepResultPayload, SnapshotPayload,
)

logger = logging.getLogger("agent.client")

def _resolve_mcp_tool(action: str) -> str:
    """将 action 名解析为 MCP 工具名。"""
    if not action:
        return ""
    # LLM 控制信号，不是浏览器工具
    if action.lower() in ("error", "done"):
        return ""
    if action.startswith('browser_') or action in ('navigate',):
        # 已经是 MCP 工具名——直接使用
        return action if not action.startswith('browser_') else action
    # 通过映射表翻译简短名称
    TOOL_MAP = {
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
    return TOOL_MAP.get(action, action)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target_host: str, target_port: int) -> None:
    """TCP proxy: forward all data from a connected client to the target host:port."""
    try:
        remote_r, remote_w = await asyncio.open_connection(target_host, target_port)
        async def forward(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass
        await asyncio.gather(
            forward(reader, remote_w),
            forward(remote_r, writer),
        )
    except (ConnectionError, OSError) as exc:
        logger.warning("CDP proxy pipe failed: %s", exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass


class AgentClient:
    """WebSocket-based agent. Receives tool calls from server, executes via local MCP."""

    def __init__(self, server_url: str, agent_name: str = None, headless: bool = False,
                 username: str = None, password: str = None,
                 on_status_change: Optional[Callable[[str], None]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None):
        self.server_url = server_url.rstrip('/')
        self.agent_name = agent_name or f"Agent-{uuid.uuid4().hex[:8]}"
        self.agent_id: Optional[str] = self.agent_name
        self.hostname = platform.node()
        self.ip_address = self._local_ip()
        self.running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._headless = headless
        self._username = username
        self._password = password
        self._session_id: Optional[str] = None

        self._mcp_process = None
        self._mcp_stdin = None
        self._mcp_stdout = None
        self._mcp_req_id = 0
        self._bu_browser = None  # shared browser-use session across batch cases

        # Hybrid / shared-CDP execution Chromium (separate from recording)
        self._exec_chrome_process = None
        self._exec_chrome_user_data = None
        self._exec_cdp_http = None  # e.g. http://127.0.0.1:9222

        # CDP Chrome recording state
        self._chrome_process = None
        self._chrome_user_data_dir = None
        self._proxy_server = None
        self._cdp_url = None
        self._is_recording = False

        # Callback hooks
        self._on_status_change = on_status_change
        self._on_log = on_log
        # Active run for fire-and-forget RUN_LOG forwarding to server
        self._active_run_id: Optional[str] = None
        self._active_step_order: Optional[int] = None
        self._active_backend: Optional[str] = None
        self._cancel_requested: bool = False

    # Mid-run: closing the browser must abort the case (do not auto-relaunch).
    BROWSER_CLOSED_USER_MSG = "浏览器已关闭，用例执行已中断"
    BROWSER_CLOSED_SNAPSHOT_MARK = "(browser closed by user)"
    # Playwright MCP snapshots after login can exceed asyncio's default 64KiB line limit.
    MCP_STDOUT_LIMIT = 16 * 1024 * 1024

    # ---- callback helpers ----

    def _emit_status(self, status: str) -> None:
        """发出状态变更通知。调用 logger 并触发 on_status_change 回调。"""
        logger.info("Agent status: %s", status)
        if self._on_status_change:
            try:
                self._on_status_change(status)
            except Exception:
                logger.warning("on_status_change callback failed", exc_info=True)

    def _emit_log(self, level: str, message: str) -> None:
        """发出日志消息。调用 logger、on_log，并在有活跃 run 时回传 RUN_LOG。"""
        log_func = getattr(logger, level, logger.info)
        log_func(message)
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                logger.warning("on_log callback failed", exc_info=True)
        self._schedule_run_log(level, message)

    def _schedule_run_log(self, level: str, message: str) -> None:
        """Fire-and-forget RUN_LOG to server when a run is active."""
        run_id = self._active_run_id
        if not run_id or not self._ws or not message:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(self._forward_run_log(level, message))
        except Exception:
            logger.debug("schedule RUN_LOG failed", exc_info=True)

    async def _forward_run_log(self, level: str, message: str) -> None:
        run_id = self._active_run_id
        if not run_id:
            return
        payload = {
            "level": level,
            "message": message,
            "backend": self._active_backend or "browser_use",
        }
        if self._active_step_order is not None:
            payload["step_order"] = self._active_step_order
        try:
            await self._send(WSMessageType.RUN_LOG, run_id, payload)
        except Exception:
            logger.debug("forward RUN_LOG failed", exc_info=True)

    def _log_info(self, msg: str) -> None:
        self._emit_log("info", msg)

    def _log_debug(self, msg: str) -> None:
        self._emit_log("debug", msg)

    def _log_warning(self, msg: str) -> None:
        self._emit_log("warning", msg)

    def _log_error(self, msg: str) -> None:
        self._emit_log("error", msg)

    # ---- network helpers ----

    @staticmethod
    def _local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as exc:
            logger.warning(f"Failed to detect local IP, using 127.0.0.1: {exc}")
            return "127.0.0.1"

    # ---- auth ----

    async def _login(self):
        self._emit_status('connecting')

        if not self._username or not self._password:
            self._log_info("No credentials provided — connecting without authentication")
            self._emit_status('connected')
            return

        import httpx
        http_url = self.server_url.replace("ws://", "http://").replace("wss://", "https://")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{http_url}/api/auth/login",
                    json={"username": self._username, "password": self._password},
                )
                if resp.status_code != 200:
                    self._log_error(f"Login failed (HTTP {resp.status_code}): {resp.text}")
                    self._emit_status('error')
                    return
                sid = resp.cookies.get("session_id") or (resp.json().get("session_id") if resp.text.startswith("{") else None)
                if isinstance(sid, str) and sid.strip():
                    self._session_id = sid
                    self._log_info(f"Authenticated as {self._username}")
                    self._emit_status('connected')
                else:
                    self._log_warning(f"Login succeeded but no session_id cookie received (got: {sid!r})")
                    self._emit_status('error')
        except Exception as e:
            self._log_warning(f"Login request failed (server may not require auth): {e}")
            self._emit_status('error')

    # ---- lifecycle ----

    async def start(self):
        # Step 1: authenticate
        await self._login()

        ws_url = self.server_url.replace("http://", "ws://").rstrip('/')
        uri = f"{ws_url}/api/agents/ws/{self.agent_name}"

        # Pass session_id via Cookie header (primary) and query param (fallback for websockets lib)
        ws_headers = {}
        if self._session_id:
            ws_headers["Cookie"] = f"session_id={self._session_id}"
            uri += f"?token={self._session_id}"

        logger.info(f"Connecting to {uri} ...")
        self._log_info(f"Connecting to server...")
        self._emit_status('connecting')
        hb_task = None  # 防止 finally 中 UnboundLocalError
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=10,
                                          additional_headers=ws_headers) as ws:
                self._ws = ws
                self.running = True
                await self._send_registration()
                logger.info(f"Connected as {self.agent_name}")
                self._log_info(f"Connected as {self.agent_name}")
                self._log_info(
                    "Agent features: cdp-recover-v2 "
                    "(startup recover; mid-run browser close aborts case)"
                )
                self._emit_status('connected')

                # 定期心跳任务（独立于消息接收，确保执行中也能保持在线）
                async def _periodic_heartbeat():
                    while self.running:
                        await asyncio.sleep(30)
                        try:
                            await self._send_heartbeat()
                        except Exception:
                            break

                hb_task = asyncio.create_task(_periodic_heartbeat())

                while self.running:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=120)
                        logger.debug(f"WS recv: {msg[:200]}...")
                        await self._handle_message(json.loads(msg))
                    except asyncio.TimeoutError:
                        await self._send_heartbeat()
                    except websockets.ConnectionClosed:
                        logger.warning("Connection closed by server")
                        self._log_warning("Connection closed by server")
                        self._emit_status('disconnected')
                        break
                    except Exception as exc:
                        logger.error(f"Message handler error: {exc}", exc_info=True)
                        self._log_error(f"Message handler error: {exc}")
                        self._emit_status('error')
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._log_error(f"Connection failed: {e}")
            self._emit_status('error')
        finally:
            if hb_task is not None:
                hb_task.cancel()
            await self._stop_mcp()
            self.running = False

    async def stop(self):
        self.running = False
        if self._ws:
            await self._ws.close()

    # ---- MCP subprocess management ----

    def _resolve_chrome_exe(self) -> Optional[str]:
        """Locate Chromium/Chrome binary for MCP or shared CDP launch."""
        _pkg_root = (
            os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(__file__))
        )
        _search_roots = [_pkg_root]
        if getattr(sys, "frozen", False):
            _search_roots.append(os.path.dirname(_pkg_root))

        if sys.platform == "win32":
            for _root in _search_roots:
                for _name in ["chromium/chrome-win64/chrome.exe", "chrome-win64/chrome.exe"]:
                    _candidate = os.path.join(_root, _name)
                    if os.path.isfile(_candidate):
                        return _candidate
            for _p in [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]:
                if os.path.isfile(_p):
                    return _p

        playwright_browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
        if not playwright_browsers.is_dir():
            playwright_browsers = Path.home() / ".cache" / "ms-playwright"
        if not playwright_browsers.is_dir():
            playwright_browsers = Path.home() / "AppData" / "Local" / "ms-playwright"
        for _pat in ["chrome-linux64/chrome", "chrome-linux/chrome", "chrome-win64/chrome.exe"]:
            _cd = (
                sorted(playwright_browsers.glob(f"chromium-*/{_pat}"))
                if playwright_browsers.is_dir()
                else []
            )
            if _cd:
                return str(_cd[-1])

        for _p in [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/snap/bin/chromium",
        ]:
            if os.path.isfile(_p):
                return _p
        return None

    async def _start_exec_chrome_cdp(self) -> str:
        """Launch Chromium with remote debugging for hybrid MCP+browser-use.

        Returns HTTP CDP endpoint ``http://127.0.0.1:PORT``.
        """
        await self._stop_exec_chrome()
        _chrome_exe = self._resolve_chrome_exe()
        if not _chrome_exe:
            raise RuntimeError("Chrome binary not found for hybrid CDP")

        import tempfile

        user_data_dir = tempfile.mkdtemp(prefix="voyan_exec_cdp_")
        proc_kwargs: dict = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        if sys.platform != "win32":
            proc_kwargs["preexec_fn"] = os.setsid

        chrome_args = [
            _chrome_exe,
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-popup-blocking",
            "--disable-features=ChromeWhatsNewUI,ChromeWhatsNew",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
        ]
        if self._headless:
            chrome_args.append("--headless=new")
        else:
            chrome_args.append("--start-maximized")

        self._exec_chrome_user_data = user_data_dir
        self._exec_chrome_process = await asyncio.create_subprocess_exec(
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
            raise RuntimeError("Exec Chrome did not write DevToolsActivePort in time")
        if self._exec_chrome_process.returncode is not None:
            raise RuntimeError(
                f"Exec Chrome exited early code={self._exec_chrome_process.returncode}"
            )

        self._exec_cdp_http = f"http://127.0.0.1:{actual_port}"
        self._log_info(f"Hybrid CDP Chromium ready: {self._exec_cdp_http}")
        return self._exec_cdp_http

    async def _stop_exec_chrome(self) -> None:
        proc = self._exec_chrome_process
        self._exec_chrome_process = None
        self._exec_cdp_http = None
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
        ud = self._exec_chrome_user_data
        self._exec_chrome_user_data = None
        if ud:
            try:
                import shutil

                shutil.rmtree(ud, ignore_errors=True)
            except Exception:
                pass

    async def _is_exec_cdp_alive(self) -> bool:
        """Probe hybrid Chromium CDP; False if process exited or port refused."""
        http = self._exec_cdp_http
        if not http:
            return False
        proc = self._exec_chrome_process
        if proc is not None and proc.returncode is not None:
            self._log_warning(
                f"Hybrid Chromium already exited code={proc.returncode}"
            )
            self._exec_cdp_http = None
            return False

        # Parse host/port for a cheap TCP probe (works when HTTP hangs oddly on Windows)
        host, port = "127.0.0.1", None
        try:
            from urllib.parse import urlparse

            parsed = urlparse(http)
            host = parsed.hostname or "127.0.0.1"
            port = int(parsed.port or 0) or None
        except Exception:
            port = None

        def _probe() -> bool:
            import socket
            import urllib.error
            import urllib.request

            if port:
                try:
                    with socket.create_connection((host, port), timeout=1.5):
                        pass
                except OSError:
                    return False
            try:
                with urllib.request.urlopen(
                    f"{http.rstrip('/')}/json/version", timeout=2
                ) as resp:
                    return 200 <= getattr(resp, "status", 200) < 300
            except (urllib.error.URLError, TimeoutError, OSError):
                return False

        try:
            ok = await asyncio.to_thread(_probe)
        except Exception:
            ok = False
        if not ok:
            # Stale URL after user closed the window — clear so next check is honest
            self._exec_cdp_http = None
        return ok

    @staticmethod
    def _looks_like_dead_browser(err_text: str) -> bool:
        low = (err_text or "").lower()
        keys = (
            "econnrefused",
            "websocket url",
            "initializeserver",
            "target closed",
            "browser has been closed",
            "browser closed",
            "connect failed",
            "connection refused",
            "net::err_",
            "cdp",
            "chromium has crashed",
            "mcp tool call timed out",
            "mcp stdout closed",
            "mcp empty response",
            "no response",
            "page not available",
            "snapshot timeout",
            "snapshot unavailable",
            "browser closed by user",
        )
        return any(k in low for k in keys)

    def _mcp_process_alive(self) -> bool:
        proc = self._mcp_process
        if proc is None:
            return False
        return proc.returncode is None

    async def _invalidate_mcp_session(self, why: str = "") -> None:
        """Drop a dead/desynced MCP session so the next call starts fresh.

        Important: do **not** kill the hybrid shared Chromium here. Login and
        other full-page navigations often briefly desync MCP; closing Chrome
        would wipe the session and leave about:blank for the next snapshot.
        """
        if why:
            self._log_warning(f"Invalidating MCP session: {why}")
        try:
            await self._stop_mcp(close_exec_chrome=False)
        except Exception as exc:
            self._log_warning(f"MCP invalidate stop failed: {exc}")

    async def _ensure_mcp_session(self, *, shared_cdp: bool) -> None:
        """Start MCP or restart if the reused browser/CDP session is dead."""
        if not self._mcp_process_alive():
            if self._mcp_process is not None:
                self._log_warning(
                    f"MCP process already exited code={self._mcp_process.returncode} — restarting"
                )
                await self._invalidate_mcp_session("process exited")
            await self._start_mcp(shared_cdp=shared_cdp)
            return

        self._log_info("Reusing existing MCP subprocess (browser stays open)")
        need_restart = False
        reason = ""

        # Switching into hybrid (or Chrome closed) while MCP still running
        if shared_cdp:
            if not await self._is_exec_cdp_alive():
                need_restart = True
                reason = "hybrid CDP browser closed or unreachable"
        elif self._exec_chrome_process is not None:
            # Leftover hybrid Chrome state while now on plain MCP
            if self._exec_chrome_process.returncode is not None:
                self._exec_cdp_http = None

        if not need_restart:
            try:
                snap = await asyncio.wait_for(
                    self._mcp_call_tool("snapshot", "", ""), timeout=8
                )
                snap_err = str(snap.get("error") or snap.get("text") or "")
                # Empty / unavailable snapshots after user closed the window
                bad_text = (
                    not snap.get("success")
                    or self._looks_like_dead_browser(snap_err)
                    or "(page not available)" in snap_err
                    or "(snapshot" in snap_err
                )
                if bad_text:
                    need_restart = True
                    reason = (
                        "MCP snapshot failed: "
                        + (snap_err or "empty/unavailable")[:160]
                    )
            except Exception as exc:
                need_restart = True
                reason = f"MCP not responding: {exc}"

        if need_restart:
            self._log_warning(f"Existing browser session unusable ({reason}) — restarting")
            await self._invalidate_mcp_session(reason)
            await self._start_mcp(shared_cdp=shared_cdp)

    async def _restart_mcp_for_dead_browser(self, *, shared_cdp: bool, why: str) -> None:
        self._log_warning(f"{why} — restarting MCP+browser")
        await self._invalidate_mcp_session(why)
        await self._start_mcp(shared_cdp=shared_cdp)

    def _run_in_progress(self) -> bool:
        return bool(self._active_run_id)

    async def _abort_run_for_closed_browser(self, why: str) -> str:
        """Stop MCP without relaunching; return the user-facing abort error."""
        self._log_warning(f"{why} — aborting run (browser closed by user)")
        try:
            await self._invalidate_mcp_session(why)
        except Exception as exc:
            self._log_warning(f"MCP cleanup after browser close failed: {exc}")
        return self.BROWSER_CLOSED_USER_MSG

    @staticmethod
    def _looks_like_mcp_protocol_glitch(err_text: str) -> bool:
        """True for oversized MCP JSON lines / pipe parse errors (browser still OK)."""
        low = (err_text or "").lower()
        return any(
            k in low
            for k in (
                "chunk is longer than limit",
                "separator is found",
                "limit overran",
                "line too long",
            )
        )

    async def _recover_mcp_keeping_chrome(self, why: str) -> bool:
        """Restart MCP only when hybrid CDP Chromium is still alive. Return True if ready."""
        shared = bool(self._exec_cdp_http)
        cdp_ok = shared and await self._is_exec_cdp_alive()
        if not cdp_ok and shared:
            self._log_warning(f"{why} — hybrid CDP also dead; cannot recover MCP alone")
            return False
        if not shared and not cdp_ok:
            # Plain MCP owns the browser — a dead process usually means browser gone
            return False
        self._log_warning(f"{why} — restarting MCP (shared Chromium kept={cdp_ok or shared})")
        try:
            if self._mcp_process_alive() or self._mcp_process is not None:
                await self._invalidate_mcp_session(why)
            await self._start_mcp(shared_cdp=bool(shared or cdp_ok))
            return self._mcp_process_alive()
        except Exception as exc:
            self._log_warning(f"MCP recover failed: {exc}")
            return False

    async def _start_mcp(self, *, shared_cdp: bool = False):
        # 清理可能残留的旧 MCP；hybrid 共用 Chromium 时不要 pkill chrome
        try:
            import subprocess as _sp

            patterns = ["playwright"] if shared_cdp else ["playwright", "chrome", "chromium"]
            for _patt in patterns:
                _out = _sp.run(["pkill", "-f", _patt], capture_output=True, timeout=3)
                if _out.returncode == 0:
                    self._log_info(f"Cleaned up stale {_patt} processes")
        except Exception:
            pass
        if self._mcp_process:
            self._log_info("Stopping previous MCP before starting new one")
            # Keep hybrid Chromium across MCP restarts
            await self._stop_mcp(close_exec_chrome=False)

        if not shared_cdp:
            # Plain MCP launches its own browser; drop leftover hybrid Chrome
            await self._stop_exec_chrome()

        cdp_endpoint = None
        if shared_cdp:
            if self._exec_cdp_http and await self._is_exec_cdp_alive():
                cdp_endpoint = self._exec_cdp_http
                self._log_info(f"Reusing hybrid CDP Chromium: {cdp_endpoint}")
            else:
                cdp_endpoint = await self._start_exec_chrome_cdp()
            self._log_info(
                f"Starting MCP attached to shared CDP headless={self._headless}"
            )
        else:
            self._log_info(f"Starting MCP: chromium headless={self._headless}")

        # 确定包根目录（exe 同级）
        _pkg_root = (
            os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(__file__))
        )
        _search_roots = [_pkg_root]
        if getattr(sys, "frozen", False):
            _search_roots.append(os.path.dirname(_pkg_root))

        _node_exe = os.path.join(_pkg_root, "node.exe")
        if not os.path.isfile(_node_exe):
            _node_exe = "node"

        _cli_js = ""
        for _root in _search_roots:
            _candidate = os.path.join(
                _root, "node_modules", "@playwright", "mcp", "cli.js"
            )
            if os.path.isfile(_candidate):
                _cli_js = _candidate
                break
        if not _cli_js or not os.path.isfile(_cli_js):
            raise RuntimeError(
                f"@playwright/mcp CLI not found. "
                f"Searched in: {[os.path.join(r, 'node_modules', '@playwright', 'mcp', 'cli.js') for r in _search_roots]}"
            )

        args = [_node_exe, _cli_js, "--browser=chromium"]

        if cdp_endpoint:
            args.extend(["--cdp-endpoint", cdp_endpoint])
        else:
            _chrome_exe = self._resolve_chrome_exe()
            if _chrome_exe and os.path.isfile(_chrome_exe):
                args.extend(["--executable-path", _chrome_exe])
                self._log_info(f"Using Chromium: {_chrome_exe}")

            import json as _json
            import tempfile as _tempfile

            # Always pass launchOptions so target=_blank / window.open are not
            # blocked by Chromium's popup blocker under automation clicks.
            _launch_args = ["--disable-popup-blocking"]
            _cfg: dict = {
                "browser": {
                    "launchOptions": {"args": _launch_args},
                }
            }
            if self._headless:
                args.append("--headless")
                args.extend(["--viewport-size", "1920x1080"])
            else:
                _launch_args.append("--start-maximized")
                _cfg["browser"]["contextOptions"] = {"viewport": None}
            _cfg_path = os.path.join(
                _tempfile.gettempdir(), f"voyantest-mcp-{os.getpid()}.json"
            )
            with open(_cfg_path, "w", encoding="utf-8") as _f:
                _json.dump(_cfg, _f)
            args.extend(["--config", _cfg_path])
            self._mcp_config_path = _cfg_path
            args.append("--isolated")

        import subprocess as _sp

        proc_kwargs = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.MCP_STDOUT_LIMIT,
        )
        if sys.platform == "win32":
            proc_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW
        else:
            proc_kwargs["preexec_fn"] = os.setsid
        self._mcp_process = await asyncio.create_subprocess_exec(*args, **proc_kwargs)
        self._mcp_stdin = self._mcp_process.stdin
        self._mcp_stdout = self._mcp_process.stdout
        asyncio.create_task(self._pipe_stderr())

        await self._mcp_send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agent-client", "version": "1.0"},
        })
        init_resp = await self._mcp_recv(timeout=60)
        if not init_resp:
            # Try to capture stderr for diagnosis
            stderr_text = ""
            try:
                if self._mcp_process.stderr:
                    stderr_text = (await asyncio.wait_for(self._mcp_process.stderr.read(), timeout=2)).decode(errors='replace')
            except Exception:
                pass
            logger.error(f"MCP initialize failed — no response. stderr: {stderr_text[:500]}")
            self._log_error(f"MCP initialize failed: {stderr_text[:200]}")
            raise RuntimeError("MCP subprocess failed to initialize")
        self._log_info("MCP initialized")

        await self._mcp_notify("notifications/initialized")
        self._log_info("MCP subprocess ready (browser started)")

    async def _stop_mcp(self, *, close_exec_chrome: bool = True):
        if self._mcp_process:
            pid = self._mcp_process.pid
            # 1. Close stdin → MCP 检测到 EOF 后优雅退出，Playwright 自动关闭浏览器
            if self._mcp_stdin:
                try:
                    self._mcp_stdin.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._mcp_process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    if sys.platform == 'win32':
                        self._mcp_process.terminate()
                    else:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGTERM)
                    await asyncio.wait_for(self._mcp_process.wait(), timeout=3)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        if sys.platform == 'win32':
                            self._mcp_process.kill()
                            import subprocess as _sp
                            _sp.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                                    capture_output=True, timeout=5)
                        else:
                            pgid = os.getpgid(pid)
                            os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        try:
                            self._mcp_process.kill()
                        except Exception:
                            logger.debug("Process kill via .kill() also failed, giving up")
            except Exception as exc:
                logger.warning(f"Failed to stop MCP subprocess cleanly: {exc}")
            self._mcp_process = None
            self._mcp_stdin = None
            self._mcp_stdout = None
            self._log_info(
                "MCP subprocess stopped"
                + (" (shared Chromium kept)" if not close_exec_chrome else " (browser closed)")
            )
        if close_exec_chrome:
            await self._stop_exec_chrome()

    async def _pipe_stderr(self):
        try:
            stderr = self._mcp_process.stderr
            if stderr is None:
                logger.debug("MCP stderr not available")
                return
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors='replace').rstrip()
                if text:
                    logger.debug(f"[MCP] {text}")
        except Exception:
            logger.debug("MCP stderr pipe closed")

    async def _mcp_send(self, method: str, params: dict = None):
        self._mcp_req_id += 1
        req_id = self._mcp_req_id
        req = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            req["params"] = params
        self._mcp_stdin.write((json.dumps(req) + "\n").encode())
        await self._mcp_stdin.drain()
        return req_id

    async def _mcp_notify(self, method: str):
        msg = {"jsonrpc": "2.0", "method": method}
        self._mcp_stdin.write((json.dumps(msg) + "\n").encode())
        await self._mcp_stdin.drain()

    async def _mcp_recv(self, timeout: float = 120) -> dict:
        while True:
            line = await asyncio.wait_for(self._mcp_stdout.readline(), timeout=timeout)
            if not line:
                code = self._mcp_process.returncode
                logger.error(f"MCP stdout closed (returncode={code})")
                return {}
            text = line.decode(errors='replace').strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                logger.warning(f"MCP non-JSON stdout: {text[:200]}")
                continue

    async def _mcp_call_tool(
        self,
        action: str,
        selector: str,
        value: str,
        step_description: str = "",
    ) -> dict:
        from core.blank_click import execute_blank_click, should_use_blank_click
        from core.mcp_tabs import (
            list_tab_count,
            should_watch_for_new_tab,
            switch_to_new_tab_if_opened,
        )

        if should_use_blank_click(
            action=action,
            selector=selector,
            step_description=step_description,
        ):
            return await execute_blank_click(self._mcp_tools_call)

        mcp_tool = _resolve_mcp_tool(action)
        if not mcp_tool:
            return {"success": False, "error": f"Unknown action: {action}"}

        watch_tabs = should_watch_for_new_tab(action)
        count_before = 1
        if watch_tabs:
            try:
                count_before = await list_tab_count(self._mcp_tools_call)
            except Exception as exc:
                logger.warning("Pre-click tab list failed: %s", exc)

        args = self._build_mcp_args(action, selector, value)
        result = await self._mcp_tools_call(mcp_tool, args)

        if watch_tabs and result.get("success"):
            try:
                switched = await switch_to_new_tab_if_opened(
                    self._mcp_tools_call,
                    count_before=count_before,
                    result_text=result.get("text") or "",
                    settle_seconds=0.6,
                    retries=4,
                    retry_interval=0.5,
                )
                if switched:
                    self._log_info("Focused newly opened browser tab after click")
            except Exception as exc:
                logger.warning("New-tab switch after click failed: %s", exc)

        return result

    async def _mcp_tools_call(self, tool_name: str, arguments: dict) -> dict:
        """Call an MCP tool by exact name (e.g. browser_tabs)."""
        return await self._mcp_tools_call_once(tool_name, arguments, _retried=False)

    async def _mcp_tools_call_once(
        self, tool_name: str, arguments: dict, *, _retried: bool,
    ) -> dict:
        if not self._mcp_process_alive():
            return {
                "success": False,
                "error": "MCP stdout closed / process dead (browser closed by user?)",
            }
        if not self._mcp_stdin or not self._mcp_stdout:
            return {"success": False, "error": "MCP session pipes not ready"}
        try:
            await self._mcp_send(
                "tools/call", {"name": tool_name, "arguments": arguments or {}},
            )
        except Exception as exc:
            await self._invalidate_mcp_session(f"MCP stdin write failed: {exc}")
            return {"success": False, "error": f"MCP write failed: {exc}"}
        try:
            # Keep timeout modest so a closed browser does not freeze the WS loop
            resp = await self._mcp_recv(timeout=45)
            if not resp:
                await self._invalidate_mcp_session("MCP empty response / stdout closed")
                return {
                    "success": False,
                    "error": "MCP empty response (browser closed by user?)",
                }
            # Notifications / unrelated messages: require matching id when present
            result = resp.get("result")
            if result is None and resp.get("error"):
                err = resp.get("error")
                err_text = (
                    err.get("message") if isinstance(err, dict) else str(err)
                )
                if self._looks_like_dead_browser(str(err_text)):
                    await self._invalidate_mcp_session(str(err_text))
                return {"success": False, "error": str(err_text)}
            if not isinstance(result, dict):
                await self._invalidate_mcp_session("MCP malformed tools/call result")
                return {"success": False, "error": "MCP empty response"}
            is_error = result.get("isError", False)
            content = result.get("content", [])
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            out = {"success": not is_error, "text": text, "_content": content}
            if (not out["success"]) and self._looks_like_dead_browser(text):
                await self._invalidate_mcp_session(text[:160])
            return out
        except asyncio.TimeoutError:
            await self._invalidate_mcp_session("MCP tool call timed out")
            return {"success": False, "error": "MCP tool call timed out"}
        except Exception as exc:
            err = str(exc)
            await self._invalidate_mcp_session(f"MCP recv failed: {exc}")
            # Oversized snapshot line: recover MCP on live CDP and retry once
            if (
                not _retried
                and self._looks_like_mcp_protocol_glitch(err)
                and await self._recover_mcp_keeping_chrome(
                    f"MCP protocol glitch on {tool_name}: {err[:120]}"
                )
            ):
                return await self._mcp_tools_call_once(
                    tool_name, arguments, _retried=True,
                )
            return {"success": False, "error": f"MCP recv failed: {exc}"}

    async def _mcp_screenshot_base64(self) -> Optional[str]:
        """Take a screenshot via MCP and return base64-encoded PNG.

        截图仅用于回传服务端落盘；客户端临时文件会尽量清理。
        """
        tmp_name = f"_fail_{int(time.time() * 1000)}.png"
        tmp_paths: list[Path] = []
        try:
            result = await self._mcp_call_tool("screenshot", "", tmp_name)
            content = result.get("_content", []) or []
            # 记录 MCP 可能落盘的路径，便于回传后清理
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "text":
                    continue
                text = c.get("text", "")
                for prefix in ("Screenshot saved to:", "Saved to:"):
                    if prefix in text:
                        path = text.split(prefix)[-1].strip().split("\n")[0].strip()
                        if path:
                            tmp_paths.append(Path(path))
                m = re.search(r'\[Screenshot of full page\]\(([^)]+)\)', text)
                if m:
                    tmp_paths.append(Path(m.group(1)))
            # 也尝试 cwd 下的临时文件名
            tmp_paths.append(Path(tmp_name))

            b64 = self._extract_screenshot_base64(content)
            if b64:
                return b64
            if not result.get("success"):
                logger.warning(
                    "MCP screenshot failed without image payload: %s",
                    (result.get("error") or result.get("text") or "")[:120],
                )
            return None
        except Exception as exc:
            logger.warning(f"MCP screenshot failed: {exc}")
            return None
        finally:
            for p in tmp_paths:
                try:
                    if p.exists() and p.is_file():
                        p.unlink()
                except OSError:
                    pass

    @staticmethod
    def _extract_screenshot_base64(content: list) -> Optional[str]:
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "image":
                data = c.get("data", "")
                if data and len(data) > 100:
                    return data
            if t == "resource":
                res = c.get("resource", {})
                for key in ("blob", "text"):
                    data = res.get(key)
                    if data:
                        return data if isinstance(data, str) and len(data) > 100 else None
            if t == "text":
                text = c.get("text", "")
                # 格式1: "Screenshot saved to: <path>"
                for prefix in ("Screenshot saved to:", "Saved to:"):
                    if prefix in text:
                        path = text.split(prefix)[-1].strip().split("\n")[0].strip()
                        p = Path(path)
                        if p.exists():
                            return base64.b64encode(p.read_bytes()).decode("utf-8")
                # 格式2: "- [Screenshot of full page](<path>)"
                m = re.search(r'\[Screenshot of full page\]\(([^)]+)\)', text)
                if m:
                    p = Path(m.group(1))
                    if p.exists():
                        return base64.b64encode(p.read_bytes()).decode("utf-8")
        return None

    @staticmethod
    def _build_mcp_args(action: str, selector: str, value: str) -> dict:
        if action in ('goto', 'navigate', 'browser_navigate'):
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
        elif action in ('press_key', 'browser_press_key'):
            return {'key': value or 'Escape'}
        elif action in ('hover', 'browser_hover'):
            return {'element': selector or '', 'target': selector or ''}
        return {}

    # ---- messaging ----

    async def _send(self, msg_type: WSMessageType, run_id: str = None, payload: dict = None):
        if not self._ws:
            return
        msg = WSMessage(
            type=msg_type,
            agent_id=self.agent_id or "",
            run_id=run_id,
            payload=payload or {},
        )
        await self._ws.send(msg.model_dump_json())

    async def _send_registration(self):
        caps = ["mcp", "playwright", "ui_testing", "local_browser"]
        try:
            import browser_use  # noqa: F401
            caps.append("browser_use")
        except ImportError:
            pass
        reg = AgentRegistration(
            name=self.agent_name,
            hostname=self.hostname,
            ip_address=self.ip_address,
            capabilities=caps,
        )
        await self._send(WSMessageType.REGISTERED, payload=reg.model_dump())

    async def send_result(self, run_id: str, result: dict) -> None:
        """发送 observe/act 执行结果回 Server。"""
        await self._send(WSMessageType.STEP_RESULT, run_id, result)

    async def _send_heartbeat(self):
        await self._send(WSMessageType.HEARTBEAT)

    # ---- message handler ----

    async def _handle_message(self, raw: dict):
        try:
            msg = WSMessage(**raw)
            logger.debug(f"Received message type={msg.type} run_id={msg.run_id}")
        except Exception:
            logger.warning(f"Invalid message: {raw}")
            return

        if msg.type == WSMessageType.RUN_START:
            payload = msg.payload or {}
            backend = (payload.get("backend") or "playwright_mcp").strip()
            if backend == "browser_use":
                await self._handle_run_start_browser_use(msg)
                return

            shared_cdp = backend == "hybrid"
            self._log_info(
                f"Run {msg.run_id} started — launching browser backend={backend} shared_cdp={shared_cdp}"
            )
            self._active_run_id = msg.run_id
            self._active_backend = backend
            self._cancel_requested = False
            self._emit_status('busy')
            ready_ok = False
            ready_err = ""
            try:
                await self._ensure_mcp_session(shared_cdp=shared_cdp)

                # BASE URL 由 Playwright MCP 直接导航，不经 LLM（批量后续用例可跳过）
                navigate = payload.get("navigate_base_url", True)
                base_url = (payload.get("base_url") or "").strip()
                if navigate and base_url:
                    self._log_info(f"Playwright navigating to BASE URL: {base_url}")
                    nav = await self._mcp_call_tool("goto", "", base_url)
                    if not nav.get("success"):
                        err_text = str(nav.get("error") or nav.get("text") or "unknown")
                        # 浏览器被手动关闭时，snapshot 偶发仍“成功”，goto 才暴露 ECONNREFUSED
                        if shared_cdp and (
                            self._looks_like_dead_browser(err_text)
                            or not await self._is_exec_cdp_alive()
                        ):
                            await self._restart_mcp_for_dead_browser(
                                shared_cdp=True,
                                why="BASE URL navigation hit dead CDP/browser",
                            )
                            nav = await self._mcp_call_tool("goto", "", base_url)
                            err_text = str(nav.get("error") or nav.get("text") or "unknown")
                        elif (not shared_cdp) and self._looks_like_dead_browser(err_text):
                            await self._restart_mcp_for_dead_browser(
                                shared_cdp=False,
                                why="BASE URL navigation hit dead browser",
                            )
                            nav = await self._mcp_call_tool("goto", "", base_url)
                            err_text = str(nav.get("error") or nav.get("text") or "unknown")
                        if not nav.get("success"):
                            self._log_warning(f"BASE URL navigation failed: {err_text}")
                            ready_err = err_text
                        else:
                            ready_ok = True
                    else:
                        ready_ok = True
                elif not navigate:
                    self._log_info("Skip BASE URL navigation (batch follow-up, keep session)")
                    ready_ok = True
                else:
                    ready_ok = True
            except Exception as e:
                ready_err = str(e)
                self._log_error(f"Failed to start MCP for run {msg.run_id}: {e}")
                self._emit_status('error')
                self._active_run_id = None
                self._active_backend = None
                await self._send(
                    WSMessageType.ERROR, msg.run_id,
                    {"message": f"MCP start failed: {e}", "ready": False},
                )
            else:
                # Single ACK so server.request(RUN_START) unblocks exactly once
                await self._send(
                    WSMessageType.SNAPSHOT_RESULT,
                    msg.run_id,
                    {
                        "text": (
                            "(browser ready)" if ready_ok
                            else f"(browser not ready: {ready_err})"
                        ),
                        "ready": ready_ok,
                        "error": ready_err or None,
                    },
                )
                if not ready_ok:
                    self._emit_status('error')
                    self._active_run_id = None
                    self._active_backend = None
                    self._log_warning(
                        f"Browser not ready for run {msg.run_id}: {ready_err or 'unknown'}"
                    )

        elif msg.type == WSMessageType.RUN_END:
            self._log_info(f"Run {msg.run_id} ended — keep browser session for next case")
            try:
                if self._mcp_process:
                    await self._mcp_call_tool("snapshot", "", "")
            except Exception as e:
                self._log_warning(f"Error during run end: {e}")
            self._active_run_id = None
            self._active_step_order = None
            self._active_backend = None
            self._emit_status('idle')

        elif msg.type == WSMessageType.GET_SNAPSHOT:
            await self._handle_get_snapshot(msg.run_id)

        elif msg.type == WSMessageType.GET_SCREENSHOT:
            await self._handle_get_screenshot(msg.run_id)

        elif msg.type == WSMessageType.STEP_EXECUTE:
            p = msg.payload or {}
            tc = p.get("tool_call") if isinstance(p.get("tool_call"), dict) else {}
            action = (tc.get("action") or p.get("action") or "").strip()
            if action == "observe":
                await self._handle_observe(msg)
            elif "tool_call" in p and "step_order" in p:
                # 逐步执行路径：含 tool_call + step_order
                await self._handle_step_execute(msg)
            else:
                # AgentBridge act：action/selector/value 在 payload 顶层
                await self._handle_act(msg)

        elif msg.type == WSMessageType.STEP_BROWSER_USE:
            await self._handle_step_browser_use(msg)

        elif msg.type == WSMessageType.SHUTDOWN:
            self._log_info("Shutdown signal received — closing browser")
            try:
                await self._stop_mcp()
            except Exception as e:
                self._log_error(f"Error shutting down MCP: {e}")
            try:
                await self._stop_bu_browser()
            except Exception as e:
                self._log_error(f"Error shutting down browser-use: {e}")

        elif msg.type == WSMessageType.CANCEL_RUN:
            self._log_info(f"Cancel run signal received for {msg.run_id}")
            self._cancel_requested = True
            # Best-effort: abort in-flight act; keep browser for subsequent cases
            try:
                if self._active_run_id and (
                    not msg.run_id or msg.run_id == self._active_run_id
                ):
                    await self._send(
                        WSMessageType.RUN_COMPLETE,
                        msg.run_id or self._active_run_id,
                        {
                            "status": "cancelled",
                            "error": "用户停止执行",
                            "steps": [],
                        },
                    )
                    self._active_run_id = None
                    self._emit_status("idle")
            except Exception as e:
                self._log_warning(f"Cancel run cleanup: {e}")

        elif msg.type == WSMessageType.HEARTBEAT:
            pass

        elif msg.type == WSMessageType.RECORDING_START:
            await self._handle_recording_start(msg)

        elif msg.type == WSMessageType.RECORDING_STOP:
            await self._handle_recording_stop(msg)

    async def _stop_bu_browser(self) -> None:
        browser = self._bu_browser
        self._bu_browser = None
        if browser is None:
            return
        try:
            if hasattr(browser, "stop"):
                await browser.stop()
            elif hasattr(browser, "close"):
                await browser.close()
        except Exception as exc:
            self._log_warning(f"browser-use session stop: {exc}")

    async def _handle_step_browser_use(self, msg: WSMessage):
        """Hybrid fallback: run one NL step via browser-use on the shared CDP browser."""
        payload = msg.payload or {}
        step_order = payload.get("step_order") or 0
        desc = payload.get("description") or ""
        expected = payload.get("expected_result")
        max_steps = int(payload.get("max_steps_per_nl") or 20)
        t0 = time.monotonic()
        prev_run = self._active_run_id
        prev_step = self._active_step_order
        prev_backend = self._active_backend
        self._active_run_id = msg.run_id
        self._active_step_order = int(step_order) if step_order else None
        self._active_backend = "browser_use_fallback"
        self._log_info(
            f"Hybrid fallback step {step_order} via browser-use on CDP={self._exec_cdp_http}"
        )
        try:
            if not self._exec_cdp_http:
                raise RuntimeError("无共享 CDP（请用 hybrid 后端启动 MCP）")

            from core.browser_use_exec import (
                create_browser_use_llm_from_config,
                execute_nl_steps_browser_use,
            )

            llm_cfg = payload.get("llm") or {}
            api_key = (llm_cfg.get("api_key") or "").strip()
            if not api_key:
                raise RuntimeError("服务端未下发 LLM api_key，无法 browser-use 救场")

            llm = create_browser_use_llm_from_config(
                api_key=api_key,
                api_base=llm_cfg.get("api_base"),
                model=llm_cfg.get("model"),
            )

            def _progress(line: str) -> None:
                self._emit_log("info", line)

            # Attach to existing Chromium; never stop it
            results = await execute_nl_steps_browser_use(
                [
                    {
                        "step_order": step_order,
                        "description": desc,
                        "expected_result": expected,
                    }
                ],
                llm=llm,
                base_url=None,
                headless=self._headless,
                max_steps_per_nl=max_steps,
                stop_browser=False,
                cdp_url=self._exec_cdp_http,
                on_progress=_progress,
            )
            r = results[0] if results else {"success": False, "error": "empty result"}
            await self._send(
                WSMessageType.STEP_RESULT,
                msg.run_id,
                {
                    "step_order": step_order,
                    "success": bool(r.get("success")),
                    "thinking": r.get("thinking") or "",
                    "action": r.get("action") or "browser_use_fallback",
                    "next_goal": "",
                    "error": r.get("error"),
                    "duration_ms": r.get("duration_ms")
                    or (time.monotonic() - t0) * 1000,
                    "screenshot_base64": r.get("screenshot_base64"),
                    "backend": "browser_use_fallback",
                },
            )
            self._log_info(
                f"Hybrid fallback step {step_order} "
                f"{'passed' if r.get('success') else 'failed'}"
            )
        except Exception as e:
            self._log_error(f"Hybrid fallback failed: {e}")
            ss_b64 = None
            try:
                ss_b64 = await self._mcp_screenshot_base64()
            except Exception:
                pass
            await self._send(
                WSMessageType.STEP_RESULT,
                msg.run_id,
                {
                    "step_order": step_order,
                    "success": False,
                    "thinking": "",
                    "action": "browser_use_fallback",
                    "error": str(e),
                    "duration_ms": (time.monotonic() - t0) * 1000,
                    "screenshot_base64": ss_b64,
                    "backend": "browser_use_fallback",
                },
            )
        finally:
            # browser-use may have focused a new tab; MCP often still points at
            # the opener — next snapshot would activate the old tab in Chrome.
            try:
                from core.mcp_tabs import ensure_on_newest_tab

                if self._mcp_process_alive() and await ensure_on_newest_tab(
                    self._mcp_tools_call
                ):
                    self._log_info(
                        "Synced MCP to newest browser tab after hybrid fallback"
                    )
            except Exception as sync_exc:
                logger.warning(
                    "MCP tab sync after hybrid fallback failed: %s", sync_exc,
                )
            self._active_run_id = prev_run
            self._active_step_order = prev_step
            self._active_backend = prev_backend

    async def _handle_run_start_browser_use(self, msg: WSMessage):
        """Client-local browser-use: run all NL steps, reply RUN_COMPLETE."""
        payload = msg.payload or {}
        prev_run = self._active_run_id
        prev_step = self._active_step_order
        prev_backend = self._active_backend
        self._active_run_id = msg.run_id
        self._active_step_order = None
        self._active_backend = "browser_use"
        self._log_info(f"Run {msg.run_id} started — backend=browser_use")
        self._emit_status('busy')
        try:
            try:
                from core.browser_use_exec import (
                    create_browser_session,
                    create_browser_use_llm_from_config,
                    execute_nl_steps_browser_use,
                )
            except ImportError as exc:
                raise RuntimeError(
                    f"browser-use 执行模块不可用: {exc}. 请在 Agent 环境安装 browser-use"
                ) from exc

            llm_cfg = payload.get("llm") or {}
            api_key = (llm_cfg.get("api_key") or "").strip()
            if not api_key:
                raise RuntimeError("服务端未下发 LLM api_key，无法启动 browser-use")

            llm = create_browser_use_llm_from_config(
                api_key=api_key,
                api_base=llm_cfg.get("api_base"),
                model=llm_cfg.get("model"),
            )
            steps = payload.get("steps") or []
            navigate = payload.get("navigate_base_url", True)
            base_url = (payload.get("base_url") or "").strip() or None
            if not navigate:
                base_url = None
            max_steps = int(payload.get("max_steps_per_nl") or 20)
            # Client GUI/CLI headless wins. Server execution-backend.headless is for
            # server-side runs only; it used to force headless=True and skip maximize.
            server_headless = payload.get("headless")
            headless = bool(self._headless)
            if server_headless is not None and bool(server_headless) != headless:
                self._log_info(
                    f"browser-use: ignore server headless={server_headless}, "
                    f"use client headless={headless}"
                )

            if navigate or self._bu_browser is None:
                await self._stop_bu_browser()
                self._bu_browser = create_browser_session(
                    headless=headless,
                    keep_alive=True,
                    enable_default_extensions=False,
                )
                self._log_info(
                    f"browser-use: new browser session headless={headless} maximized={not headless}"
                )
            else:
                self._log_info("browser-use: reuse browser session (batch follow-up)")

            def _progress(line: str) -> None:
                # Parse "--- Step N " to update step_order for RUN_LOG payload
                m = re.match(r"--- Step (\d+)", line or "")
                if m:
                    try:
                        self._active_step_order = int(m.group(1))
                    except ValueError:
                        pass
                self._emit_log("info", line)

            step_results = await execute_nl_steps_browser_use(
                steps,
                llm=llm,
                base_url=base_url,
                headless=headless,
                max_steps_per_nl=max_steps,
                browser_session=self._bu_browser,
                stop_browser=False,
                on_progress=_progress,
            )
            status = (
                "passed"
                if step_results and all(r.get("success") for r in step_results)
                else "failed"
            )
            await self._send(
                WSMessageType.RUN_COMPLETE,
                msg.run_id,
                {"status": status, "steps": step_results, "backend": "browser_use"},
            )
            self._log_info(f"browser-use run {msg.run_id} complete: {status}")
        except Exception as e:
            self._log_error(f"browser-use run failed: {e}")
            self._emit_status('error')
            await self._send(
                WSMessageType.RUN_COMPLETE,
                msg.run_id,
                {
                    "status": "failed",
                    "error": str(e),
                    "steps": [],
                    "backend": "browser_use",
                },
            )
        finally:
            self._active_run_id = prev_run
            self._active_step_order = prev_step
            self._active_backend = prev_backend
            self._emit_status('idle')

    async def _handle_get_screenshot(self, run_id: str):
        try:
            ss_b64 = await self._mcp_screenshot_base64()
            await self._send(
                WSMessageType.SCREENSHOT_RESULT, run_id,
                {"screenshot_base64": ss_b64 or ""},
            )
        except Exception as e:
            await self._send(WSMessageType.ERROR, run_id, {"message": str(e)})

    async def _handle_get_snapshot(self, run_id: str):
        try:
            text = "(page not available)"
            if not self._mcp_process_alive():
                if self._run_in_progress():
                    recovered = await self._recover_mcp_keeping_chrome(
                        "Snapshot: MCP dead mid-run"
                    )
                    if not recovered:
                        text = self.BROWSER_CLOSED_SNAPSHOT_MARK
                        await self._abort_run_for_closed_browser(
                            "Snapshot: MCP/browser dead mid-run"
                        )
                else:
                    # Idle between runs — recover so the next case can start
                    try:
                        shared = self._exec_cdp_http is not None
                        await self._ensure_mcp_session(shared_cdp=shared)
                    except Exception as exc:
                        self._log_warning(f"Snapshot: failed to recover browser: {exc}")
            if self._mcp_process_alive() and text != self.BROWSER_CLOSED_SNAPSHOT_MARK:
                try:
                    result = await asyncio.wait_for(
                        self._mcp_call_tool("snapshot", "", ""), timeout=15
                    )
                    text = result.get("text") or result.get("error") or "(empty page)"
                    if (
                        not result.get("success")
                        and self._looks_like_mcp_protocol_glitch(str(text))
                    ):
                        if await self._recover_mcp_keeping_chrome(
                            "Snapshot MCP protocol glitch"
                        ):
                            result = await asyncio.wait_for(
                                self._mcp_call_tool("snapshot", "", ""), timeout=15
                            )
                            text = result.get("text") or result.get("error") or "(empty page)"
                    if (
                        not result.get("success")
                        and self._looks_like_dead_browser(str(text))
                    ):
                        if self._run_in_progress():
                            recovered = await self._recover_mcp_keeping_chrome(
                                "Snapshot hit dead MCP mid-run"
                            )
                            if recovered:
                                result = await asyncio.wait_for(
                                    self._mcp_call_tool("snapshot", "", ""), timeout=15
                                )
                                text = (
                                    result.get("text")
                                    or result.get("error")
                                    or "(empty page)"
                                )
                            else:
                                await self._abort_run_for_closed_browser(
                                    "Snapshot hit dead browser mid-run"
                                )
                                text = self.BROWSER_CLOSED_SNAPSHOT_MARK
                        else:
                            shared = self._exec_cdp_http is not None
                            await self._restart_mcp_for_dead_browser(
                                shared_cdp=shared,
                                why="snapshot hit dead browser",
                            )
                            result = await asyncio.wait_for(
                                self._mcp_call_tool("snapshot", "", ""), timeout=15
                            )
                            text = result.get("text") or result.get("error") or "(empty page)"
                    if (
                        text != self.BROWSER_CLOSED_SNAPSHOT_MARK
                        and len(text) > 8000
                    ):
                        text = text[:8000] + "\n\n[... TRUNCATED]"
                except asyncio.TimeoutError:
                    text = "(snapshot timeout)"
                    await self._invalidate_mcp_session("snapshot timeout")
                except Exception as exc:
                    if self._looks_like_mcp_protocol_glitch(str(exc)):
                        if await self._recover_mcp_keeping_chrome(
                            f"Snapshot recv glitch: {exc}"
                        ):
                            try:
                                result = await asyncio.wait_for(
                                    self._mcp_call_tool("snapshot", "", ""), timeout=15
                                )
                                text = (
                                    result.get("text")
                                    or result.get("error")
                                    or "(empty page)"
                                )
                            except Exception:
                                text = "(snapshot unavailable)"
                        else:
                            text = "(snapshot unavailable)"
                    else:
                        text = "(snapshot unavailable)"
            await self._send(
                WSMessageType.SNAPSHOT_RESULT, run_id,
                SnapshotPayload(text=text).model_dump(),
            )
        except Exception as e:
            await self._send(WSMessageType.ERROR, run_id, {"message": str(e)})

    async def _handle_step_execute(self, msg: WSMessage):
        tc = msg.payload.get("tool_call", {})
        step_order = msg.payload.get("step_order", 1)
        desc = msg.payload.get("description", "")
        t_start = time.monotonic()

        action = tc.get("action", "")
        selector = tc.get("selector") or ""
        value = tc.get("value")

        result = StepResultPayload(
            step_order=step_order,
            success=False,
            thinking=f"Executing: {action}",
            action=f"{action}({selector})",
        )

        try:
            if not self._mcp_process_alive():
                # Protocol glitch / MCP crash with Chrome still up → recover, don't abort
                recovered = await self._recover_mcp_keeping_chrome(
                    f"Step {step_order}: MCP dead"
                )
                if not recovered:
                    result.error = await self._abort_run_for_closed_browser(
                        f"Step {step_order}: MCP/browser dead"
                    )
                    result.thinking = result.error
                    result.success = False
                    result.duration_ms = (time.monotonic() - t_start) * 1000
                    await self._send(
                        WSMessageType.STEP_RESULT, msg.run_id, result.model_dump(),
                    )
                    return

            if action == "error":
                result.thinking = value or "LLM reported error for this step"
                result.action = f"error({value})"
                result.success = False
                result.error = value or "LLM reported error"
                result.screenshot_base64 = await self._mcp_screenshot_base64()

            elif action == "done":
                result.thinking = value or "LLM marked step done"
                result.action = f"done({value})"
                result.success = True
                result.screenshot_base64 = await self._mcp_screenshot_base64()

            else:
                mcp_result = await self._mcp_call_tool(
                    action, selector, value, step_description=desc,
                )
                err_blob = str(mcp_result.get("error") or mcp_result.get("text") or "")
                if not mcp_result.get("success") and self._looks_like_mcp_protocol_glitch(err_blob):
                    if await self._recover_mcp_keeping_chrome(
                        f"step {step_order} MCP protocol glitch"
                    ):
                        mcp_result = await self._mcp_call_tool(
                            action, selector, value, step_description=desc,
                        )
                        err_blob = str(
                            mcp_result.get("error") or mcp_result.get("text") or ""
                        )
                if (
                    not mcp_result.get("success")
                    and self._looks_like_dead_browser(err_blob)
                    and not self._looks_like_mcp_protocol_glitch(err_blob)
                ):
                    # Real browser death (CDP gone) → abort; else try one MCP recover
                    cdp_alive = bool(self._exec_cdp_http) and await self._is_exec_cdp_alive()
                    if cdp_alive and await self._recover_mcp_keeping_chrome(
                        f"step {step_order} hit dead MCP with live CDP"
                    ):
                        mcp_result = await self._mcp_call_tool(
                            action, selector, value, step_description=desc,
                        )
                        result.success = mcp_result.get("success", False)
                        if not result.success:
                            result.error = (
                                mcp_result.get("error")
                                or mcp_result.get("text", "MCP execution failed")
                            )
                            result.screenshot_base64 = await self._mcp_screenshot_base64()
                    else:
                        result.error = await self._abort_run_for_closed_browser(
                            f"step {step_order} hit dead browser"
                        )
                        result.thinking = result.error
                        result.success = False
                else:
                    result.success = mcp_result.get("success", False)
                    if not result.success:
                        result.error = mcp_result.get("error") or mcp_result.get(
                            "text", "MCP execution failed"
                        )
                        result.screenshot_base64 = await self._mcp_screenshot_base64()

        except Exception as e:
            err = str(e)
            if self._looks_like_mcp_protocol_glitch(err):
                if await self._recover_mcp_keeping_chrome(
                    f"Step {step_order} exception glitch: {err[:120]}"
                ):
                    result.error = f"MCP protocol glitch (recovered): {err[:160]}"
                else:
                    result.error = err
            elif self._looks_like_dead_browser(err) or "browser closed" in err.lower():
                result.error = await self._abort_run_for_closed_browser(
                    f"Step {step_order} exception: {err[:120]}"
                )
            else:
                result.error = err
            result.success = False
            self._emit_status('error')
            try:
                result.screenshot_base64 = await self._mcp_screenshot_base64()
            except Exception:
                pass

        result.duration_ms = (time.monotonic() - t_start) * 1000
        await self._send(
            WSMessageType.STEP_RESULT, msg.run_id, result.model_dump(),
        )

    async def _handle_observe(self, msg: WSMessage) -> None:
        """在客户端执行 observe 指令：获取页面 DOM/AX Tree 快照 + 截图。

        通过 self.send_result() 发回 Server，返回格式：
        {"success": ..., "snapshot": ..., "screenshot_b64": ..., "page_url": ..., "page_title": ...}
        """
        run_id = msg.run_id
        result: dict = {"success": False, "snapshot": "", "screenshot_b64": "", "page_url": "", "page_title": ""}

        try:
            if not self._mcp_process:
                raise RuntimeError("MCP subprocess not started")

            # 获取页面快照（15 秒超时保护）
            snapshot_result = await asyncio.wait_for(
                self._mcp_call_tool("snapshot", "", ""), timeout=15
            )
            snapshot_text = snapshot_result.get("text", "")
            result["snapshot"] = snapshot_text

            # 从快照文本中提取 page_url / page_title
            url_match = re.search(r"Page\s+URL:\s*(.+?)(?:\r?\n|$)", snapshot_text, re.IGNORECASE)
            title_match = re.search(r"Page\s+Title:\s*(.+?)(?:\r?\n|$)", snapshot_text, re.IGNORECASE)
            result["page_url"] = url_match.group(1).strip() if url_match else ""
            result["page_title"] = title_match.group(1).strip() if title_match else ""

            # 获取页面截图 base64
            screenshot_b64 = await self._mcp_screenshot_base64()
            result["screenshot_b64"] = screenshot_b64 or ""

            result["success"] = True
        except asyncio.TimeoutError:
            result["error"] = "Observe snapshot timed out after 15s"
            self._log_warning(f"Observe timeout for run {run_id}")
        except Exception as e:
            result["error"] = str(e)
            self._log_error(f"Observe failed for run {run_id}: {e}")

        await self.send_result(run_id, result)

    async def _handle_act(self, msg: WSMessage) -> None:
        """在客户端执行 MCP 操作（click、fill、goto 等）。

        从 msg.payload.tool_call 读取 action / selector / value，
        通过 self._mcp_call_tool 分发到对应 MCP 方法，执行后截图。
        通过 self.send_result() 发回 Server，返回格式：
        {"success": ..., "screenshot_b64": ..., "error": ...}
        """
        run_id = msg.run_id
        p = msg.payload or {}
        tc = p.get("tool_call", {}) or p
        action = tc.get("action") or p.get("action") or ""
        selector = tc.get("selector") if "selector" in tc else (p.get("selector") or "")
        value = tc.get("value") if "value" in tc else p.get("value")
        # send_act 可能把 URL 放在 url 字段
        if not value:
            value = tc.get("url") or p.get("url")
        step_description = (
            p.get("description")
            or tc.get("step_description")
            or tc.get("description")
            or ""
        )

        result: dict = {"success": False, "screenshot_b64": "", "error": ""}

        try:
            if not self._mcp_process:
                raise RuntimeError("MCP subprocess not started")

            # Bridge / 顶层 action 也可能带上 error/done 控制信号
            if (action or "").lower() in ("error", "done"):
                result["success"] = (action or "").lower() == "done"
                result["error"] = (
                    "" if result["success"]
                    else (value or "LLM reported error for this step")
                )
                ss_b64 = await self._mcp_screenshot_base64()
                result["screenshot_b64"] = ss_b64 or ""
                await self.send_result(run_id, result)
                return

            mcp_result = await self._mcp_call_tool(
                action, selector, value, step_description=step_description,
            )
            result["success"] = mcp_result.get("success", False)
            if not result["success"]:
                result["error"] = mcp_result.get("error") or mcp_result.get("text", "MCP execution failed")

            # 执行后始终截图
            ss_b64 = await self._mcp_screenshot_base64()
            result["screenshot_b64"] = ss_b64 or ""
        except Exception as e:
            result["error"] = str(e)
            result["success"] = False
            self._log_error(f"Act failed for run {run_id} (action={action}): {e}")
            # 异常时同样尝试截图
            try:
                ss_b64 = await self._mcp_screenshot_base64()
                result["screenshot_b64"] = ss_b64 or ""
            except Exception:
                pass

        await self.send_result(run_id, result)

    # ---- Recording handlers ----

    async def _start_chrome_with_cdp(self, headless: bool, target_url: str = "") -> Optional[str]:
        """Start Chrome directly with CDP debugging for remote recording.

        Returns CDP WebSocket URL with agent's LAN IP (e.g.
        ws://192.168.x.x:PORT/...) so the server can connect across the LAN.
        """
        # Find chrome binary (same search order as _start_mcp)
        _pkg_root = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.dirname(__file__))
        _search_roots = [_pkg_root]
        if getattr(sys, 'frozen', False):
            _search_roots.append(os.path.dirname(_pkg_root))  # parent of dist/
        _chrome_exe = None
        for _root in _search_roots:
            _candidate = os.path.join(_root, 'chromium', 'chrome-win64', 'chrome.exe')
            if os.path.isfile(_candidate):
                _chrome_exe = _candidate
                break
            _candidate = os.path.join(_root, 'chrome-win64', 'chrome.exe')
            if os.path.isfile(_candidate):
                _chrome_exe = _candidate
                break
        if not _chrome_exe:
            for _p in [
                'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
                'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
            ]:
                if os.path.isfile(_p):
                    _chrome_exe = _p
                    break
        if not _chrome_exe:
            raise RuntimeError("Chrome binary not found")

        import tempfile, socket
        # Use fixed port 9222 so firewall rules can be applied
        cdp_port = 0
        user_data_dir = tempfile.mkdtemp(prefix="voyan_cdp_")
        proc_kwargs = dict(
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if sys.platform != 'win32':
            proc_kwargs['preexec_fn'] = os.setsid

        chrome_args = [
            _chrome_exe,
            f'--remote-debugging-port={cdp_port}',
            f'--user-data-dir={user_data_dir}',
            '--no-first-run', '--no-default-browser-check',
            '--no-sandbox', '--disable-gpu',
            '--disable-popup-blocking',
            '--disable-features=ChromeWhatsNewUI,ChromeWhatsNew',
            '--disable-sync', '--disable-background-networking',
            '--disable-default-apps', '--disable-extensions',
        ]
        if headless:
            chrome_args.append('--headless=new')
        else:
            chrome_args.append('--start-maximized')

        self._chrome_user_data_dir = user_data_dir
        self._chrome_process = await asyncio.create_subprocess_exec(
            *chrome_args, **proc_kwargs,
        )

        # Read actual port from DevToolsActivePort (Chrome writes it after start)
        active_port_file = os.path.join(user_data_dir, 'DevToolsActivePort')
        actual_port = None
        for _attempt in range(30):
            await asyncio.sleep(0.5)
            try:
                with open(active_port_file) as f:
                    actual_port = int(f.readline().strip())
                break
            except (OSError, ValueError):
                continue
        if actual_port is None:
            raise RuntimeError("Chrome did not write DevToolsActivePort in time")

        # Check Chrome still alive and dump any startup output
        if self._chrome_process.returncode is not None:
            stdout, _ = await self._chrome_process.communicate()
            output = stdout.decode('utf-8', errors='replace') if stdout else '(empty)'
            raise RuntimeError(f"Chrome exited prematurely with code {self._chrome_process.returncode}. Output: {output[:500]}")

        # Chrome only listens on 127.0.0.1 (--remote-debugging-address often ignored on Windows).
        # Start a local TCP proxy on 0.0.0.0 so the server can connect across subnets.
        _proxy_port = actual_port + 1
        _proxy_server = await asyncio.start_server(
            lambda r, w: _pipe(r, w, '127.0.0.1', actual_port),
            host='0.0.0.0', port=_proxy_port,
        )
        self._proxy_server = _proxy_server
        logger.info(f"CDP proxy listening on 0.0.0.0:{_proxy_port} → 127.0.0.1:{actual_port}")

        try:
            # Poll /json/version for the full WS URL using async HTTP
            import httpx
            async with httpx.AsyncClient() as client:
                for _attempt in range(20):
                    await asyncio.sleep(0.5)
                    try:
                        resp = await client.get(f'http://127.0.0.1:{actual_port}/json/version', timeout=2)
                        data = resp.json()
                        ws_url = data.get('webSocketDebuggerUrl')
                        if ws_url:
                            logger.info(f"Chrome CDP ready (browser): {ws_url}")
                            break
                    except Exception:
                        continue
                else:
                    raise RuntimeError("Chrome CDP endpoint did not start in time")

                # Create a page target via CDP and get the page-level WS URL
                # Prefer reusing existing blank page to avoid extra tab
                import websockets as _ws
                try:
                    async with _ws.connect(ws_url) as _cdp:
                        # 先查已有页面，避免创建多余标签页
                        await _cdp.send(json.dumps({
                            "id": 1, "method": "Target.getTargets",
                        }))
                        resp_msg = json.loads(await _cdp.recv())
                        targets = resp_msg.get("result", {}).get("targetInfos", [])
                        target_id = None
                        for t in targets:
                            if t.get("type") == "page":
                                target_id = t["targetId"]
                                break
                        # 无已有页面时才创建新页面
                        if not target_id:
                            await _cdp.send(json.dumps({
                                "id": 2, "method": "Target.createTarget",
                                "params": {"url": target_url or "about:blank"},
                            }))
                            resp_msg = json.loads(await _cdp.recv())
                            target_id = resp_msg.get("result", {}).get("targetId")
                        elif target_url:
                            # 复用已有空白页面 - 连接到页面级 WS 后导航
                            page_ws_url = f"ws://127.0.0.1:{_proxy_port}/devtools/page/{target_id}"
                            try:
                                async with _ws.connect(page_ws_url) as page_cdp:
                                    await page_cdp.send(json.dumps({
                                        "id": 1, "method": "Page.navigate",
                                        "params": {"url": target_url},
                                    }))
                                    # 等待导航开始响应
                                    try:
                                        await asyncio.wait_for(page_cdp.recv(), timeout=10)
                                    except asyncio.TimeoutError:
                                        logger.warning("Page.navigate response timeout, continuing anyway")
                            except Exception as e:
                                logger.warning(f"Failed to navigate on reused page: {e}")
                        if target_id:
                            page_ws = f"ws://127.0.0.1:{_proxy_port}/devtools/page/{target_id}"
                            logger.info(f"Page target acquired: {page_ws}")
                            ws_url = page_ws
                except Exception as exc:
                    logger.warning(f"Failed to create page target via CDP: {exc}")

                ws_url = ws_url.replace('127.0.0.1', self.ip_address)
                logger.info(f"CDP page WS URL: {ws_url}")
                return ws_url
        except Exception:
            # Clean up Chrome and proxy on failure
            if self._chrome_process:
                self._chrome_process.kill()
                try:
                    await asyncio.wait_for(self._chrome_process.wait(), timeout=5)
                except Exception:
                    pass
            if self._proxy_server:
                self._proxy_server.close()
                self._proxy_server = None
            raise

    async def _handle_recording_start(self, msg: WSMessage):
        """Start Chrome with CDP for recording. Returns CDP URL to server."""
        payload = msg.payload or {}
        url = payload.get("url", "")
        headless = payload.get("headless", False)
        self._log_info(f"Recording start — url={url}")

        try:
            cdp_url = await self._start_chrome_with_cdp(headless, url)
            self._cdp_url = cdp_url
            self._is_recording = True

            await self._send(WSMessageType.RECORDING_READY, msg.run_id, {
                "status": "ready",
                "cdp_url": cdp_url,
                "browser_type": "chromium",
            })
        except Exception as e:
            self._log_error(f"Recording start failed: {e}")
            self._emit_status('error')
            await self._send(WSMessageType.ERROR, msg.run_id, {"message": f"Recording start failed: {e}"})

    async def _handle_recording_stop(self, msg: WSMessage):
        """Kill the CDP Chrome process. Events were already captured server-side."""
        self._log_info("Recording stop — killing CDP Chrome")
        self._is_recording = False
        if self._chrome_process:
            try:
                if sys.platform != 'win32' and self._chrome_process.pid:
                    os.killpg(os.getpgid(self._chrome_process.pid), 9)
                else:
                    self._chrome_process.kill()
                await asyncio.wait_for(self._chrome_process.wait(), timeout=5)
            except Exception:
                try:
                    self._chrome_process.kill()
                except Exception:
                    pass
        # Close CDP TCP proxy
        if self._proxy_server:
            self._proxy_server.close()
            self._proxy_server = None

        # Clean up user data dir
        if hasattr(self, '_chrome_user_data_dir') and self._chrome_user_data_dir:
            import shutil
            try:
                shutil.rmtree(self._chrome_user_data_dir, ignore_errors=True)
            except Exception:
                pass
        self._cdp_url = None
        await self._send(WSMessageType.RECORDING_READY, msg.run_id, {"status": "stopped"})
