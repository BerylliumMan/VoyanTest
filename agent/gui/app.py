"""VoyanTest Agent - GUI Application."""

import sys
import os
import threading
import asyncio
import logging
import collections
from typing import Optional

logger = logging.getLogger("agent.gui.app")

# ── 日志缓冲区（线程安全） ──────────────────────────────────────────────
_LOG_BUFFER = collections.deque(maxlen=1000)
_LOG_BUFFER_LOCK = threading.Lock()

# ── 可选依赖（导入失败时优雅降级） ─────────────────────────────────────

try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    ctk = None  # type: ignore[assignment]
    HAS_CTK = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    pystray = None  # type: ignore[assignment]
    HAS_TRAY = False

from agent.gui.config_store import ConfigStore

try:
    from agent.gui.config_dialog import ConfigDialog
except ImportError:
    ConfigDialog = None  # type: ignore[assignment]


# ── 托盘图标生成 ───────────────────────────────────────────────────────

def _create_tray_image(status: str = "disconnected") -> "Image.Image":
    """以程序方式生成 64×64 的 Agent 托盘图标，按连接状态着色。

    状态颜色：
        disconnected — 灰色（未连接）
        connecting   — 黄色（连接中）
        connected / idle / busy — 绿色（连接成功）
        error        — 红色（连接失败）
    """
    color_map = {
        "disconnected": "#6e7681",  # 灰
        "connecting": "#d29922",    # 黄
        "connected": "#2ea043",     # 绿
        "idle": "#2ea043",
        "busy": "#2ea043",
        "error": "#da3633",         # 红
    }
    fill_color = color_map.get(status, "#6e7681")
    accent_color = fill_color

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 圆形背景
    margin = 6
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=fill_color,
    )

    # 双眼（白色圆点）
    eye_y = 20
    draw.ellipse([18, eye_y, 26, eye_y + 8], fill="white")   # 左眼
    draw.ellipse([38, eye_y, 46, eye_y + 8], fill="white")   # 右眼

    # 嘴巴（白色矩形）
    mouth_y = 36
    draw.rectangle([24, mouth_y, 40, mouth_y + 4], fill="white")

    # 天线
    draw.ellipse([28, 4, 36, 12], fill=accent_color)

    return image


# ── 托盘图标缓存 ───────────────────────────────────────────────────────
_TRAY_IMAGE_CACHE: dict = {}


# ── 主应用类 ──────────────────────────────────────────────────────────

