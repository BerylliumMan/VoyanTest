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
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore[assignment,misc]
    ImageDraw = None  # type: ignore[assignment,misc]
    HAS_PIL = False

try:
    import pystray
    HAS_TRAY = HAS_PIL  # 托盘同时需要 pystray + Pillow
except ImportError:
    pystray = None  # type: ignore[assignment]
    HAS_TRAY = False

from agent.gui.config_store import ConfigStore

try:
    from agent.gui.config_dialog import ConfigDialog
except ImportError:
    ConfigDialog = None  # type: ignore[assignment]


# ── 托盘状态 / 图标生成 ───────────────────────────────────────────────

# 内部状态 key → 显示文案
_STATUS_DISPLAY = {
    "disconnected": "未连接",
    "connecting": "连接中…",
    "connected": "已连接",
    "idle": "已连接",
    "busy": "执行中",
    "error": "连接失败",
}

# 托盘着色（RGB 元组，避免 Pillow/Win32 对 hex/RGBA 处理不一致）
# 选用高饱和色，保证 Windows 任务栏 16×16 缩略后仍可辨认
_STATUS_RGB = {
    "disconnected": (142, 150, 160),  # 灰
    "connecting": (240, 180, 40),     # 黄
    "connected": (40, 190, 70),       # 绿
    "idle": (40, 190, 70),
    "busy": (40, 190, 70),
    "error": (230, 60, 55),           # 红
}


def normalize_status_key(value: Optional[str]) -> str:
    """将状态值归一化为英文 key（兼容中文显示值）。"""
    if not value:
        return "disconnected"
    if value in _STATUS_DISPLAY or value in _STATUS_RGB:
        return value
    for key, label in _STATUS_DISPLAY.items():
        if label == value:
            return key
    return "disconnected"


def tray_rgb_for_status(status: str) -> tuple:
    """返回托盘图标填充色 (R, G, B)。"""
    key = normalize_status_key(status)
    return _STATUS_RGB.get(key, _STATUS_RGB["disconnected"])


