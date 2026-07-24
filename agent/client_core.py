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

        # CDP Chrome recording state
        self._chrome_process = None
        self._chrome_user_data_dir = None
        self._proxy_server = None
        self._cdp_url = None
        self._is_recording = False

        # Callback hooks
        self._on_status_change = on_status_change
        self._on_log = on_log

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
        """发出日志消息。调用 logger 并触发 on_log 回调。"""
        log_func = getattr(logger, level, logger.info)
        log_func(message)
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                logger.warning("on_log callback failed", exc_info=True)

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

    async def _start_mcp(self):
        # 清理可能残留的旧进程
        try:
            import subprocess as _sp
            for _patt in ['playwright', 'chrome', 'chromium']:
                _out = _sp.run(['pkill', '-f', _patt], capture_output=True, timeout=3)
                if _out.returncode == 0:
                    self._log_info(f"Cleaned up stale {_patt} processes")
        except Exception:
            pass
        if self._mcp_process:
            self._log_info("Stopping previous MCP before starting new one")
            await self._stop_mcp()
        self._log_info(f"Starting MCP: chromium headless={self._headless}")

        # 确定包根目录（exe 同级）
        _pkg_root = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.dirname(__file__))
        _search_roots = [_pkg_root]
        if getattr(sys, 'frozen', False):
            _search_roots.append(os.path.dirname(_pkg_root))  # parent of dist/

        # 查找捆绑的 node.exe
        _node_exe = os.path.join(_pkg_root, 'node.exe')
        if not os.path.isfile(_node_exe):
            _node_exe = 'node'  # fallback to system PATH

        # 查找捆绑的 @playwright/mcp 入口（搜索全部根目录，与 node.exe 一致）
        _cli_js = ""
        for _root in _search_roots:
            _candidate = os.path.join(_root, 'node_modules', '@playwright', 'mcp', 'cli.js')
            if os.path.isfile(_candidate):
                _cli_js = _candidate
                break
        if not _cli_js or not os.path.isfile(_cli_js):
            raise RuntimeError(
                f"@playwright/mcp CLI not found. "
                f"Searched in: {[os.path.join(r, 'node_modules', '@playwright', 'mcp', 'cli.js') for r in _search_roots]}"
            )

        args = [_node_exe, _cli_js, '--browser=chromium']

        # 查找 Chromium（Playwright 缓存优先，系统安装次之）
        _chrome_exe = None
        if sys.platform == 'win32':
            for _root in _search_roots:
                for _name in ['chromium/chrome-win64/chrome.exe', 'chrome-win64/chrome.exe']:
                    _candidate = os.path.join(_root, _name)
                    if os.path.isfile(_candidate):
                        _chrome_exe = _candidate
                        break
                if _chrome_exe:
                    break
        if not _chrome_exe:
            # Playwright 缓存中的 Chromium
            playwright_browsers = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', ''))
            if not playwright_browsers.is_dir():
                # Linux
                playwright_browsers = Path.home() / '.cache' / 'ms-playwright'
            if not playwright_browsers.is_dir():
                playwright_browsers = Path.home() / 'AppData' / 'Local' / 'ms-playwright'
            for _pat in ['chrome-linux64/chrome', 'chrome-linux/chrome', 'chrome-win64/chrome.exe']:
                _cd = sorted(playwright_browsers.glob(f'chromium-*/{_pat}')) if playwright_browsers.is_dir() else []
                if _cd:
                    _chrome_exe = str(_cd[-1])
                    break
        if not _chrome_exe:
            # Linux 系统 Chromium
            for _p in [
                '/usr/bin/chromium',
                '/usr/bin/chromium-browser',
                '/usr/bin/google-chrome',
                '/usr/bin/google-chrome-stable',
                '/snap/bin/chromium',
            ]:
                if os.path.isfile(_p):
                    _chrome_exe = _p
                    break
        if not _chrome_exe:
            # Windows 系统路径兜底
            for _p in [
                'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
                'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
            ]:
                if os.path.isfile(_p):
                    _chrome_exe = _p
                    break

        if os.path.isfile(_chrome_exe):
            args.extend(['--executable-path', _chrome_exe])
            self._log_info(f"Using Chromium: {_chrome_exe}")
        if self._headless:
            args.append('--headless')
        else:
            args.extend(['--viewport-size', '1920x1080'])
        args.append('--isolated')
        import sys as _sys
        proc_kwargs = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if _sys.platform != 'win32':
            proc_kwargs['preexec_fn'] = os.setsid
        self._mcp_process = await asyncio.create_subprocess_exec(
            *args, **proc_kwargs,
        )
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

    async def _stop_mcp(self):
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
            self._log_info("MCP subprocess stopped (browser closed)")

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

    async def _mcp_call_tool(self, action: str, selector: str, value: str) -> dict:
        mcp_tool = _resolve_mcp_tool(action)
        if not mcp_tool:
            return {"success": False, "error": f"Unknown action: {action}"}

        args = self._build_mcp_args(action, selector, value)
        req_id = await self._mcp_send("tools/call", {"name": mcp_tool, "arguments": args})

        try:
            resp = await self._mcp_recv()
            result = resp.get("result", {})
            is_error = result.get("isError", False)
            content = result.get("content", [])
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            return {"success": not is_error, "text": text, "_content": content}
        except asyncio.TimeoutError:
            return {"success": False, "error": "MCP tool call timed out"}

    async def _mcp_screenshot_base64(self) -> Optional[str]:
        """Take a screenshot via MCP and return base64-encoded PNG."""
        try:
            result = await self._mcp_call_tool("screenshot", "", f"_fail_{int(time.time())}.png")
            if not result.get("success"):
                return None
            return self._extract_screenshot_base64(result.get("_content", []))
        except Exception as exc:
            logger.warning(f"MCP screenshot failed: {exc}")
            return None

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
        reg = AgentRegistration(
            name=self.agent_name,
            hostname=self.hostname,
            ip_address=self.ip_address,
            capabilities=["mcp", "playwright", "ui_testing", "local_browser"],
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
            self._log_info(f"Run {msg.run_id} started — launching browser")
            self._emit_status('busy')
            try:
                if not self._mcp_process:
                    await self._start_mcp()
                else:
                    self._log_info("Reusing existing MCP subprocess (browser stays open)")
                    try:
                        await asyncio.wait_for(self._mcp_call_tool("snapshot", "", ""), timeout=10)
                    except Exception:
                        self._log_warning("Existing MCP not responding, restarting")
                        await self._stop_mcp()
                        await self._start_mcp()
            except Exception as e:
                self._log_error(f"Failed to start MCP for run {msg.run_id}: {e}")
                self._emit_status('error')
                await self._send(
                    WSMessageType.ERROR, msg.run_id,
                    {"message": f"MCP start failed: {e}"},
                )

        elif msg.type == WSMessageType.RUN_END:
            self._log_info(f"Run {msg.run_id} ended — MCP stays alive for next run")
            try:
                await self._mcp_call_tool("snapshot", "", "")
            except Exception as e:
                self._log_warning(f"Error during run end: {e}")
            self._emit_status('idle')

        elif msg.type == WSMessageType.GET_SNAPSHOT:
            await self._handle_get_snapshot(msg.run_id)

        elif msg.type == WSMessageType.GET_SCREENSHOT:
            await self._handle_get_screenshot(msg.run_id)

        elif msg.type == WSMessageType.STEP_EXECUTE:
            p = msg.payload or {}
            action = p.get("tool_call", {}).get("action", "") or p.get("action", "")
            if action == "observe":
                await self._handle_observe(msg)
            else:
                await self._handle_act(msg)

        elif msg.type == WSMessageType.SHUTDOWN:
            self._log_info("Shutdown signal received — closing browser")
            try:
                await self._stop_mcp()
            except Exception as e:
                self._log_error(f"Error shutting down MCP: {e}")

        elif msg.type == WSMessageType.HEARTBEAT:
            pass

        elif msg.type == WSMessageType.RECORDING_START:
            await self._handle_recording_start(msg)

        elif msg.type == WSMessageType.RECORDING_STOP:
            await self._handle_recording_stop(msg)

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
            if self._mcp_process:
                try:
                    result = await asyncio.wait_for(
                        self._mcp_call_tool("snapshot", "", ""), timeout=15
                    )
                    text = result.get("text", "(empty page)")
                    if len(text) > 8000:
                        text = text[:8000] + "\n\n[... TRUNCATED]"
                except asyncio.TimeoutError:
                    text = "(snapshot timeout)"
                except Exception:
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
            if not self._mcp_process:
                raise RuntimeError("MCP subprocess not started")

            if action == "error":
                result.thinking = value or "LLM reported error for this step"
                result.action = f"error({value})"
                result.success = False
                result.error = value or "LLM reported error"
                result.screenshot_base64 = await self._mcp_screenshot_base64()

            else:
                mcp_result = await self._mcp_call_tool(action, selector, value)
                result.success = mcp_result.get("success", False)
                if not result.success:
                    result.error = mcp_result.get("error") or mcp_result.get("text", "MCP execution failed")
                    result.screenshot_base64 = await self._mcp_screenshot_base64()

        except Exception as e:
            result.error = str(e)
            result.success = False
            self._emit_status('error')
            result.screenshot_base64 = await self._mcp_screenshot_base64()

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
        action = tc.get("action", "")
        selector = tc.get("selector") or ""
        value = tc.get("value")

        result: dict = {"success": False, "screenshot_b64": "", "error": ""}

        try:
            if not self._mcp_process:
                raise RuntimeError("MCP subprocess not started")

            mcp_result = await self._mcp_call_tool(action, selector, value)
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
