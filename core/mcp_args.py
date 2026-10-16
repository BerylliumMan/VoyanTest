"""MCP 参数构造共享模块（025-ref-click）。

Server 端（core/playwright_manager.py）与客户端（agent/client_core.py）
共用同一套 action→MCP 工具参数映射，消除两份实现的行为漂移。

契约: specs/025-ref-click/contracts/ws-mcp-contract.md C2
关键事实（@playwright/mcp 0.0.77+ 源码核实）:
- 交互工具参数为 elementSchema: {element?: str, target: str}
- target 匹配 ^(f\\d+)?e\\d+$ 时按快照 ref 定位，否则按 CSS selector 定位
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

# 与 @playwright/mcp tab.ts targetLocator 一致的 ref 判定正则
_REF_RE = re.compile(r"^(f\d+)?e\d+$")

# LLM 简短 action 名 → MCP 工具名（服务端 AgentRunner 与客户端 AgentBridge 共用的
# 单一来源；新增动作只改这里 + build_mcp_args，两个执行路径自动保持一致）
TOOL_MAP = {
    "goto": "browser_navigate",
    "navigate": "browser_navigate",
    "click": "browser_click",
    "double_click": "browser_click",
    "right_click": "browser_click",
    "fill": "browser_type",
    "select": "browser_select_option",
    "wait": "browser_wait_for",
    "screenshot": "browser_take_screenshot",
    "snapshot": "browser_snapshot",
    "assert_text": "browser_wait_for",
    "press_key": "browser_press_key",
    "hover": "browser_hover",
    "evaluate": "browser_evaluate",
    "drag": "browser_drag",
    "scroll": "browser_mouse_wheel",
    "check": "browser_click",
    "uncheck": "browser_click",
    "dialog": "browser_handle_dialog",
    "upload": "browser_file_upload",
    "drop": "browser_drop",
    "tabs": "browser_tabs",
    "navigate_back": "browser_navigate_back",
    "click_xy": "browser_mouse_click_xy",
    "move_mouse": "browser_mouse_move_xy",
    "drag_xy": "browser_mouse_drag_xy",
    "console": "browser_console_messages",
    "highlight": "browser_highlight",
    "hide_highlight": "browser_hide_highlight",
    "find": "browser_find",
    # ── B1 动作面全量接线（2026-10-14）：MCP 已暴露但此前未接线的工具 ──
    "fill_form": "browser_fill_form",
    "network": "browser_network_requests",
    "network_request": "browser_network_request",
    "resize": "browser_resize",
    "emulate_media": "browser_emulate_media",
    "mouse_down": "browser_mouse_down",
    "mouse_up": "browser_mouse_up",
    "close": "browser_close",
    "pdf": "browser_pdf_save",
    "video_start": "browser_start_video",
    "video_stop": "browser_stop_video",
    "video_chapter": "browser_video_chapter",
    "video_show_actions": "browser_video_show_actions",
    "video_hide_actions": "browser_video_hide_actions",
    "trace_start": "browser_start_tracing",
    "trace_stop": "browser_stop_tracing",
    "recording_start": "browser_start_recording",
    "recording_stop": "browser_stop_recording",
    "run_code": "browser_run_code_unsafe",
    # ── B5 红线面/试验接入（2026-10-15）：caps=testing/network/storage ──
    # verify 四件套（read-only 断言，默认可用）
    "verify_visible": "browser_verify_element_visible",
    "verify_text": "browser_verify_text_visible",
    "verify_value": "browser_verify_value",
    "verify_list": "browser_verify_list_visible",
    # 网络模拟（红线：默认关，exec_network_mock_enabled 放行）
    "route": "browser_route",
    "route_list": "browser_route_list",
    "unroute": "browser_unroute",
    "network_state": "browser_network_state_set",
    # 存储操作（红线：默认关，exec_storage_actions_enabled 放行）
    "cookie_list": "browser_cookie_list",
    "cookie_get": "browser_cookie_get",
    "cookie_set": "browser_cookie_set",
    "cookie_delete": "browser_cookie_delete",
    "cookie_clear": "browser_cookie_clear",
    "localstorage_list": "browser_localstorage_list",
    "localstorage_get": "browser_localstorage_get",
    "localstorage_set": "browser_localstorage_set",
    "localstorage_delete": "browser_localstorage_delete",
    "localstorage_clear": "browser_localstorage_clear",
    "sessionstorage_list": "browser_sessionstorage_list",
    "sessionstorage_get": "browser_sessionstorage_get",
    "sessionstorage_set": "browser_sessionstorage_set",
    "sessionstorage_delete": "browser_sessionstorage_delete",
    "sessionstorage_clear": "browser_sessionstorage_clear",
    "storage_state_save": "browser_storage_state",
    "storage_state_restore": "browser_set_storage_state",
    # 036：页面 API 调用（在页面上下文经 evaluate 注入 fetch；分发由各链路 fetch 分支处理）
    "fetch": "browser_evaluate",
}

ACTION_DESCRIPTIONS = {
    "goto": "导航到指定 URL",
    "click": "点击页面元素",
    "double_click": "双击页面元素",
    "right_click": "右键点击页面元素（打开上下文菜单）",
    "fill": "在输入框中输入文本",
    "select": "选择下拉选项",
    "wait": "等待文本出现或指定时间",
    "screenshot": "截取页面截图",
    "snapshot": "刷新页面可访问性快照",
    "evaluate": "在浏览器中执行 JavaScript",
    "press_key": "发送按键或组合键（Enter/Escape/Control+A…）",
    "hover": "鼠标悬停到元素上",
    "drag": "把元素拖拽到另一个元素（selector=起点 ref，value=终点 ref）",
    "scroll": "滚动页面（value=像素数或 up/down）",
    "check": "勾选复选框（点击切换；模型需先确认当前勾选状态）",
    "uncheck": "取消勾选复选框（点击切换）",
    "dialog": "处理浏览器对话框（value=accept/dismiss，prompt 可 accept:文本）",
    "upload": "上传文件（selector=文件输入框 ref，value=绝对路径，多个用逗号分隔）",
    "drop": "把本地文件拖放到目标元素（selector=目标 ref，value=绝对路径）",
    "tabs": "管理标签页（value=list/new/select:N/close[:N]）",
    "navigate_back": "浏览器后退",
    "click_xy": "按坐标点击（value=\"x,y\"，用于 canvas）",
    "move_mouse": "移动鼠标到坐标（value=\"x,y\"）",
    "drag_xy": "按坐标拖拽（value=\"startX,startY,endX,endY\"）",
    "fill_form": "一次批量填写多个表单字段（value=JSON 数组，见任务提示词示例）",
    "network": "列出网络请求证据（value=可选过滤正则 或 \"static\"）",
    "network_request": "查看单条请求详情（value=序号 或 \"序号:response-body\"）",
    "resize": "调整浏览器视口大小（value=\"宽x高\"，如 1280x720）",
    "emulate_media": "模拟 CSS 媒体特性（value=dark/light，或 JSON：media=print 等）",
    "mouse_down": "按下鼠标按键（value=left/right/middle，默认 left）",
    "mouse_up": "释放鼠标按键（value=left/right/middle，默认 left）",
    "close": "关闭当前页面/标签页（弹窗清理）",
    "pdf": "把当前页面导出为 PDF（value=可选文件名）",
    "video_start": "开始录制视频（value=可选文件名或 JSON：size/fps/cursor）",
    "video_stop": "停止录制视频",
    "video_chapter": "在视频中插入章节标记（value=标题；或 JSON：description/duration）",
    "video_show_actions": "视频中显示动作标注（value=可选 JSON：position/duration/cursor/style）",
    "video_hide_actions": "视频中隐藏动作标注",
    "trace_start": "开始 Playwright 追踪（trace）",
    "trace_stop": "停止追踪并落盘 trace 文件",
    "recording_start": "开始录制（Playwright recording）",
    "recording_stop": "停止录制",
    "run_code": "在 Playwright 服务端执行 JS 代码片段（红线段：默认关闭，需配置放行）",
    # B5：verify 四件套（Playwright 原生断言，read-only，默认可用）
    "verify_visible": "断言元素可见（role=角色如 button/heading，value=可访问名）",
    "verify_text": "断言文本可见（value=期望文本；比 assert_text 更严格的原生断言）",
    "verify_value": "断言输入值（selector=ref，value=期望值；checkbox 用 true/false）",
    "verify_list": "断言列表内容（selector=列表 ref，value=JSON 数组的期望项）",
    # B5：网络模拟（红线段：默认关闭）
    "route": "Mock 网络请求（value=JSON：pattern/status/body/contentType；红线段默认关）",
    "route_list": "列出当前生效的网络 mock 路由",
    "unroute": "移除网络 mock（value=可选 pattern，缺省移除全部）",
    "network_state": "设置网络状态（value=offline|online；红线段默认关）",
    # B5：存储操作（红线段：默认关闭）
    "cookie_list": "列出 cookies（value=可选 JSON：domain/path 过滤）",
    "cookie_get": "读取指定 cookie（value=名称）",
    "cookie_set": "设置 cookie（value=JSON：name/value/domain/path/...）",
    "cookie_delete": "删除指定 cookie（value=名称）",
    "cookie_clear": "清空全部 cookies",
    "localstorage_list": "列出 localStorage 全部键值",
    "localstorage_get": "读取 localStorage 项（value=key）",
    "localstorage_set": "设置 localStorage 项（value=JSON：key/value）",
    "localstorage_delete": "删除 localStorage 项（value=key）",
    "localstorage_clear": "清空 localStorage",
    "sessionstorage_list": "列出 sessionStorage 全部键值",
    "sessionstorage_get": "读取 sessionStorage 项（value=key）",
    "sessionstorage_set": "设置 sessionStorage 项（value=JSON：key/value）",
    "sessionstorage_delete": "删除 sessionStorage 项（value=key）",
    "sessionstorage_clear": "清空 sessionStorage",
    "storage_state_save": "保存存储状态（cookies+localStorage）到文件（value=可选文件名）",
    "storage_state_restore": "从文件恢复存储状态（value=文件名）",
    # 036：页面 API 调用（系统级窗口/下载受阻时的替代路径；报告会如实标注「以 API 方式完成」）
    "fetch": "在页面上下文调用页面 API（value=JSON：method/url/headers?/body?/evidence?/filename?；自动携带页面会话；evidence=true 时响应体存为报告证据）",
}


def is_ref_selector(selector: Optional[str]) -> bool:
    """判定 selector 是否为 MCP 快照 ref（如 e12 / f1e2）。"""
    if not selector or not isinstance(selector, str):
        return False
    return bool(_REF_RE.match(selector))


def resolve_mcp_tool(action: str) -> str:
    """action → MCP 工具名；已是 browser_ 前缀的原样返回。"""
    if not action:
        return ""
    lowered = action.lower()
    if lowered in ("error", "done"):
        return ""
    if action.startswith("browser_"):
        return action
    return TOOL_MAP.get(action, action)


_NETWORK_MOCK_ACTIONS = frozenset({"route", "unroute", "network_state"})
_STORAGE_GATED_ACTIONS = frozenset({
    "cookie_list", "cookie_get", "cookie_set", "cookie_delete", "cookie_clear",
    "localstorage_list", "localstorage_get", "localstorage_set",
    "localstorage_delete", "localstorage_clear",
    "sessionstorage_list", "sessionstorage_get", "sessionstorage_set",
    "sessionstorage_delete", "sessionstorage_clear",
    "storage_state_save", "storage_state_restore",
})


def action_disabled_reason(action: str) -> Optional[str]:
    """B1/B5：配置门控的动作在派发前的拦截原因（放行返回 None）。

    - run_code（browser_run_code_unsafe，RCE 等价）：exec_run_code_enabled（默认 False）；
    - 网络模拟（route/unroute/network_state）：exec_network_mock_enabled（默认 False）；
    - 存储操作（cookie/localstorage/sessionstorage/storage_state）：
      exec_storage_actions_enabled（默认 False）。
    客户端进程读不到配置时保守拒绝（派发拦截在服务端 manager，客户端只需执行）。
    """
    name = str(action or "").strip().lower().replace("browser_", "")
    try:
        from app.config import get_settings

        s = get_settings()
    except Exception:  # 客户端进程无 app 配置 → 保守拒绝（由服务端决定是否放行）
        if name == "run_code":
            return "run_code 未启用（当前执行环境未配置放行）"
        if name in _NETWORK_MOCK_ACTIONS:
            return "网络模拟动作未启用（当前执行环境未配置放行）"
        if name in _STORAGE_GATED_ACTIONS:
            return "存储操作未启用（当前执行环境未配置放行）"
        return None
    if name == "run_code":
        if bool(getattr(s, "exec_run_code_enabled", False)):
            return None
        return "run_code 未启用（系统设置 exec_run_code_enabled，默认关闭：该动作可在 Playwright 服务端执行任意代码）"
    if name in _NETWORK_MOCK_ACTIONS:
        if bool(getattr(s, "exec_network_mock_enabled", False)):
            return None
        return "网络模拟动作未启用（系统设置 exec_network_mock_enabled，默认关闭：mock/断网会改变被测系统行为）"
    if name in _STORAGE_GATED_ACTIONS:
        if bool(getattr(s, "exec_storage_actions_enabled", False)):
            return None
        return "存储操作未启用（系统设置 exec_storage_actions_enabled，默认关闭：直接读写 cookies/存储可能伪造登录态）"
    return None


def _parse_json_object(raw: Optional[str]) -> dict:
    """宽松解析 JSON 对象（失败返回 {}）。"""
    try:
        obj = json.loads(raw or "")
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


_FILL_FORM_TYPES = {"textbox", "checkbox", "radio", "combobox", "slider"}


def _parse_fill_form_fields(raw: Optional[str]) -> list[dict]:
    """解析 fill_form 的 value → browser_fill_form 的 fields 数组。

    标准形态：``[{"target":"e11","name":"用户名","type":"textbox","value":"admin"}, ...]``
    简写容忍：``{"e11": "admin", "e13": {"value":"***","type":"textbox"}}``
    """
    token = (raw or "").strip()
    if not token:
        return []
    try:
        data = json.loads(token)
    except (ValueError, TypeError):
        return []

    def _entry(target: str, item: Any) -> Optional[dict]:
        target = str(target or "").strip()
        if not target:
            return None
        if isinstance(item, dict):
            ftype = str(item.get("type") or "textbox").strip().lower()
            return {
                "target": target,
                "name": str(item.get("name") or target)[:120],
                "type": ftype if ftype in _FILL_FORM_TYPES else "textbox",
                "value": str(item.get("value") if item.get("value") is not None else ""),
            }
        return {"target": target, "name": target[:120], "type": "textbox", "value": str(item)}

    fields: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            entry = _entry(item.get("target") or item.get("ref") or "", item)
            if entry:
                fields.append(entry)
    elif isinstance(data, dict):
        for key, item in data.items():
            entry = _entry(key, item)
            if entry:
                fields.append(entry)
    return fields


def build_mcp_args(
    action: str,
    *,
    selector: Optional[str],
    element_desc: Optional[str] = None,
    value: Optional[str] = None,
    timeout_ms: int = 30000,
    wait_mode: Optional[str] = None,
    role: Optional[str] = None,
    field_type: Optional[str] = None,
) -> dict:
    """按 contracts C2 构造 MCP 工具参数。

    element 为人类可读描述（权限确认用），缺失时回退空字符串；
    target 原样传递 ref 或 CSS 选择器，不做转义改写；粘贴整行候选
    时提取其中的裸 ref token。
    """
    from core.locator_candidates import extract_ref_token
    sel = extract_ref_token(selector or "") or ""
    desc = (element_desc or "").strip()

    if action in ("goto", "navigate", "browser_navigate"):
        return {"url": value or "about:blank"}
    if action == "click":
        return {"element": desc, "target": sel}
    if action in ("double_click", "browser_click_double"):
        return {"element": desc, "target": sel, "doubleClick": True}
    if action in ("right_click", "context_click", "browser_click_right"):
        return {"element": desc, "target": sel, "button": "right"}
    if action in ("drag", "browser_drag"):
        end = extract_ref_token((value or "").strip()) or (value or "").strip()
        return {
            "startElement": desc,
            "startTarget": sel,
            "endElement": "",
            "endTarget": end,
        }
    if action in ("scroll", "browser_mouse_wheel"):
        token = (value or "").strip().lower()
        if token == "up":
            dy = -600
        elif token in ("", "down"):
            dy = 600
        else:
            dy = int(token) if token.lstrip("-").isdigit() else 600
        return {"deltaX": 0, "deltaY": dy}
    if action in ("check", "uncheck", "browser_check", "browser_uncheck"):
        return {"element": desc, "target": sel}
    if action in ("dialog", "handle_dialog", "browser_handle_dialog"):
        token = (value or "").strip()
        lowered = token.lower()
        accept = lowered not in ("dismiss", "cancel", "false", "取消", "拒绝")
        args: dict = {"accept": accept}
        if accept and lowered.startswith("accept:") and len(token) > len("accept:"):
            args["promptText"] = token[len("accept:"):].strip()
        return args
    if action in ("upload", "browser_file_upload"):
        paths = [p.strip() for p in (value or "").split(",") if p.strip()]
        return {"paths": paths}
    if action in ("drop", "browser_drop"):
        paths = [p.strip() for p in (value or "").split(",") if p.strip()]
        return {"element": desc, "target": sel, "paths": paths}
    if action in ("tabs", "browser_tabs"):
        token = (value or "list").strip().lower()
        if token.startswith("new"):
            return {"action": "new"}
        if token.startswith("close"):
            idx = re.sub(r"\D", "", token)
            return {"action": "close", **({"index": int(idx)} if idx else {})}
        if token.startswith("select"):
            idx = re.sub(r"\D", "", token) or "0"
            return {"action": "select", "index": int(idx)}
        return {"action": "list"}
    if action in ("navigate_back", "back", "browser_navigate_back"):
        return {}
    if action in ("click_xy", "browser_mouse_click_xy"):
        nums = [int(n) for n in re.findall(r"-?\d+", value or "")]
        if len(nums) >= 2:
            return {"x": nums[0], "y": nums[1]}
        return {}
    if action in ("move_mouse", "browser_mouse_move_xy"):
        nums = [int(n) for n in re.findall(r"-?\d+", value or "")]
        if len(nums) >= 2:
            return {"x": nums[0], "y": nums[1]}
        return {}
    if action in ("drag_xy", "browser_mouse_drag_xy"):
        nums = [int(n) for n in re.findall(r"-?\d+", value or "")]
        if len(nums) >= 4:
            return {"startX": nums[0], "startY": nums[1], "endX": nums[2], "endY": nums[3]}
        return {}
    if action == "fill":
        return {"element": desc, "target": sel, "text": value or ""}
    if action == "select":
        return {
            "element": desc,
            "target": sel,
            "values": [value] if value else [],
        }
    if action == "wait":
        _wv = str(value).strip() if value is not None else ""
        if _wv.isdigit():
            return {"time": int(_wv)}
        # 032 US4：负向等待（等待【X】消失）→ textGone；出现 → text（互斥）
        if (wait_mode or "").strip().lower() == "gone":
            return {"textGone": value or ""}
        return {"text": value or ""}
    if action == "console":
        # 032 US2：console 错误证据（收尾采集一次；error 级含更严重的 pageerror）
        return {"level": "error", "all": True}
    if action == "highlight":
        # 032 US3：失败目标高亮（元素级截图前尽力而为）
        return {"element": desc or "", "target": sel or ""}
    if action == "hide_highlight":
        return {}
    if action == "find":
        # 032 US6：快照定向查找（text 子串匹配；regex 形式以 /pattern/ 传入）
        token = (value or "").strip()
        if token.startswith("/") and token.endswith("/") and len(token) > 2:
            return {"regex": token}
        return {"text": token}
    if action == "screenshot":
        if sel or desc:
            # 032 US3：元素级截图（与 fullPage 互斥；target 支持 ref/CSS）
            return {
                "element": desc or "",
                "target": sel or "",
                "filename": value or f"screenshot_{int(time.time())}.png",
                "type": "png",
            }
        return {
            "filename": value or f"screenshot_{int(time.time())}.png",
            "fullPage": True,
            "type": "png",
        }
    if action == "snapshot":
        return {}
    if action == "assert_text":
        return {"text": value or ""}
    if action in ("press_key", "browser_press_key"):
        return {"key": value or "Escape"}
    if action in ("hover", "browser_hover"):
        return {"element": desc, "target": sel}
    if action in ("evaluate", "browser_evaluate"):
        fn = (value or sel or "").strip()
        if fn and not fn.lstrip().startswith(("(", "function", "async")):
            if "return" not in fn:
                fn = f"() => {{ return Boolean(({fn})); }}"
            else:
                fn = f"() => {{ {fn} }}"
        return {"function": fn or "() => true"}
    # ── B1 动作面全量接线（2026-10-14）──────────────────────────────────────
    if action == "fill_form":
        return {"fields": _parse_fill_form_fields(value)}
    if action == "network":
        token = (value or "").strip()
        if token.lower() == "static":
            return {"static": True}
        if token:
            return {"static": False, "filter": token}
        return {"static": False}
    if action == "network_request":
        token = (value or "").strip()
        idx_part, _, part = token.partition(":")
        m = re.search(r"\d+", idx_part)
        if not m:
            return {}
        args: dict = {"index": int(m.group(0))}
        part = part.strip().lower()
        if part in ("request-headers", "request-body", "response-headers", "response-body"):
            args["part"] = part
        return args
    if action == "resize":
        nums = [int(n) for n in re.findall(r"\d+", value or "")]
        if len(nums) >= 2:
            return {"width": nums[0], "height": nums[1]}
        return {}
    if action == "emulate_media":
        token = (value or "").strip()
        if token.startswith("{"):
            return _parse_json_object(token)
        if token in ("light", "dark"):
            return {"colorScheme": token}
        if token in ("screen", "print"):
            return {"media": token}
        if token in ("reduce", "no-preference"):
            return {"reducedMotion": token}
        return {}
    if action in ("mouse_down", "browser_mouse_down"):
        return {"button": (value or "left").strip().lower() or "left"}
    if action in ("mouse_up", "browser_mouse_up"):
        return {"button": (value or "left").strip().lower() or "left"}
    if action in ("close", "browser_close"):
        return {}
    if action == "pdf":
        token = (value or "").strip()
        return {"filename": token} if token else {}
    if action == "video_start":
        token = (value or "").strip()
        if token.startswith("{"):
            return _parse_json_object(token)
        return {"filename": token} if token else {}
    if action == "video_stop":
        return {}
    if action == "video_chapter":
        token = (value or "").strip()
        if token.startswith("{"):
            return _parse_json_object(token)
        return {"title": token} if token else {}
    if action == "video_show_actions":
        token = (value or "").strip()
        return _parse_json_object(token) if token.startswith("{") else {}
    if action == "video_hide_actions":
        return {}
    if action in ("trace_start", "trace_stop", "recording_start", "recording_stop"):
        return {}
    if action == "run_code":
        return {"code": value or ""}
    # ── B5：verify 四件套（Playwright 原生断言） ──
    if action == "verify_text":
        return {"text": value or ""}
    if action == "verify_visible":
        r = (role or "").strip()
        if not r:
            raise ValueError("verify_visible 需要 role 字段（如 button/heading/link），可从快照角色读取")
        return {"role": r, "accessibleName": value or ""}
    if action == "verify_value":
        return {
            "type": (field_type or "textbox").strip() or "textbox",
            "element": desc,
            "target": sel,
            "value": value or "",
        }
    if action == "verify_list":
        token = (value or "").strip()
        if token.startswith("["):
            try:
                items = json.loads(token)
            except (ValueError, TypeError):
                items = [token]
        else:
            items = [token] if token else []
        if not isinstance(items, list):
            items = [str(items)]
        return {"element": desc, "target": sel, "items": items}
    # ── B5：网络模拟（红线：默认关） ──
    if action == "route":
        obj = _parse_json_object(value)
        if not obj.get("pattern"):
            raise ValueError('route 需要 JSON 且含 pattern（如 {"pattern":"**/api/x","status":500}）')
        return {
            k: obj[k]
            for k in ("pattern", "status", "body", "contentType", "headers", "removeHeaders")
            if obj.get(k) is not None
        }
    if action == "route_list":
        return {}
    if action == "unroute":
        token = (value or "").strip()
        return {"pattern": token} if token else {}
    if action == "network_state":
        token = (value or "").strip().lower()
        if token not in ("offline", "online"):
            raise ValueError("network_state 需要 offline|online")
        return {"state": token}
    # ── B5：存储操作（红线：默认关） ──
    if action == "cookie_list":
        obj = _parse_json_object(value)
        return {k: obj[k] for k in ("domain", "path") if obj.get(k)}
    if action in ("cookie_get", "cookie_delete"):
        token = (value or "").strip()
        if not token:
            raise ValueError(f"{action} 需要 cookie 名称")
        return {"name": token}
    if action == "cookie_set":
        obj = _parse_json_object(value)
        if not obj.get("name"):
            raise ValueError('cookie_set 需要 JSON 且含 name（如 {"name":"a","value":"1"}）')
        return {
            k: obj[k]
            for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
            if obj.get(k) is not None
        }
    if action == "cookie_clear":
        return {}
    if action.startswith(("localstorage_", "sessionstorage_")):
        op = action.split("_", 1)[1]
        if op in ("clear", "list"):
            return {}
        if op in ("get", "delete"):
            token = (value or "").strip()
            if not token:
                raise ValueError(f"{action} 需要 key")
            return {"key": token}
        if op == "set":
            obj = _parse_json_object(value)
            if not obj.get("key"):
                raise ValueError(f'{action} 需要 JSON 且含 key（如 {{"key":"k","value":"v"}}）')
            return {
                "key": str(obj["key"]),
                "value": "" if obj.get("value") is None else str(obj["value"]),
            }
    if action == "storage_state_save":
        token = (value or "").strip()
        return {"filename": token} if token else {}
    if action == "storage_state_restore":
        token = (value or "").strip()
        if not token:
            raise ValueError("storage_state_restore 需要文件名")
        return {"filename": token}
    return {}


def legacy_build_mcp_args(action: str, selector: Optional[str], value: Optional[str],
                          wait_mode: Optional[str] = None,
                          role: Optional[str] = None,
                          field_type: Optional[str] = None) -> dict:
    """旧签名兼容包装（原 _build_mcp_args(action, selector, value)）。"""
    return build_mcp_args(action, selector=selector, element_desc=None, value=value,
                          wait_mode=wait_mode, role=role, field_type=field_type)


# browser_wait_for 只扫主 frame；iframe 内文本需 evaluate 遍历 frames 检查
_CROSS_FRAME_FN = (
    "() => {{ const t = {text}; const out = [];"
    "const scan = (d, l) => {{ try {{ if (d && d.body && typeof d.body.innerText === 'string'"
    " && d.body.innerText.includes(t)) out.push(l); }} catch (e) {{}} }};"
    "scan(document, 'main');"
    "for (let i = 0; i < window.frames.length; i++) {{"
    "try {{ scan(window.frames[i].document, 'frame' + i); }} catch (e) {{}}}}"
    "return out.join(','); }}"
)


async def verify_text_cross_frame(caller, text: str) -> Optional[str]:
    """跨 frame（含主 frame）即时检查文本是否可见。

    caller: 协程回调 (tool_name, args) -> {"success": bool, "text": str}
    返回命中的位置标签（如 'main' / 'frame0'），未命中返回 None。

    只解析 `### Result` 块（2026-10-15 修复假命中）：MCP 响应里含
    「### Ran Playwright code」回显的函数源码，源码中字面量 'main'/'frameN'
    会被宽松正则误判为命中——必须先从 Result 块取真实返回值再判定。
    """
    import json as _json

    if not text:
        return None
    fn = _CROSS_FRAME_FN.format(text=_json.dumps(text))
    try:
        r = await caller("browser_evaluate", {"function": fn})
    except Exception:
        return None
    if not isinstance(r, dict) or not r.get("success"):
        return None

    raw = str(r.get("text") or "")
    # 提取 ### Result 块（到下一个 ### 段落为止）
    if "### Result" in raw:
        raw = raw.split("### Result", 1)[1].split("###", 1)[0].strip()
    if not raw:
        return None
    # 真实返回值是 JSON 编码的字符串（out.join(',')），可能为空串
    value = raw
    try:
        decoded = _json.loads(raw)
        if isinstance(decoded, str):
            value = decoded
    except (ValueError, TypeError):
        pass
    value = value.strip()
    if not value:
        return None
    for token in value.split(","):
        token = token.strip()
        if re.fullmatch(r"main|frame\d+", token):
            return token
    return None


def build_tool_status(result: dict) -> str:
    """OTA 上下文中的工具结果行。

    断言类成功必须携带跨 frame 确认证据——否则 LLM 因快照中
    看不到非交互文本而拒绝宣告 done，陷入截图自证循环。
    """
    if not result.get("success"):
        return f"失败: {result.get('error', '未知')}"
    text = str(result.get("text") or "")
    if "visible in" in text:
        where = text.split("in")[-1].strip() or "?"
        return f"成功（断言通过，跨frame确认于 {where}）"
    return "成功"


# 导航/观察类动作不计入「有效操作数」（026-gen-exec-fixes C3）
NON_COUNTING_ACTIONS = frozenset({
    "goto", "navigate", "snapshot", "screenshot",
    "browser_navigate", "browser_snapshot", "browser_take_screenshot",
    # B1：证据/环境类动作同样非"有效操作"
    "network", "network_request", "resize", "emulate_media", "pdf",
    "video_start", "video_stop", "video_chapter", "video_show_actions",
    "video_hide_actions", "trace_start", "trace_stop",
    "recording_start", "recording_stop", "console",
})


def act_count_key(action: dict) -> Optional[tuple]:
    """成功操作的去重键 (action_name, selector, value)；导航/观察类返回 None 不计。"""
    if not isinstance(action, dict):
        return None
    name = str(action.get("name") or action.get("action") or "?")
    if name in NON_COUNTING_ACTIONS:
        return None
    args = action.get("args") or {}
    return (
        name,
        str(args.get("selector") or ""),
        str(args.get("value") or ""),
    )


def error_fingerprint(msg: str | None) -> str:
    """error 归一化指纹：strip 后前 40 字符（027-e2e-fixes 同因熔断用）。"""
    if not msg:
        return ""
    return str(msg).strip()[:40]


_TRANSIENT_ACTION_ERROR_RE = re.compile(
    r"(not found|no node|not visible|not attached|timed out|timeout|stale|"
    r"no element|detached|waiting for|not an? element|"
    r"元素不存在|未找到|不可见|超时|元素不在)",
    re.I,
)


def is_transient_action_error(msg: str | None) -> bool:
    """动作失败是否属于"瞬时"（元素未就绪/超时/被替换），值得本地重试。

    Phase C（2026-09-30）：这类失败此前直接记失败轮，把时序问题变成回合成本；
    现在由执行器先做本地重试（0.6s/1.5s），全部失败才记账。
    """
    if not msg:
        return False
    return bool(_TRANSIENT_ACTION_ERROR_RE.search(str(msg)))


# ── 032-exec-evidence：副作用动作的"不确定"归类 ───────────────────────────────
# 与 is_transient_action_error 的边界：后者覆盖"派发前"错误（元素未就绪等），
# 动作必然未生效、可安全本地重试；本函数只认 transport 级不确定（WS 等待超时/
# 连接中断/无回包）——此时动作可能已实际执行，重试存在重复副作用风险。
SIDE_EFFECT_ACTIONS = frozenset({
    "click", "double_click", "right_click", "fill", "press_key", "select",
    "check", "drag", "drag_xy", "click_xy", "upload", "drop", "dialog",
    "goto", "navigate", "hover",
    # B1：批量填表 / 鼠标按键 / 关页 / 任意代码都属"派发后可能已生效"
    "fill_form", "mouse_down", "mouse_up", "close", "run_code",
    # 036：页面 API 调用（写方法派发后可能已生效；传输级超时按 uncertain 不盲重试）
    "fetch",
})

_UNCERTAIN_FAILURE_RE = re.compile(
    r"(act timeout|act 超时|connection (closed|lost|reset|refused)|"
    r"disconnected|offline|not connected|no response|did not respond|"
    r"broken pipe|websocket|连接(中断|关闭|丢失|失败)|无响应|未回包)",
    re.I,
)


def is_uncertain_failure(action: Any, error: str | None) -> bool:
    """副作用动作 + transport 级失败 → "可能已生效"（豁免本地重试，强制重观察对账）。"""
    if not error:
        return False
    if isinstance(action, dict):
        name = str(action.get("name") or action.get("action") or "")
    else:
        name = str(action or "")
    name = name.strip().lower().replace("browser_", "")
    if name not in SIDE_EFFECT_ACTIONS:
        return False
    return bool(_UNCERTAIN_FAILURE_RE.search(str(error)))


UNCERTAIN_NOTE = "（可能已生效·不确定）"


def annotate_uncertain(action: Any, result: dict[str, Any] | None) -> bool:
    """副作用动作 transport 级失败 → result 标 `_uncertain`；返回是否 uncertain。"""
    if not isinstance(result, dict) or result.get("success"):
        return False
    if result.get("_uncertain"):
        return True
    if is_uncertain_failure(action, result.get("error")):
        result["_uncertain"] = True
        return True
    return False


def uncertain_note(args: dict[str, Any] | None) -> str:
    """报告步骤 message 的"可能已生效"标注后缀（无标注返回空串）。"""
    return UNCERTAIN_NOTE if (args or {}).get("_uncertain") else ""


def uncertain_context_hint(action_name: str) -> str:
    """uncertain 后的重观察提示（注入下一轮上下文，防止重复执行副作用）。"""
    return (
        f"SYSTEM: 上一步动作「{action_name}」在派发后超时/中断——可能已生效。"
        "请先观察当前页面状态对账：若预期效果已出现则按成功继续；"
        "确未生效再显式重试一次。不要重复执行副作用动作。"
    )