class AgentGUI:
    """VoyanTest Agent 主 GUI 应用。

    流程：加载配置 → 显示配置对话框 → 连接服务器 → 最小化到系统托盘。
    """

    # 状态中文映射（内部仍用英文 key 驱动托盘颜色）
    _STATUS_MAP = {
        "disconnected": "未连接",
        "connecting": "连接中…",
        "connected": "已连接",
        "idle": "已连接",
        "busy": "执行中",
        "error": "连接失败",
    }

    def __init__(
        self,
        config_store: Optional[ConfigStore] = None,
        config: Optional[dict] = None,
    ):
        self._config_store = config_store or ConfigStore()
        self._config = config or self._config_store.load()

        # Agent 相关状态
        self._agent: Optional["AgentClient"] = None         # type: ignore[name-defined]
        self._agent_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None   # 线程安全停止信号
        self._status: str = "disconnected"

        # 系统托盘
        self._tray_icon: Optional["pystray.Icon"] = None    # type: ignore[name-defined]

        # Bug 4 修复：防止重复弹出配置窗口
        self._config_window: Optional["ConfigDialog"] = None       # type: ignore[name-defined]
        self._log_window: Optional[ctk.CTkToplevel] = None

        # CTk 主窗口（隐藏，仅作为 ConfigDialog 的父窗口）
        self._root = None
        if HAS_CTK:
            self._root = ctk.CTk()
            self._root.title("VoyanTest Agent")
            self._root.withdraw()  # 初始隐藏

    # ── 公共入口 ───────────────────────────────────────────────────────

    def run(self) -> None:
        """启动 GUI 应用的主流程。阻塞直到用户退出。"""
        if not HAS_CTK:
            self._fatal("customtkinter 未安装，无法启动 GUI。请执行: pip install customtkinter")
            return

        if ConfigDialog is None:
            self._fatal("ConfigDialog 不可用，请检查 customtkinter 安装。")
            return

        # 1. 显示配置对话框（模态，阻塞直到用户操作）
        dialog = ConfigDialog(self._config_store, self._config)
        result = dialog.show()

        if not result:
            # 用户取消 — 清理并退出
            if self._root is not None:
                self._root.destroy()
            return

        # 2. 保存配置
        self._config = result
        self._config_store.save(result)

        # 3. 启动 AgentClient 到后台 asyncio 线程
        self._start_agent()

        # 4. 设置系统托盘（或回退到窗口模式）
        self._setup_tray()

    def _on_connect(self, config: dict) -> None:
        """处理配置对话框「连接」按钮点击的备用回调（当前由 dialog 内部处理）。"""
        pass

    # ── Agent 生命周期 ──────────────────────────────────────────────────

    def _start_agent(self) -> None:
        """在后台线程中启动 AgentClient 的 asyncio 事件循环。"""
        if self._agent_thread is not None and self._agent_thread.is_alive():
            logger.warning("Agent 已在运行中，跳过重复启动")
            return

        self._agent_thread = threading.Thread(
            target=self._run_agent_loop,
            daemon=True,
            name="agent-asyncio",
        )
        self._agent_thread.start()

    def _run_agent_loop(self) -> None:
        """后台线程：创建 asyncio 事件循环并执行 Agent 协程。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._agent_main())
        except Exception:
            logger.error("Agent 循环异常退出", exc_info=True)
        finally:
            self._agent = None
            self._loop.close()
            self._loop = None

    async def _agent_main(self) -> None:
        """AgentClient 主协程：连接、运行、带重连的无限循环。"""
        from agent.client_core import AgentClient

        config = self._config
        self._stop_event = asyncio.Event()

        agent = AgentClient(
            server_url=config.get("server_url", ""),
            agent_name=config.get("agent_name") or None,
            headless=config.get("headless", False),
            username=config.get("username") or None,
            password=config.get("password") or None,
            on_status_change=self._on_status_change,
            on_log=self._on_agent_log,
        )
        self._agent = agent

        # 重连循环：_stop_agent 设置 _stop_event 后退出
        reconnect_delay = 5
        while not self._stop_event.is_set():
            try:
                await agent.start()
            except Exception:
                logger.error("Agent 连接异常", exc_info=True)
                self._update_status_and_tray("disconnected")

            if self._stop_event.is_set():
                break

            # 可中断的等待（_stop_event.set() 立即唤醒）
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=reconnect_delay
                )
            except asyncio.TimeoutError:
                pass  # 超时 = 正常等待结束，继续重连

    def _stop_agent(self) -> None:
        """安全停止 AgentClient（跨线程 可重复调用）。"""
        # 1. 立即发出停止信号（asyncio.Event 线程安全）
        if self._stop_event is not None:
            self._stop_event.set()

        # 2. 调度 AgentClient.stop() 到后台事件循环
        agent = self._agent
        loop = self._loop
        if agent is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(agent.stop(), loop)
            except Exception:
                logger.debug("停止 Agent 时发生异常（可能已断开）")

        # 3. 释放引用，允许 _start_agent 创建新线程
        self._agent = None
        self._agent_thread = None
        # 注：不置 _stop_event = None，asyncio 线程可能仍在访问 .is_set()

    # ── 状态与托盘更新 ─────────────────────────────────────────────────

    def _resolve_status_key(self, value: Optional[str] = None) -> str:
        """将内部状态解析为英文 key（用于托盘颜色）。"""
        raw = value if value is not None else self._status
        if raw in self._STATUS_MAP:
            return raw
        for k, v in self._STATUS_MAP.items():
            if v == raw:
                return k
        return "disconnected"

    def _status_display(self, key: Optional[str] = None) -> str:
        resolved = self._resolve_status_key(key)
        return self._STATUS_MAP.get(resolved, resolved)

    def _on_status_change(self, status: str) -> None:
        """AgentClient 状态变更回调（在 asyncio 线程中调用）。"""
        key = self._resolve_status_key(status)
        self._status = key
        # 委托到主线程更新 UI
        if self._root is not None:
            self._root.after(0, lambda k=key: self._update_status_and_tray(k))

    def _on_agent_log(self, level: str, message: str) -> None:
        """AgentClient 日志回调（在 asyncio 线程中调用）——写入线程安全缓冲区。"""
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append((level, message))

    def _update_status_and_tray(self, status_value: str) -> None:
        """更新状态文本、托盘标题、图标颜色、菜单文字（主线程安全）。"""
        old_status = self._status
        key = self._resolve_status_key(status_value)
        self._status = key
        status_display = self._status_display(key)

        self._refresh_tray_icon()
        self._refresh_tray_title()

        if self._tray_icon is not None and HAS_TRAY:
            self._tray_icon.menu = self._build_tray_menu()
            if old_status == "disconnected" and key != "disconnected":
                try:
                    self._tray_icon.notify(
                        f"VoyanTest Agent — {status_display}",
                        title="VoyanTest",
                    )
                except Exception:
                    pass

    def _refresh_tray_title(self) -> None:
        """刷新托盘图标的提示文本（线程安全）。"""
        icon = self._tray_icon
        if icon is not None and HAS_TRAY:
            icon.title = f"VoyanTest Agent — {self._status_display()}"

    def _refresh_tray_icon(self) -> None:
        """按当前状态刷新托盘图标颜色。"""
        icon = self._tray_icon
        if icon is None or not HAS_TRAY:
            return
        status_key = self._resolve_status_key()
        cache_key = f"tray_{status_key}"
        if cache_key not in _TRAY_IMAGE_CACHE:
            _TRAY_IMAGE_CACHE[cache_key] = _create_tray_image(status_key)
        icon.icon = _TRAY_IMAGE_CACHE[cache_key]

    # ── 系统托盘设置 ───────────────────────────────────────────────────

    def _build_tray_menu(self) -> "pystray.Menu":  # type: ignore[name-defined]
        """根据连接状态动态构建托盘菜单。"""
        key = self._resolve_status_key()
        is_connected = key not in ("disconnected", "error")
        connect_label = "断开" if is_connected else "连接"
        return pystray.Menu(
            pystray.MenuItem(
                f"状态: {self._status_display(key)}",
                self._on_tray_action,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "显示窗口",
                self._on_tray_action,
                default=True,
            ),
            pystray.MenuItem(
                "查看日志",
                self._on_tray_action,
            ),
            pystray.MenuItem(
                connect_label,
                self._on_tray_action,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "退出",
                self._on_tray_action,
            ),
        )

    def _setup_tray(self) -> None:
        """创建系统托盘图标并进入 CTk 主事件循环。"""
        if not HAS_TRAY:
            logger.warning("pystray / Pillow 未安装，回退到窗口模式。请执行: pip install pystray pillow")
            if self._root is not None:
                self._root.deiconify()
                self._root.protocol("WM_DELETE_WINDOW", self._on_window_close)
                self._root.mainloop()
            return

        image = _create_tray_image(self._status)
        self._tray_icon = pystray.Icon(
            "voyantest-agent",
            image,
            f"VoyanTest Agent — {self._status}",
            self._build_tray_menu(),
        )

        tray_thread = threading.Thread(
            target=self._tray_icon.run,
            daemon=True,
            name="tray-icon",
        )
        tray_thread.start()

        if self._root is not None:
            self._root.protocol("WM_DELETE_WINDOW", self._root.withdraw)
            self._root.mainloop()

    # ── 托盘菜单回调 ───────────────────────────────────────────────────

    def _on_tray_action(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:  # type: ignore[name-defined]
        text = str(item.text)

        if text == "显示窗口":
            self._show_config_window()
        elif text == "查看日志":
            self._show_log_window()
        elif text == "断开":
            self._do_disconnect()
        elif text == "连接":
            self._show_config_window()
        elif text == "退出":
            self._do_exit()

    def _show_config_window(self) -> None:
        """在主线程显示配置对话框（Bug 4 修复：去重窗口）。"""
        # 如果已有配置窗口打开，聚焦它
        if self._config_window is not None:
            try:
                self._config_window.lift()
                self._config_window.focus_force()
            except Exception:
                self._config_window = None
            return

        if self._root is None:
            return

        def _show():
            self._config_window = ConfigDialog(self._config_store, self._config)
            result = self._config_window.show()
            self._config_window = None  # 关闭后重置
            if result is not None:
                self._config = result
                self._config_store.save(result)
                self._stop_agent()
                self._start_agent()

        self._root.after(0, _show)

    def _show_log_window(self) -> None:
        """在主线程显示日志查看窗口（C-4 修复：委托到主线程）。"""

        def _build():
            # 如果已有日志窗口打开，聚焦它
            if self._log_window is not None:
                try:
                    self._log_window.lift()
                    self._log_window.focus_force()
                except Exception:
                    self._log_window = None
                return

            if not HAS_CTK:
                return

            log_win = ctk.CTkToplevel(self._root)
            log_win.title("VoyanTest Agent — 日志")
            log_win.geometry("700x450")
            log_win.resizable(True, True)

            # 关闭时重置引用
            def _on_log_window_close():
                self._log_window = None
                log_win.destroy()

            log_win.protocol("WM_DELETE_WINDOW", _on_log_window_close)

            text_box = ctk.CTkTextbox(log_win, wrap="word", font=ctk.CTkFont(size=11))
            text_box.pack(fill="both", expand=True, padx=10, pady=10)
            text_box.configure(state="disabled")

            # 按钮栏
            btn_frame = ctk.CTkFrame(log_win, fg_color="transparent")
            btn_frame.pack(fill="x", padx=10, pady=(0, 10))
            ctk.CTkButton(
                btn_frame, text="刷新", width=80,
                command=lambda: self._refresh_log_text(text_box),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                btn_frame, text="清空", width=80,
                command=lambda: self._clear_log_buffer(text_box),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                btn_frame, text="关闭", width=80,
                command=_on_log_window_close,
            ).pack(side="right", padx=4)

            self._log_window = log_win
            self._refresh_log_text(text_box)

        self._root.after(0, _build)  # 初始加载

    @staticmethod
    def _refresh_log_text(text_box: "ctk.CTkTextbox") -> None:
        """从全局日志缓冲区刷新到文本框。"""
        with _LOG_BUFFER_LOCK:
            entries = list(_LOG_BUFFER)
        text_box.configure(state="normal")
        text_box.delete("1.0", "end")
        for level, msg in reversed(entries):  # 最新在前
            text_box.insert("1.0", f"[{level}] {msg}\n")
        text_box.configure(state="disabled")

    @staticmethod
    def _clear_log_buffer(text_box: "ctk.CTkTextbox") -> None:
        """清空日志缓冲区。"""
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.clear()
        text_box.configure(state="normal")
        text_box.delete("1.0", "end")
        text_box.configure(state="disabled")

    def _do_disconnect(self) -> None:
        """断开当前 Agent 连接。"""
        self._stop_agent()
        self._update_status_and_tray("disconnected")

    def _do_exit(self) -> None:
        self._stop_agent()

        def _cleanup():
            if self._config_window is not None:
                try:
                    self._config_window.destroy()
                except Exception:
                    pass
                self._config_window = None
            if self._log_window is not None:
                try:
                    self._log_window.destroy()
                except Exception:
                    pass
                self._log_window = None
            tray = self._tray_icon
            if tray is not None:
                tray.stop()
                self._tray_icon = None
            if self._root is not None:
                self._root.destroy()

        if self._root is not None:
            self._root.after(0, _cleanup)
        else:
            _cleanup()

    def _on_window_close(self) -> None:
        """窗口关闭按钮回调（无托盘模式下使用）。"""
        self._stop_agent()
        if self._root is not None:
            self._root.destroy()

    # ── 辅助方法 ───────────────────────────────────────────────────────

    @staticmethod
    def _fatal(message: str) -> None:
        """输出致命错误并退出。"""
        print(f"错误: {message}", file=sys.stderr)
        sys.exit(1)


# ── 入口 ───────────────────────────────────────────────────────────────

def main() -> None:
    """从命令行启动 GUI 应用。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = AgentGUI()
    app.run()


if __name__ == "__main__":
    main()
