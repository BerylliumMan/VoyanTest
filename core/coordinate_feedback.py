"""038 US1：坐标点击回读反馈（点击后"实际命中了什么"）。

复用 032 的 elementFromPoint 探针；与 _resolved（固化定位符）不同，
本模块的输出只用于"下一轮决策反馈"：命中元素/空白/读取失败，以及连续点偏提示。
任何失败静默降级，不改变动作成败。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

COORD_ACTIONS = frozenset({
    "click_xy", "browser_mouse_click_xy",
    "drag_xy", "browser_mouse_drag_xy",
    "move_mouse", "browser_mouse_move_xy",
})


def parse_coordinates(value: Any) -> list[int]:
    """从 "x,y" / "x1,y1,x2,y2" 提取整数坐标（非法返回 []）。"""
    text = str(value or "")
    nums = [int(n) for n in re.findall(r"-?\d+", text)]
    return nums


def feedback_enabled() -> bool:
    """038 开关：坐标点击回读是否启用（关闭=基线，不回读、不注入提示）。"""
    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "exec_click_feedback", True))
    except Exception:
        return True


# 兼容旧名（内部使用）
_enabled = feedback_enabled


def _miss_limit() -> int:
    try:
        from app.config import get_settings

        return max(1, int(getattr(get_settings(), "exec_click_feedback_max_misses", 3) or 3))
    except Exception:
        return 3


def build_feedback_text(
    info: dict[str, Any] | None,
    x: int,
    y: int,
    *,
    streak: int = 0,
    read_failed: bool = False,
) -> str:
    """把回读结果格式化为可注入决策上下文的一句话。"""
    if read_failed:
        return f"坐标({x},{y})回读失败（页面可能已变化），请重新观察后再操作。"
    if not info:
        text = f"坐标({x},{y})未命中任何元素（空白或视口外）。"
        if streak >= _miss_limit():
            return text + f"已连续 {streak} 次未命中，请重新观察页面/截图后再估算坐标，不要继续盲点。"
        return text
    name = str(info.get("name") or "").strip()
    role = str(info.get("role") or "").strip()
    tag = str(info.get("tag") or "").strip()
    selector = str(info.get("selector") or "").strip()
    label = name or selector or tag or "元素"
    kind = role or tag or "element"
    text = f"坐标({x},{y})实际命中：{kind}「{label[:60]}」"
    if selector and selector != label:
        text += f"（{selector[:60]}）"
    rect = info.get("rect") if isinstance(info.get("rect"), dict) else {}
    if rect:
        text += (
            f"；元素区域 x={rect.get('x')},y={rect.get('y')},"
            f"w={rect.get('w')},h={rect.get('h')}"
        )
    if str(tag).lower() == "canvas" or "canvas" in selector.lower():
        text += (
            "；这是画布：若目标图形在画布内，页面坐标 = 画布内坐标 + 元素偏移(x,y)"
        )
    return text + "。若这不是目标元素，请重新估算坐标。"


def parse_feedback_payload(raw: Any) -> dict[str, Any] | None:
    """解析回读结果：canvas 等无 name/selector 的元素也算命中（带 tag/rect）。"""
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw or "")
        if "### Result" in text:
            text = text.split("### Result", 1)[1].split("###", 1)[0].strip()
        try:
            import json

            data = json.loads(text)
            if isinstance(data, str):
                data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, dict) or not data:
        return None
    if not (data.get("name") or data.get("selector") or data.get("tag") or data.get("rect")):
        return None
    return data


async def probe_via_act(send_act, x: int, y: int) -> tuple[dict[str, Any] | None, bool]:
    """客户端链：经 act 通道回读（区分未命中与读取失败）。"""
    try:
        from core.coordinate_probe import build_element_probe_js

        res = await send_act({
            "name": "evaluate", "args": {"value": build_element_probe_js(x, y)},
        })
        if not (isinstance(res, dict) and res.get("success")):
            return None, True
        text = str(res.get("text") or res.get("result") or "")
        return parse_feedback_payload(text), False
    except Exception:
        logger.debug("坐标回读失败（act）", exc_info=True)
        return None, True


async def probe_via_mcp(call_tool, x: int, y: int) -> tuple[dict[str, Any] | None, bool]:
    """服务端链：经 MCP evaluate 回读。"""
    try:
        from core.coordinate_probe import build_element_probe_js

        res = await call_tool("browser_evaluate", {"function": build_element_probe_js(x, y)})
        if not (isinstance(res, dict) and res.get("success")):
            return None, True
        text = str(res.get("text") or "")
        return parse_feedback_payload(text), False
    except Exception:
        logger.debug("坐标回读失败（mcp）", exc_info=True)
        return None, True


async def read_target(browser, x: int, y: int) -> tuple[dict[str, Any] | None, bool]:
    """只读回读坐标处元素；返回 (info, read_failed)。

    info=None 表示未命中；read_failed=True 表示 evaluate 本身失败（页面已变化/不支持）。
    """
    try:
        from core.coordinate_probe import (
            build_element_probe_js,
            parse_element_probe_result,
        )

        js = build_element_probe_js(x, y)
        raw = await browser.evaluate_json(js)
        if raw is None:
            return None, True
        return parse_feedback_payload(raw), False
    except Exception:
        logger.debug("坐标回读失败（静默降级）", exc_info=True)
        return None, True


def json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


def feedback_from_result(
    action: dict[str, Any], result: dict[str, Any], *, streak: int = 0,
) -> str | None:
    """执行链：用 result["_resolved"] 生成反馈（无坐标动作返回 None）。"""
    if not _enabled():
        return None
    name = str(action.get("name") or action.get("action") or "").strip().lower()
    if name not in COORD_ACTIONS:
        return None
    if not isinstance(result, dict) or not result.get("success"):
        return None
    args = action.get("args") if isinstance(action.get("args"), dict) else action
    nums = parse_coordinates(args.get("value"))
    if len(nums) < 2:
        return None
    x, y = nums[0], nums[1]
    info = result.get("_resolved")
    if info is None:
        args_info = args.get("_resolved")
        info = args_info if isinstance(args_info, dict) else None
    return build_feedback_text(info if isinstance(info, dict) else None, x, y, streak=streak)
