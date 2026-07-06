"""Agent client — connects via WebSocket, receives step-by-step tool calls, executes via local MCP subprocess."""

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
from typing import Optional

import websockets

_base_dir = os.path.dirname(os.path.dirname(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable)
project_root = os.path.abspath(_base_dir)
sys.path.insert(0, project_root)

from agent.models import (
    AgentRegistration, WSMessage, WSMessageType,
    StepResultPayload, SnapshotPayload,
)

logger = logging.getLogger("agent.client")

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
                 username: str = None, password: str = None):
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
        self._cdp_url = None
        self._is_recording = False

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
        """Authenticate with the server and store session_id cookie."""
        if not self._username or not self._password:
            logger.info("No credentials provided — connecting without authentication")
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
                    logger.error(f"Login failed (HTTP {resp.status_code}): {resp.text}")
                    return
                # httpx < v0.28: resp.cookies["session_id"] returns str
                sid = resp.cookies.get("session_id") or resp.cookies.get("session_id")
                if isinstance(sid, str) and sid.strip():
                    self._session_id = sid
                    logger.info(f"Authenticated as {self._username}")
                else:
                    logger.warning(f"Login succeeded but no session_id cookie received (got: {sid!r})")
        except Exception as e:
            logger.warning(f"Login request failed (server may not require auth): {e}")

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
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=10,
                                          additional_headers=ws_headers) as ws:
                self._ws = ws
                self.running = True
                await self._send_registration()
                logger.info(f"Connected as {self.agent_name}")

                while self.running:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=120)
                        logger.debug(f"WS recv: {msg[:200]}...")
                        await self._handle_message(json.loads(msg))
                    except asyncio.TimeoutError:
                        await self._send_heartbeat()
                    except websockets.ConnectionClosed:
                        logger.warning("Connection closed by server")
                        break
                    except Exception as exc:
                        logger.error(f"Message handler error: {exc}", exc_info=True)
        except Exception as e:
            logger.error(f"Connection failed: {e}")
        finally:
            await self._stop_mcp()
            self.running = False

    async def stop(self):
        self.running = False
        if self._ws:
            await self._ws.close()

    # ---- MCP subprocess management ----

    async def _start_mcp(self):
        if self._mcp_process:
            logger.info("Stopping previous MCP before starting new one")
            await self._stop_mcp()
        logger.info(f"Starting MCP subprocess: chromium headless={self._headless}")

        # 确定包根目录（exe 同级）
        _pkg_root = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.dirname(__file__))
        _search_roots = [_pkg_root]
        if getattr(sys, 'frozen', False):
            _search_roots.append(os.path.dirname(_pkg_root))  # parent of dist/

        # 查找捆绑的 node.exe
        _node_exe = os.path.join(_pkg_root, 'node.exe')
        if not os.path.isfile(_node_exe):
            _node_exe = 'node'  # fallback to system PATH

        # 查找捆绑的 @playwright/mcp 入口
        _cli_js = os.path.join(_pkg_root, 'node_modules', '@playwright', 'mcp', 'cli.js')
        if not os.path.isfile(_cli_js):
            # fallback: try local node_modules relative to project
            _base = os.path.dirname(os.path.dirname(__file__))
            _cli_js = os.path.join(_base, 'node_modules', '@playwright', 'mcp', 'cli.js')

        args = [_node_exe, _cli_js, '--browser=chromium']

        # 查找捆绑的 Chromium（多种布局兼容）
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
            playwright_browsers = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', ''))
            if not playwright_browsers.is_dir():
                playwright_browsers = Path.home() / 'AppData' / 'Local' / 'ms-playwright'
            chrome_dirs = sorted(playwright_browsers.glob('chromium-*/chrome-win64/chrome.exe')) if playwright_browsers.is_dir() else []
            if chrome_dirs:
                _chrome_exe = str(chrome_dirs[-1])
        if not _chrome_exe:
            # 尝试系统已安装的 Chrome
            for _p in [
                'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
                'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
            ]:
                if os.path.isfile(_p):
                    _chrome_exe = _p
                    break

        if os.path.isfile(_chrome_exe):
            args.extend(['--executable-path', _chrome_exe])
            logger.info(f"Using Chromium executable: {_chrome_exe}")
        if self._headless:
            args.append('--headless')
        else:
            args.extend(['--viewport-size', '1920x1080'])
        args.append('--isolated')
        import sys as _sys
        proc_kwargs = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
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
            logger.error("MCP initialize failed — no response")
            raise RuntimeError("MCP subprocess failed to initialize")
        logger.info("MCP initialized")

        await self._mcp_notify("notifications/initialized")
        logger.info("MCP subprocess ready (browser started)")

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
            logger.info("MCP subprocess stopped (browser closed)")

    async def _pipe_stderr(self):
        try:
            while True:
                line = await self._mcp_process.stderr.readline()
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
        mcp_tool = ACTION_TOOL_MAP.get(action)
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
            logger.info(f"Run {msg.run_id} started — launching browser")
            try:
                if not self._mcp_process:
                    await self._start_mcp()
                else:
                    logger.info("Reusing existing MCP subprocess (browser stays open)")
                    try:
                        await asyncio.wait_for(self._mcp_call_tool("snapshot", "", ""), timeout=10)
                    except Exception:
                        logger.warning("Existing MCP not responding, restarting")
                        await self._stop_mcp()
                        await self._start_mcp()
            except Exception as e:
                logger.error(f"Failed to start MCP for run {msg.run_id}: {e}")
                await self._send(
                    WSMessageType.ERROR, msg.run_id,
                    {"message": f"MCP start failed: {e}"},
                )

        elif msg.type == WSMessageType.RUN_END:
            logger.info(f"Run {msg.run_id} ended — MCP stays alive for next run")
            try:
                await self._mcp_call_tool("snapshot", "", "")
            except Exception as e:
                logger.warning(f"Error during run end: {e}")

        elif msg.type == WSMessageType.GET_SNAPSHOT:
            await self._handle_get_snapshot(msg.run_id)

        elif msg.type == WSMessageType.GET_SCREENSHOT:
            await self._handle_get_screenshot(msg.run_id)

        elif msg.type == WSMessageType.STEP_EXECUTE:
            await self._handle_step_execute(msg)

        elif msg.type == WSMessageType.SHUTDOWN:
            logger.info("Shutdown signal received — closing browser")
            try:
                await self._stop_mcp()
            except Exception as e:
                logger.error(f"Error shutting down MCP: {e}")

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
                    result = await self._mcp_call_tool("snapshot", "", "")
                    text = result.get("text", "(empty page)")
                    if len(text) > 8000:
                        text = text[:8000] + "\n\n[... TRUNCATED]"
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
            result.screenshot_base64 = await self._mcp_screenshot_base64()

        result.duration_ms = (time.monotonic() - t_start) * 1000
        await self._send(
            WSMessageType.STEP_RESULT, msg.run_id, result.model_dump(),
        )

    # ---- Recording handlers ----

    async def _start_chrome_with_cdp(self, headless: bool) -> Optional[str]:
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

        self._chrome_process = await asyncio.create_subprocess_exec(
            _chrome_exe,
            f'--remote-debugging-port={cdp_port}',
            f'--user-data-dir={user_data_dir}',
            '--no-first-run', '--no-default-browser-check',
            '--no-sandbox', '--disable-gpu',
            '--disable-features=ChromeWhatsNewUI,ChromeWhatsNew',
            '--disable-sync', '--disable-background-networking',
            '--disable-default-apps', '--disable-extensions',
            '--start-maximized',
            **proc_kwargs,
        )
        self._chrome_user_data_dir = user_data_dir

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
        _proxy_server = await asyncio.start_server(
            lambda r, w: _pipe(r, w, '127.0.0.1', actual_port),
            host='0.0.0.0', port=actual_port,
        )
        self._proxy_server = _proxy_server
        logger.info(f"CDP proxy listening on 0.0.0.0:{actual_port} → 127.0.0.1:{actual_port}")

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
                        # Replace 127.0.0.1 with LAN IP so server can connect
                        ws_url = ws_url.replace('127.0.0.1', self.ip_address)
                        logger.info(f"Chrome CDP ready: {ws_url}")
                        return ws_url
                except Exception:
                    continue

        raise RuntimeError("Chrome CDP endpoint did not start in time")

    async def _handle_recording_start(self, msg: WSMessage):
        """Start Chrome with CDP for recording. Returns CDP URL to server."""
        payload = msg.payload or {}
        url = payload.get("url", "")
        headless = payload.get("headless", False)
        logger.info(f"Recording start — url={url}")

        try:
            cdp_url = await self._start_chrome_with_cdp(headless)
            self._cdp_url = cdp_url
            self._is_recording = True

            # Navigate to target URL via CDP (browser-level Target.createTarget)
            if url and cdp_url:
                import websockets as _ws
                try:
                    async with _ws.connect(cdp_url) as _cdp_ws:
                        # Create a new page/tab with the target URL
                        cmd = json.dumps({
                            "id": 1, "method": "Target.createTarget",
                            "params": {"url": url},
                        })
                        await _cdp_ws.send(cmd)
                        await _cdp_ws.recv()
                        logger.info(f"CDP navigation to {url} sent via Target.createTarget")
                except Exception as e:
                    logger.warning(f"CDP navigation to {url} failed: {e}")

            await self._send(WSMessageType.RECORDING_READY, msg.run_id, {
                "status": "ready",
                "cdp_url": cdp_url,
                "browser_type": "chromium",
            })
        except Exception as e:
            logger.error(f"Recording start failed: {e}")
            await self._send(WSMessageType.ERROR, msg.run_id, {"message": f"Recording start failed: {e}"})

    async def _handle_recording_stop(self, msg: WSMessage):
        """Kill the CDP Chrome process. Events were already captured server-side."""
        logger.info("Recording stop — killing CDP Chrome")
        self._is_recording = False
        if hasattr(self, '_chrome_process') and self._chrome_process:
            try:
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


