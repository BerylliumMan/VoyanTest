"""MCP 参数构造共享模块（025-ref-click）。

Server 端（core/playwright_manager.py）与客户端（agent/client_core.py）
共用同一套 action→MCP 工具参数映射，消除两份实现的行为漂移。

契约: specs/025-ref-click/contracts/ws-mcp-contract.md C2
关键事实（@playwright/mcp 0.0.77+ 源码核实）:
- 交互工具参数为 elementSchema: {element?: str, target: str}
- target 匹配 ^(f\\d+)?e\\d+$ 时按快照 ref 定位，否则按 CSS selector 定位
"""
from __future__ import annotations

import re
import time
from typing import Optional

# 与 @playwright/mcp tab.ts targetLocator 一致的 ref 判定正则
_REF_RE = re.compile(r"^(f\d+)?e\d+$")

# LLM 简短 action 名 → MCP 工具名
TOOL_MAP = {
    "goto": "browser_navigate",
    "navigate": "browser_navigate",
    "click": "browser_click",
    "fill": "browser_type",
    "select": "browser_select_option",
    "wait": "browser_wait_for",
    "screenshot": "browser_take_screenshot",
    "snapshot": "browser_snapshot",
    "assert_text": "browser_wait_for",
    "press_key": "browser_press_key",
    "hover": "browser_hover",
    "evaluate": "browser_evaluate",
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


def build_mcp_args(
    action: str,
    *,
    selector: Optional[str],
    element_desc: Optional[str] = None,
    value: Optional[str] = None,
    timeout_ms: int = 30000,
) -> dict:
    """按 contracts C2 构造 MCP 工具参数。

    element 为人类可读描述（权限确认用），缺失时回退空字符串；
    target 原样传递 ref 或 CSS 选择器，不做转义改写。
    """
    sel = selector or ""
    desc = (element_desc or "").strip()

    if action in ("goto", "navigate", "browser_navigate"):
        return {"url": value or "about:blank"}
    if action == "click":
        return {"element": desc, "target": sel}
    if action == "fill":
        return {"element": desc, "target": sel, "text": value or ""}
    if action == "select":
        return {
            "element": desc,
            "target": sel,
            "values": [value] if value else [],
        }
    if action == "wait":
        if value and value.isdigit():
            return {"time": int(value)}
        return {"text": value or ""}
    if action == "screenshot":
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
    return {}


def legacy_build_mcp_args(action: str, selector: Optional[str], value: Optional[str]) -> dict:
    """旧签名兼容包装（原 _build_mcp_args(action, selector, value)）。"""
    return build_mcp_args(action, selector=selector, element_desc=None, value=value)


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
    # MCP evaluate 返回形如 '### Result\n"main"'，用正则宽松提取位置标签
    import re as _re
    m = _re.search(r"\b(main|frame\d+)\b", str(r.get("text") or ""))
    return m.group(1) if m else None


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
