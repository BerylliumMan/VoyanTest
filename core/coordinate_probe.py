"""032 US5：坐标兜底的元素回解（click_xy/drag_xy 命中元素 → 语义定位）。

链路：坐标动作成功后，用 browser_evaluate 在页面执行 elementFromPoint，
把命中的元素（name/role/selector）回解出来，写入 tool_args._resolved 与
journal.replay——固化脚本可据此使用语义定位而非裸坐标。任何失败静默降级。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def build_element_probe_js(x: int, y: int) -> str:
    """构造 elementFromPoint 回解函数（坐标内联；浏览器侧执行，不引用模块闭包）。"""
    return (
        "() => {\n"
        f"  const x = {int(x)}, y = {int(y)};\n"
        "  const el = document.elementFromPoint(x, y);\n"
        "  if (!el) return JSON.stringify({});\n"
        "  const txt = (v) => (v == null ? '' : String(v).trim());\n"
        "  const tag = (el.tagName || '').toLowerCase();\n"
        "  const rawName = el.getAttribute('aria-label') || el.getAttribute('alt')\n"
        "    || el.getAttribute('title') || el.getAttribute('placeholder')\n"
        "    || (tag === 'input' || tag === 'select' || tag === 'textarea' ? el.value : '')\n"
        "    || el.innerText || el.textContent;\n"
        "  const name = txt(rawName).replace(/\\s+/g, ' ').slice(0, 120);\n"
        "  const implicit = {a:'link', button:'button', select:'combobox',\n"
        "    textarea:'textbox', option:'option'};\n"
        "  let role = txt(el.getAttribute('role')) || implicit[tag] || '';\n"
        "  if (!role && tag === 'input') {\n"
        "    const t = (el.getAttribute('type') || 'text').toLowerCase();\n"
        "    role = ({checkbox:'checkbox', radio:'radio', search:'searchbox',\n"
        "      button:'button', submit:'button', reset:'button'})[t] || 'textbox';\n"
        "  }\n"
        "  let selector = '';\n"
        "  if (el.id && /^[A-Za-z][\\w-]*$/.test(el.id)) selector = '#' + el.id;\n"
        "  else if (el.getAttribute('data-testid')) selector = `[data-testid=\"${el.getAttribute('data-testid')}\"]`;\n"
        "  else if (el.getAttribute('name')) selector = `${tag}[name=\"${el.getAttribute('name')}\"]`;\n"
        "  const r = el.getBoundingClientRect();\n"
        "  return JSON.stringify({name, role, selector, tag,\n"
        "    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}});\n"
        "}"
    )


def parse_element_probe_result(text: str | None) -> dict[str, Any] | None:
    """解析 evaluate 返回文本为元素信息；未命中/垃圾输入返回 None。

    playwright-mcp 的 evaluate 输出形如：
        ### Result
        "{\\"name\\":\\"Login\\",...}"   ← 返回字符串时会被再包一层引号/转义
        ### Ran Playwright code ...
    需先取 ### Result 区块、必要时反转义，再提取 JSON 对象。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    m = re.search(r"###\s*Result\s*\n(.+?)(?:\n###|\Z)", raw, re.S)
    payload = m.group(1).strip() if m else raw
    if payload.startswith('"') and payload.endswith('"'):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = payload[1:-1]
    m2 = re.search(r"\{.*\}", payload, re.S)
    if not m2:
        return None
    try:
        data = json.loads(m2.group(0))
    except Exception:
        return None
    if not isinstance(data, dict) or not data:
        return None
    if not (data.get("name") or data.get("selector")):
        return None
    return data


SendActFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def probe_via_evaluate_act(send_act: SendActFn, x: int, y: int) -> dict[str, Any] | None:
    """客户端链路：经 act 通道执行 evaluate 回解（失败静默）。"""
    try:
        js = build_element_probe_js(x, y)
        res = await send_act({"name": "evaluate", "args": {"value": js}})
        if not isinstance(res, dict) or not res.get("success"):
            return None
        text = str(res.get("text") or res.get("result") or "")
        return parse_element_probe_result(text)
    except Exception:  # noqa: BLE001
        logger.debug("坐标回解失败（静默降级）", exc_info=True)
        return None


async def probe_via_mcp(call_tool: Callable[..., Awaitable[dict[str, Any]]],
                        x: int, y: int) -> dict[str, Any] | None:
    """服务端链路：经 MCP call_tool 执行 evaluate 回解（失败静默）。"""
    try:
        js = build_element_probe_js(x, y)
        res = await call_tool("browser_evaluate", {"function": js})
        if not isinstance(res, dict) or not res.get("success"):
            return None
        return parse_element_probe_result(str(res.get("text") or ""))
    except Exception:  # noqa: BLE001
        logger.debug("坐标回解失败（静默降级）", exc_info=True)
        return None