def main():
    import argparse

    # Check if running as packaged exe with no args → interactive mode
    is_frozen = getattr(sys, 'frozen', False)

    if is_frozen and len(sys.argv) == 1:
        # Packaged with no args → interactive mode
        print("=" * 50)
        print("  VoyanTest Agent Client")
        print("=" * 50)
        print()
        server = input("Server URL (e.g. ws://192.168.1.100:8002): ").strip()
        if not server:
            server = "ws://localhost:8002"
        if not server.startswith("ws://") and not server.startswith("wss://"):
            server = "ws://" + server
        name_input = input("Agent name (leave empty for auto-generated): ").strip()
        name = name_input or None
        headless_input = input("Use headless mode? (y/N): ").strip().lower()
        headless = headless_input in ("y", "yes")
        username_input = input("Username (leave empty to skip auth): ").strip()
        password_input = input("Password (leave empty to skip auth): ").strip()
        print()
        print(f"Server: {server}")
        print(f"Name: {name or '(auto-generated)'}")
        print(f"Headless: {'yes' if headless else 'no'}")
        if username_input:
            print(f"User: {username_input}")
        print("-" * 50)
        print("Connecting...")
        print()
        args = argparse.Namespace(
            server=server,
            name=name,
            headless=headless,
            username=username_input or None,
            password=password_input or None,
        )
    else:
        parser = argparse.ArgumentParser(description="VoyanTest Agent Client")
        parser.add_argument("--server", required=not is_frozen, help="Server URL (e.g. ws://192.168.1.100:8002)")
        parser.add_argument("--name", help="Agent name (default: auto-generated)")
        parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
        parser.add_argument("--username", help="Username for server authentication")
        parser.add_argument("--password", help="Password for server authentication")
        args = parser.parse_args()

        # Unpackaged and no server arg → interactive mode
        if not args.server:
            print("=" * 50)
            print("  VoyanTest Agent Client")
            print("=" * 50)
            print()
            server = input("Server URL (e.g. ws://192.168.1.100:8002): ").strip()
            if not server:
                server = "ws://localhost:8002"
            if not server.startswith("ws://") and not server.startswith("wss://"):
                server = "ws://" + server
            args.server = server
            name_input = input("Agent name (leave empty for auto-generated): ").strip()
            args.name = name_input or None
            headless_input = input("Use headless mode? (y/N): ").strip().lower()
            args.headless = headless_input in ("y", "yes")
            username_input = input("Username (leave empty to skip auth): ").strip()
            args.username = username_input or None
            password_input = input("Password (leave empty to skip auth): ").strip()
            args.password = password_input or None
            print()
            print(f"Server: {args.server}")
            print(f"Name: {args.name or '(auto-generated)'}")
            print(f"Headless: {'yes' if args.headless else 'no'}")
            if args.username:
                print(f"User: {args.username}")
            print("-" * 50)
            print("Connecting...")
            print()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    agent = AgentClient(args.server, args.name, headless=args.headless,
                        username=args.username, password=args.password)
    try:
        asyncio.run(agent.start())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")


if __name__ == "__main__":
    main()
