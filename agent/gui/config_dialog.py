"""VoyanTest Agent 配置对话框 — CustomTkinter 配置表单。"""

import os
from typing import Optional

try:
    import customtkinter as ctk
except ImportError:
    ctk = None  # type: ignore[assignment]
    print("请安装 customtkinter: pip install customtkinter")


if ctk is not None:

    class ConfigDialog(ctk.CTkToplevel):
        """代理配置弹窗，用户可填写服务器连接信息并保存或直接连接。"""

        # ── 默认窗口几何 ──────────────────────────────────────────────
        _WINDOW_WIDTH = 500
        _WINDOW_HEIGHT = 470
        _TITLE = "VoyanTest Agent 配置"

        def __init__(
            self,
            config_store,  # : ConfigStore
            initial_config: Optional[dict] = None,
            on_connect: Optional[callable] = None,
        ):
            super().__init__()
            self._config_store = config_store
            self._on_connect = on_connect
            self._result: Optional[dict] = None

            # 加载初始配置（传入优先，否则从 store 读取）
            if initial_config is not None:
                initial = dict(initial_config)
            else:
                initial = self._config_store.load()

            # ── 窗口属性 ─────────────────────────────────────────────
            self.title(self._TITLE)
            self.resizable(True, True)
            self.minsize(self._WINDOW_WIDTH, self._WINDOW_HEIGHT)
            self.geometry(f"{self._WINDOW_WIDTH}x{self._WINDOW_HEIGHT}")
            self._center_window()

            # wait_window() 已提供模态效果，不设 grab_set 以支持最小化

            # ── 布局 ─────────────────────────────────────────────────
            self.grid_columnconfigure(0, weight=0)  # label column
            self.grid_columnconfigure(1, weight=1)  # entry column
            pad_opts = {"padx": (20, 10), "pady": (8, 4), "sticky": "w"}

            row = 0
            # ---- 标题 ----
            title = ctk.CTkLabel(
                self, text="服务器连接配置", font=ctk.CTkFont(size=16, weight="bold")
            )
            title.grid(row=row, column=0, columnspan=2, padx=20, pady=(16, 12), sticky="w")
            row += 1

            # ---- 服务器地址 ----
            ctk.CTkLabel(self, text="服务器地址").grid(row=row, **pad_opts)
            self._server_url_entry = ctk.CTkEntry(
                self,
                placeholder_text="ws://192.168.1.100:8002",
                width=320,
            )
            self._server_url_entry.insert(0, initial.get("server_url", ""))
            self._server_url_entry.grid(row=row, column=1, padx=(0, 20), pady=(8, 4), sticky="ew")
            row += 1

            # ---- Agent 名称 ----
            ctk.CTkLabel(self, text="Agent 名称").grid(row=row, **pad_opts)
            self._agent_name_entry = ctk.CTkEntry(
                self,
                placeholder_text="留空自动生成",
                width=320,
            )
            self._agent_name_entry.insert(0, initial.get("agent_name", ""))
            self._agent_name_entry.grid(row=row, column=1, padx=(0, 20), pady=(8, 4), sticky="ew")
            row += 1

            # ---- 用户名 ----
            ctk.CTkLabel(self, text="用户名").grid(row=row, **pad_opts)
            self._username_entry = ctk.CTkEntry(
                self,
                placeholder_text="登录用户名",
                width=320,
            )
            self._username_entry.insert(0, initial.get("username", ""))
            self._username_entry.grid(row=row, column=1, padx=(0, 20), pady=(8, 4), sticky="ew")
            row += 1

            # ---- 密码 ----
            ctk.CTkLabel(self, text="密码").grid(row=row, **pad_opts)
            self._password_entry = ctk.CTkEntry(
                self,
                placeholder_text="登录密码",
                show="*",
                width=320,
            )
            self._password_entry.insert(0, initial.get("password", ""))
            self._password_entry.grid(row=row, column=1, padx=(0, 20), pady=(8, 4), sticky="ew")
            row += 1

            # ── 分隔线 ───────────────────────────────────────────────
            sep = ctk.CTkLabel(self, text="")
            sep.grid(row=row, column=0, columnspan=2, pady=(4, 2))
            row += 1

            # ---- 无头模式 ----
            self._headless_var = ctk.BooleanVar(value=bool(initial.get("headless", False)))
            ctk.CTkSwitch(
                self, text="启用无头模式（不显示浏览器窗口）", variable=self._headless_var
            ).grid(row=row, column=0, columnspan=2, padx=20, pady=(4, 6), sticky="w")
            row += 1

            # ---- 自动连接 ----
            self._auto_connect_var = ctk.BooleanVar(value=bool(initial.get("auto_connect", False)))
            ctk.CTkSwitch(
                self, text="启动时自动连接", variable=self._auto_connect_var
            ).grid(row=row, column=0, columnspan=2, padx=20, pady=(4, 8), sticky="w")
            row += 1

            # ── 按钮 ─────────────────────────────────────────────────
            btn_frame = ctk.CTkFrame(self, fg_color="transparent")
            btn_frame.grid(row=row, column=0, columnspan=2, padx=20, pady=(12, 20), sticky="ew")
            btn_frame.grid_columnconfigure((0, 1, 2), weight=1)

            self._connect_btn = ctk.CTkButton(
                btn_frame,
                text="连接",
                fg_color="#2ea043",
                hover_color="#3fb950",
                command=self._on_connect_click,
                width=110,
            )
            self._connect_btn.grid(row=0, column=0, padx=4)

            self._save_btn = ctk.CTkButton(
                btn_frame,
                text="保存",
                command=self._on_save_click,
                width=110,
            )
            self._save_btn.grid(row=0, column=1, padx=4)

            self._cancel_btn = ctk.CTkButton(
                btn_frame,
                text="取消",
                fg_color="gray40",
                hover_color="gray50",
                command=self._on_cancel_click,
                width=110,
            )
            self._cancel_btn.grid(row=0, column=2, padx=4)

            # 关闭窗口时等同取消
            self.protocol("WM_DELETE_WINDOW", self._on_cancel_click)

        # ── 公共方法 ──────────────────────────────────────────────────

        def show(self) -> Optional[dict]:
            """阻塞直到用户点击 连接/取消。返回配置 dict 或 None。"""
            self.wait_window()
            return self._result

        # ── 内部方法 ──────────────────────────────────────────────────

        def _gather_config(self) -> dict:
            """从所有表单控件收集当前配置。"""
            return {
                "server_url": self._server_url_entry.get().strip(),
                "agent_name": self._agent_name_entry.get().strip(),
                "username": self._username_entry.get().strip(),
                "password": self._password_entry.get(),
                "headless": self._headless_var.get(),
                "auto_connect": self._auto_connect_var.get(),
            }

        def _on_connect_click(self) -> None:
            """连接按钮：校验服务器地址非空，触发回调，返回配置。"""
            server_url = self._server_url_entry.get().strip()
            if not server_url:
                self._flash_entry(self._server_url_entry)
                return

            config = self._gather_config()

            if self._on_connect is not None:
                self._on_connect(config)

            self._result = config
            self.destroy()

        def _on_save_click(self) -> None:
            """保存按钮：将当前配置写入 ConfigStore。"""
            config = self._gather_config()
            self._config_store.save(config)
            # 闪烁保存按钮以提供视觉反馈
            self._flash_button(self._save_btn, "#3a7bd5")

        def _on_cancel_click(self) -> None:
            """取消按钮：返回 None。"""
            self._result = None
            self.destroy()

        # ── 辅助方法 ──────────────────────────────────────────────────

        def _flash_entry(self, entry: "ctk.CTkEntry") -> None:
            """短暂改变 Entry 边框颜色以提示校验失败。"""
            original = entry.cget("border_color")
            entry.configure(border_color="red")
            self.after(800, lambda: entry.configure(border_color=original))

        def _flash_button(self, btn: "ctk.CTkButton", color: str, duration: int = 400) -> None:
            """短暂改变按钮颜色以提供反馈。"""
            original = btn.cget("fg_color")
            btn.configure(fg_color=color)
            self.after(duration, lambda: btn.configure(fg_color=original))

        def _center_window(self) -> None:
            """将窗口居中放置在屏幕上。"""
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = (sw - self._WINDOW_WIDTH) // 2
            y = (sh - self._WINDOW_HEIGHT) // 2
            self.geometry(f"+{x}+{y}")

else:
    # customtkinter 未安装时的占位类，允许 import 不崩溃
    class ConfigDialog:  # type: ignore[no-redef]
        """占位：customtkinter 未安装。请执行 pip install customtkinter。"""

        def __init__(self, *args, **kwargs):
            raise ImportError(
                "customtkinter 未安装，无法创建配置对话框。"
                "请执行: pip install customtkinter"
            )

        def show(self) -> None:
            raise ImportError(
                "customtkinter 未安装，无法显示配置对话框。"
                "请执行: pip install customtkinter"
            )