def _create_tray_image(status: str = "disconnected") -> "Image.Image":
    """生成 Agent 托盘图标，按连接状态着色。

    状态颜色：
        disconnected — 灰色（未连接）
        connecting   — 黄色（连接中）
        connected / idle / busy — 绿色（连接成功）
        error        — 红色（连接失败）
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 未安装，无法生成托盘图标")

    fill_color = tray_rgb_for_status(status)
    # Windows 托盘实际约 16×16；用 32×32 + 实心圆，缩略后颜色更稳
    size = 32
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = 1
    draw.ellipse(
        [margin, margin, size - margin - 1, size - margin - 1],
        fill=fill_color + (255,),
    )

    # 简易面孔（白色），占比较小，避免淹没状态色
    draw.ellipse([9, 10, 13, 14], fill=(255, 255, 255, 255))
    draw.ellipse([19, 10, 23, 14], fill=(255, 255, 255, 255))
    draw.rectangle([11, 19, 21, 21], fill=(255, 255, 255, 255))

    return image


# ── 托盘图标缓存 ───────────────────────────────────────────────────────
_TRAY_IMAGE_CACHE: dict = {}


def _get_tray_image(status: str) -> "Image.Image":
    """按状态取托盘图；返回副本，避免 Win32 因同一 Image 对象跳过刷新。"""
    key = normalize_status_key(status)
    if key not in _TRAY_IMAGE_CACHE:
        _TRAY_IMAGE_CACHE[key] = _create_tray_image(key)
    return _TRAY_IMAGE_CACHE[key].copy()


# ── 主应用类 ──────────────────────────────────────────────────────────

class AgentGUI:
    """VoyanTest Agent 主 GUI 应用。

    流程：加载配置 → 显示配置对话框 → 连接服务器 → 最小化到系统托盘。
    """

    # 兼容旧引用；权威映射见模块级 _STATUS_DISPLAY
    _STATUS_MAP = _STATUS_DISPLAY

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
        self._status_lock = threading.Lock()

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
                self._schedule_status_update("disconnected")

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
        with self._status_lock:
            raw = value if value is not None else self._status
        return normalize_status_key(raw)

    def _status_display(self, key: Optional[str] = None) -> str:
        resolved = self._resolve_status_key(key)
        return _STATUS_DISPLAY.get(resolved, resolved)

    def _schedule_status_update(self, status: str) -> None:
        """任意线程均可调用：把状态更新派发到 Tk 主线程（若可用）。"""
        key = normalize_status_key(status)
        root = self._root
        if root is not None:
            try:
                root.after(0, lambda k=key: self._update_status_and_tray(k))
                return
            except Exception:
                logger.debug("schedule status update via after() failed", exc_info=True)
        # 无 Tk 主循环时直接更新（含托盘跨线程赋值）
        self._update_status_and_tray(key)

    def _on_status_change(self, status: str) -> None:
        """AgentClient 状态变更回调（在 asyncio 线程中调用）。"""
        self._schedule_status_update(status)

    def _on_agent_log(self, level: str, message: str) -> None:
        """AgentClient 日志回调（在 asyncio 线程中调用）——写入线程安全缓冲区。"""
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append((level, message))

    def _update_status_and_tray(self, status_value: str) -> None:
        """更新状态文本、托盘标题、图标颜色、菜单文字。"""
        key = normalize_status_key(status_value)
        with self._status_lock:
            old_status = self._status
            self._status = key
        status_display = self._status_display(key)

        self._refresh_tray_icon(key)
        self._refresh_tray_title()

        icon = self._tray_icon
        if icon is not None and HAS_TRAY:
            try:
                # 动态菜单项用 callable 时需显式刷新
                if hasattr(icon, "update_menu"):
                    icon.update_menu()
                else:
                    icon.menu = self._build_tray_menu()
            except Exception:
                logger.debug("Failed to update tray menu", exc_info=True)

            if old_status == "disconnected" and key != "disconnected":
                try:
                    icon.notify(
                        f"VoyanTest Agent — {status_display}",
                        title="VoyanTest",
                    )
                except Exception:
                    pass

    def _refresh_tray_title(self) -> None:
        """刷新托盘图标的提示文本（线程安全）。"""
        icon = self._tray_icon
        if icon is not None and HAS_TRAY:
            try:
                icon.title = f"VoyanTest Agent — {self._status_display()}"
            except Exception:
                logger.debug("Failed to refresh tray title", exc_info=True)

    def _refresh_tray_icon(self, status_key: Optional[str] = None) -> None:
        """按当前状态刷新托盘图标颜色。

        每次赋值新的 Image 副本，避免 Win32 因同一对象引用跳过 NIM_MODIFY。
        """
        icon = self._tray_icon
        if icon is None or not HAS_TRAY:
            return
        key = normalize_status_key(
            status_key if status_key is not None else self._resolve_status_key()
        )
        try:
            icon.icon = _get_tray_image(key)
        except Exception:
            logger.debug("Failed to refresh tray icon", exc_info=True)

    # ── 系统托盘设置 ───────────────────────────────────────────────────

    def _build_tray_menu(self) -> "pystray.Menu":  # type: ignore[name-defined]
        """构建托盘菜单；文案用 callable，便于 update_menu 刷新。"""

        def status_text(_item: "pystray.MenuItem" = None) -> str:  # type: ignore[name-defined]
            return f"状态: {self._status_display()}"

        def connect_label(_item: "pystray.MenuItem" = None) -> str:  # type: ignore[name-defined]
            key = self._resolve_status_key()
            return "断开" if key not in ("disconnected", "error") else "连接"

        def on_connect_toggle(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:  # type: ignore[name-defined]
            key = self._resolve_status_key()
            if key not in ("disconnected", "error"):
                self._do_disconnect()
            else:
                self._show_config_window()

        return pystray.Menu(
            pystray.MenuItem(
                status_text,
                lambda *_: None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "显示窗口",
                lambda icon, item: self._show_config_window(),
                default=True,
            ),
            pystray.MenuItem(
                "查看日志",
                lambda icon, item: self._show_log_window(),
            ),
            pystray.MenuItem(
                connect_label,
                on_connect_toggle,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "退出",
                lambda icon, item: self._do_exit(),
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

        status_key = self._resolve_status_key()
        image = _get_tray_image(status_key)
        self._tray_icon = pystray.Icon(
            "voyantest-agent",
            image,
            f"VoyanTest Agent — {self._status_display(status_key)}",
            self._build_tray_menu(),
        )

        def _on_tray_ready(icon: "pystray.Icon") -> None:  # type: ignore[name-defined]
            # run() 就绪后再刷一次，避免初始化前的状态更新被吞掉
            icon.visible = True
            self._refresh_tray_icon(self._resolve_status_key())
            self._refresh_tray_title()
            if hasattr(icon, "update_menu"):
                try:
                    icon.update_menu()
                except Exception:
                    pass

        tray_thread = threading.Thread(
            target=lambda: self._tray_icon.run(setup=_on_tray_ready),
            daemon=True,
            name="tray-icon",
        )
        tray_thread.start()

        if self._root is not None:
            self._root.protocol("WM_DELETE_WINDOW", self._root.withdraw)
            self._root.mainloop()

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
        self._schedule_status_update("disconnected")

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
