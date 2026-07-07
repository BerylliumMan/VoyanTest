# core/cdp_session.py
"""
CDP recording session orchestrator.

通过 Chrome DevTools Protocol (CDP) 监听浏览器交互事件，并将原始 CDP
事件转换为结构化的 RecordedEvent 列表，供 core/cdp_converter.py (T2)
进一步转换为可执行的测试步骤。

支持的 event_type:
  - navigation:   页面导航（Page.frameNavigated / Page.loadEventFired）
  - click:        元素点击（通过注入的 JS 监听器捕获）
  - input:        输入框文本输入（通过注入的 JS 监听器捕获）
  - select:       下拉框选择（change 事件，target.tagName === 'SELECT'）
  - screenshot:   主动截图
  - wait:         等待动作
  - assertion:    断言/验证动作

输入 page_or_cdp_url 可以是：
  - PlaywrightMCPManager 实例（使用其 call_tool 桥接到 MCP 的 CDP 工具）
  - playwright.async_api.Page 实例（使用 page.context.new_cdp_session）
  - CDP WebSocket URL 字符串（"ws://..."）
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 事件类型白名单（与 RecordedEvent.event_type 保持一致）
VALID_EVENT_TYPES: frozenset[str] = frozenset({
    "navigation",
    "click",
    "input",
    "select",
    "screenshot",
    "wait",
    "assertion",
})

# CDP method → event_type 映射（针对原生 CDP 事件）
_CDP_METHOD_TO_EVENT: dict[str, str] = {
    "Page.frameNavigated": "navigation",
    "Page.navigatedWithinDocument": "navigation",
    "Page.loadEventFired": "navigation",
    "Page.domContentEventFired": "navigation",
}

# 注入到页面中的 JS 监听器：把用户交互上报为 __cdp_recorder__ event，
# 由 Runtime.consoleAPICalled 接收并转换为 RecordedEvent。
_INJECT_RECORDER_SCRIPT = r"""
(function () {
  if (window.__cdp_recorder_installed__) return;
  window.__cdp_recorder_installed__ = true;

  function report(type, payload) {
    try {
      var detail = Object.assign({__recorder_type__: type}, payload || {});
      console.log("__CDP_RECORDER__:" + JSON.stringify(detail));
    } catch (e) { /* ignore serialization errors */ }
  }

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.id) return "#" + el.id;
    var name = el.getAttribute("name");
    if (name) return "[name=\"" + name + "\"]";
    if (el.dataset && el.dataset.testid) return "[data-testid=\"" + el.dataset.testid + "\"]";
    var role = el.getAttribute("role");
    if (role) {
      var label = el.getAttribute("aria-label") || (el.textContent || "").trim().slice(0, 40);
      return "[role=\"" + role + "\"]:has-text(\"" + (label || "").replace(/"/g, '\\"') + "\")";
    }
    var tag = (el.tagName || "").toLowerCase();
    if (!tag) return null;
    var text = (el.textContent || "").trim().slice(0, 40);
    return text ? tag + ":has-text(\"" + text.replace(/"/g, '\\"') + "\")" : tag;
  }

  var _input_timers = {};

  document.addEventListener("click", function (ev) {
    var el = ev.target;
    report("click", {selector: describe(el), tag: el && el.tagName, text: (el && el.textContent || "").trim().slice(0, 80)});
  }, true);

  document.addEventListener("input", function (ev) {
    var el = ev.target;
    if (!el) return;
    var tag = (el.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea") return;
    var key = describe(el) || tag;
    if (_input_timers[key]) clearTimeout(_input_timers[key]);
    _input_timers[key] = setTimeout(function() {
      report("input", {selector: key, value: el.value, tag: tag});
      delete _input_timers[key];
    }, 500);
  }, true);

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (!el) return;
    var tag = (el.tagName || "").toLowerCase();
    if (tag === "select") {
      report("select", {selector: describe(el), value: el.value, tag: tag});
    }
    // input/textarea change (blur): cancel pending timer and report immediately
    if (tag === "input" || tag === "textarea") {
      var key = describe(el) || tag;
      if (_input_timers[key]) {
        clearTimeout(_input_timers[key]);
        delete _input_timers[key];
      }
      report("input", {selector: key, value: el.value, tag: tag});
    }
  }, true);
})();
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RecordedEvent:
    """A single recorded browser event captured via CDP."""

    event_type: str
    timestamp: float
    selector: Optional[str] = None
    value: Optional[str] = None
    url: str = ""
    screenshot: Optional[str] = None
    page_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return asdict(self)

    def is_valid(self) -> bool:
        """Return True if the event_type is recognised."""
        return self.event_type in VALID_EVENT_TYPES


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class CDPRecordingSession:
    """Connects to a Playwright CDP endpoint and records user interactions.

    Lifecycle:
        session = CDPRecordingSession(session_id="abc")
        await session.start_recording(page_or_cdp_url=...)
        ...
        await session.stop_recording()
        events = session.collect_events()
    """

    def __init__(self, session_id: str) -> None:
        self._session_id: str = session_id
        self._events: list[RecordedEvent] = []
        self._recording: bool = False
        self._start_time: Optional[float] = None

        # CDP wiring state (private; not part of the public interface)
        self._cdp_session: Any = None
        self._cdp_url: Optional[str] = None
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._msg_counter: int = 0
        self._attached_manager: Any = None  # PlaywrightMCPManager reference
        self._last_page_url: str = ""
        self._last_page_title: str = ""

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return max(0.0, time.time() - self._start_time)

    @property
    def events_count(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_recording(self, page_or_cdp_url: Any) -> bool:
        """Start recording browser events from a CDP-capable source.

        Parameters
        ----------
        page_or_cdp_url : Any
            One of:
              - ``playwright.async_api.Page`` instance
              - ``str`` CDP WebSocket URL (``ws://...``)
              - ``PlaywrightMCPManager`` instance (uses MCP ``browser_cdp_session`` tool)

        Returns
        -------
        bool
            True if recording was successfully started, False otherwise.
        """
        if self._recording:
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: start_recording called "
                f"while already recording; ignoring."
            )
            return False

        try:
            ok = await self._attach_cdp(page_or_cdp_url)
            if not ok:
                logger.error(
                    f"CDPRecordingSession[{self._session_id}]: failed to attach CDP."
                )
                return False

            await self._enable_domains()
            await self._install_page_recorder()

            self._recording = True
            self._start_time = time.time()
            logger.info(
                f"CDPRecordingSession[{self._session_id}]: recording started."
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 录制启动涉及 Playwright/CDP/asyncio，任一失败都需清理
            logger.error(
                f"CDPRecordingSession[{self._session_id}]: start_recording failed: {exc}",
                exc_info=True,
            )
            await self._safe_detach()
            self._recording = False
            self._start_time = None
            return False

    async def stop_recording(self) -> bool:
        """Stop recording, release the CDP session, and reset state."""
        if not self._recording:
            logger.debug(
                f"CDPRecordingSession[{self._session_id}]: stop_recording called "
                f"when not recording; no-op."
            )
            return True

        try:
            await self._disable_domains()
        except Exception as exc:  # noqa: BLE001 - 停止录制时关闭域是 best-effort
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: disable domains failed: {exc}"
            )

        try:
            await self._safe_detach()
        except Exception as exc:
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: detach failed: {exc}"
            )

        self._recording = False
        logger.info(
            f"CDPRecordingSession[{self._session_id}]: recording stopped "
            f"({self.events_count} events captured over "
            f"{self.elapsed_seconds:.1f}s)."
        )
        return True

    def collect_events(self) -> list[RecordedEvent]:
        """Return a copy of the recorded events and clear the internal buffer.

        Using a copy-then-clear pattern prevents the caller from mutating the
        session's internal state.
        """
        events = list(self._events)
        self._events.clear()
        return events

    def get_events(self) -> list[RecordedEvent]:
        """Return a copy of the recorded events without clearing the internal buffer.

        Unlike :meth:`collect_events`, this method does **not** clear ``_events``,
        so the same events remain available for subsequent calls (e.g. by the
        convert endpoint after the events endpoint has already read them).
        """
        return list(self._events)

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def record_event(self, event_data: dict[str, Any]) -> None:
        """Append a new RecordedEvent built from a raw CDP-style event dict.

        This is the internal ingestion point: domain listeners (Page, Runtime,
        custom console) call this with a normalised dict. The session itself
        is responsible for translating CDP payloads into this shape.

        Expected keys in ``event_data`` (all optional except event_type):
          - event_type: str  (required, must be in VALID_EVENT_TYPES)
          - selector:   str
          - value:      str
          - url:        str
          - screenshot: str  (base64-encoded image data, if any)
          - page_title: str
        Extra keys are ignored. Missing optional keys fall back to defaults.
        """
        if not isinstance(event_data, dict):
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: record_event received "
                f"non-dict payload: {type(event_data).__name__}"
            )
            return

        event_type = str(event_data.get("event_type") or "")
        if event_type not in VALID_EVENT_TYPES:
            logger.debug(
                f"CDPRecordingSession[{self._session_id}]: ignoring event "
                f"with unknown type: {event_type!r}"
            )
            return

        event = RecordedEvent(
            event_type=event_type,
            timestamp=float(event_data.get("timestamp") or time.time()),
            selector=event_data.get("selector"),
            value=event_data.get("value"),
            url=str(event_data.get("url") or self._last_page_url or ""),
            screenshot=event_data.get("screenshot"),
            page_title=str(event_data.get("page_title") or self._last_page_title or ""),
        )
        self._events.append(event)
        logger.debug(
            f"CDPRecordingSession[{self._session_id}]: recorded {event.event_type} "
            f"selector={event.selector!r} value={event.value!r}"
        )

    # ------------------------------------------------------------------
    # CDP attachment (private)
    # ------------------------------------------------------------------

    async def _attach_cdp(self, target: Any) -> bool:
        """Attach to a CDP source. Returns True on success."""
        if target is None:
            logger.error("CDPRecordingSession: start_recording target is None")
            return False

        # 1) PlaywrightMCPManager (use MCP tool to obtain a CDP endpoint)
        if hasattr(target, "call_tool") and hasattr(target, "session"):
            self._attached_manager = target
            try:
                result = await target.call_tool("browser_cdp_session", {})
                cdp_url = self._extract_cdp_url(result)
                if not cdp_url:
                    logger.error(
                        "PlaywrightMCPManager did not return a CDP URL via "
                        "browser_cdp_session tool."
                    )
                    return False
                self._cdp_url = cdp_url
                await self._open_websocket(cdp_url)
                return True
            except Exception as exc:  # noqa: BLE001 - PlaywrightMCP tool call 可能抛任何 MCP 错误
                logger.error(
                    f"Failed to obtain CDP URL from PlaywrightMCPManager: {exc}",
                    exc_info=True,
                )
                return False

        # 2) A raw CDP WebSocket URL string
        if isinstance(target, str):
            self._cdp_url = target
            try:
                await self._open_websocket(target)
                return True
            except Exception as exc:  # noqa: BLE001 - WebSocket connect 失败可能为 ConnectionError / OSError / InvalidHandshake
                logger.error(
                    f"Failed to connect to CDP WebSocket {target}: {exc}",
                    exc_info=True,
                )
                return False

        # 3) A Playwright Page (use new_cdp_session)
        if hasattr(target, "context") and hasattr(target, "evaluate"):
            try:
                cdp = await target.context.new_cdp_session(target)
                self._cdp_session = cdp
                cdp.on("Page.frameNavigated", self._on_page_frame_navigated)
                cdp.on("Page.loadEventFired", self._on_page_load_event_fired)
                cdp.on(
                    "Runtime.consoleAPICalled",
                    self._on_runtime_console_api_called,
                )
                # Capture the initial URL/title so first events are anchored
                try:
                    self._last_page_url = target.url or ""
                    self._last_page_title = await target.title() or ""
                except Exception:  # noqa: BLE001 - 首次 URL/title 抓取失败时静默回退
                    logger.debug(
                        f"CDPRecordingSession[{self._session_id}]: "
                        f"failed to capture initial URL/title"
                    )
                return True
            except Exception as exc:  # noqa: BLE001 - Playwright Page CDP 会话创建可能抛任何错误
                logger.error(
                    f"Failed to create CDP session from Playwright Page: {exc}",
                    exc_info=True,
                )
                return False

        logger.error(
            f"CDPRecordingSession: unsupported start_recording target: "
            f"{type(target).__name__}"
        )
        return False

    async def _open_websocket(self, cdp_url: str) -> None:
        """Open a websocket to a raw CDP URL and start a reader task."""
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "websockets package is required to connect to a raw CDP URL"
            ) from exc

        self._ws = await websockets.connect(cdp_url, max_size=32 * 1024 * 1024)
        self._reader_task = asyncio.create_task(
            self._read_ws_loop(), name=f"cdp-reader-{self._session_id}"
        )

    async def _read_ws_loop(self) -> None:
        """Read messages from the raw CDP websocket and dispatch to handlers."""
        if self._ws is None:
            return
        try:
            async for message in self._ws:
                try:
                    payload = _json.loads(message)
                except (ValueError, TypeError):
                    continue
                await self._dispatch_cdp_message(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - WS reader 循环结束后的清理异常
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: ws reader loop ended: {exc}"
            )

    async def _dispatch_cdp_message(self, payload: dict[str, Any]) -> None:
        """Route a parsed CDP message to the correct internal handler."""
        method = payload.get("method") or ""
        params = payload.get("params") or {}

        if method == "Page.frameNavigated":
            self._on_page_frame_navigated(params)
        elif method == "Page.loadEventFired":
            self._on_page_load_event_fired(params)
        elif method == "Runtime.consoleAPICalled":
            self._on_runtime_console_api_called(params)
        # Other methods are ignored intentionally

    @staticmethod
    def _extract_cdp_url(tool_result: dict[str, Any]) -> Optional[str]:
        """Best-effort extraction of a CDP URL from an MCP tool result."""
        if not isinstance(tool_result, dict):
            return None
        text = tool_result.get("text") or ""
        if isinstance(text, str) and text.startswith("ws"):
            return text.strip()
        for key in ("url", "wsUrl", "webSocketDebuggerUrl", "cdp_url"):
            val = tool_result.get(key)
            if isinstance(val, str) and val.startswith("ws"):
                return val
        return None

    # ------------------------------------------------------------------
    # CDP domain management (private)
    # ------------------------------------------------------------------

    async def _enable_domains(self) -> None:
        """Enable the CDP domains we listen on."""
        for domain in ("Page", "Runtime", "Network"):
            try:
                await self._send_cdp(f"{domain}.enable", {})
            except Exception as exc:  # noqa: BLE001 - 单个域启用失败不阻塞其他域
                logger.warning(
                    f"CDPRecordingSession[{self._session_id}]: failed to enable "
                    f"{domain}: {exc}"
                )

    async def _disable_domains(self) -> None:
        """Disable the CDP domains we previously enabled."""
        for domain in ("Page", "Runtime", "Network"):
            try:
                await self._send_cdp(f"{domain}.disable", {})
            except Exception as exc:  # noqa: BLE001 - 关闭域的 best-effort
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: disable {domain} "
                    f"failed (ignored): {exc}"
                )

    async def _install_page_recorder(self) -> None:
        """Install the JS recorder script on every new document and the current page."""
        try:
            await self._send_cdp(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _INJECT_RECORDER_SCRIPT},
            )
            # Also inject into the current page immediately (addScriptToEvaluateOnNewDocument
            # only applies to future navigations).
            await self._send_cdp(
                "Runtime.evaluate",
                {"expression": _INJECT_RECORDER_SCRIPT, "awaitPromise": False},
            )
        except Exception as exc:  # noqa: BLE001 - 注入 recorder 脚本失败时 recording 仍可继续
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: failed to install "
                f"page recorder script: {exc}"
            )

    async def _send_cdp(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a CDP command via whichever transport is active."""
        if self._cdp_session is not None:
            result = await self._cdp_session.send(method, params)
            return result if isinstance(result, dict) else {}

        if self._ws is not None:
            msg_id = self._next_msg_id()
            await self._ws.send(_json.dumps({"id": msg_id, "method": method, "params": params}))
            # For fire-and-forget methods we don't block on a response here;
            # responses are matched by id in _dispatch_cdp_message if needed.
            return {}

        raise RuntimeError("CDPRecordingSession: no active CDP transport")

    def _next_msg_id(self) -> int:
        """Allocate a monotonically increasing CDP message id."""
        self._msg_counter += 1
        return self._msg_counter

    async def _safe_detach(self) -> None:
        """Best-effort cleanup of CDP transport resources."""
        if self._reader_task is not None:
            try:
                self._reader_task.cancel()
                await asyncio.gather(self._reader_task, return_exceptions=True)
            except Exception as exc:  # noqa: BLE001 - 资源清理路径：task 取消本身可能抛 CancelledError
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: reader task "
                    f"cancel error (ignored): {exc}"
                )
            self._reader_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001 - WebSocket close 失败属于清理阶段
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: ws close "
                    f"error (ignored): {exc}"
                )
            self._ws = None

        if self._cdp_session is not None:
            try:
                detach = getattr(self._cdp_session, "detach", None)
                if callable(detach):
                    result = detach()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001 - Playwright detach 失败属于清理阶段
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: cdp detach "
                    f"error (ignored): {exc}"
                )
            self._cdp_session = None

        self._cdp_url = None
        self._attached_manager = None

    # ------------------------------------------------------------------
    # CDP event handlers (private)
    # ------------------------------------------------------------------

    def _on_page_frame_navigated(self, params: dict[str, Any]) -> None:
        """Handle Page.frameNavigated → record a navigation event."""
        try:
            frame = params.get("frame") or {}
            url = frame.get("url") or self._last_page_url
            self._last_page_url = url or ""
        except (AttributeError, TypeError):  # 防御：参数结构可能与预期不一致
            url = self._last_page_url
        self.record_event({
            "event_type": "navigation",
            "value": url,
            "url": url,
        })

    def _on_page_load_event_fired(self, params: dict[str, Any]) -> None:
        """Handle Page.loadEventFired → record a navigation event (load)."""
        self.record_event({
            "event_type": "navigation",
            "value": self._last_page_url,
            "url": self._last_page_url,
        })

    def _on_runtime_console_api_called(
        self, params: dict[str, Any]
    ) -> None:
        """Handle Runtime.consoleAPICalled → record user-interaction events
        emitted by the injected JS recorder (printed as ``__CDP_RECORDER__:...``).
        """
        try:
            args = params.get("args") or []
            for arg in args:
                value = arg.get("value")
                if not isinstance(value, str):
                    continue
                if not value.startswith("__CDP_RECORDER__:"):
                    continue
                payload = _json.loads(value[len("__CDP_RECORDER__:"):])
                rec_type = payload.pop("__recorder_type__", None)
                if rec_type not in VALID_EVENT_TYPES:
                    continue
                payload.setdefault("url", self._last_page_url)
                payload.setdefault("page_title", self._last_page_title)
                payload["event_type"] = rec_type
                self.record_event(payload)
        except (_json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.debug(
                f"CDPRecordingSession[{self._session_id}]: failed to parse "
                f"recorder console payload: {exc}"
            )
