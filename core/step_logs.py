# core/step_logs.py
# 报告步骤日志：agent_tool_calls → 步骤行 的统一渲染（034 迭代，2026-10-10）。
#
# 背景：客户端 AgentBridge._save_report 与服务端 AgentRunner 是两条独立执行链路，
# 此前只有客户端路径把「每动作」渲染成报告步骤行；服务端路径只写一条
# "AgentRunner 完成" 摘要行 + console 行，报告里看不到真实步骤
# （2026-10-10 用户实测：批次 483 详情显示"两个步骤都失败但结果通过"）。
# 本模块统一两条链路的渲染口径；步骤行形状与 RunLog 兼容：
#   {"step_id", "level", "message", "screenshot_path"}
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# playwright-mcp 工具名 → 中文标签（其余按原样展示）
_ACTION_LABELS = {
    "browser_navigate": "导航到",
    "browser_click": "点击",
    "browser_type": "输入",
    "browser_snapshot": "查看页面",
    "browser_take_screenshot": "截图",
    "browser_wait_for": "等待",
    "browser_select_option": "选择",
}


def render_step_message(
    tool_name: str,
    tool_args: dict | None,
    error_message: str | None = None,
) -> str:
    """把一次工具调用渲染为一行步骤描述（含错误摘要与 uncertain 标注）。"""
    tool = str(tool_name or "")
    args = tool_args if isinstance(tool_args, dict) else {}
    lbl = _ACTION_LABELS.get(tool, tool)
    vals = [
        v
        for v in (
            args.get("url"),
            args.get("value"),
            args.get("selector"),
            args.get("element"),
            args.get("text"),
        )
        if v
    ]
    msg = f"{lbl} {vals[0]}" if vals else lbl
    if error_message:
        msg += f" | {error_message}"
    try:
        from core.mcp_args import uncertain_note

        note = uncertain_note(args)
        if note:
            msg += note
    except Exception:
        logger.debug("uncertain_note 渲染跳过", exc_info=True)
    return msg


async def build_step_logs_from_tool_calls(
    db,
    agent_run_id: int,
    resolve_screenshot: Optional[Callable[[int], Optional[str]]] = None,
) -> list[dict]:
    """读取 ``agent_tool_calls``（按 agent_run id）构建步骤日志行。

    - success=1 → level=info；否则 level=error（前端渲染为通过/失败徽标）；
    - 失败行可通过 ``resolve_screenshot(turn_number)`` 附带现场截图；
    - 读取失败返回 []（调用方自行兜底，不阻塞报告落库）。
    """
    from sqlalchemy import select

    from app.db_models import AgentToolCall

    rows = (
        await db.execute(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == agent_run_id)
            .order_by(AgentToolCall.id)
        )
    ).scalars().all()
    logs: list[dict] = []
    for tc in rows:
        ok = tc.success == 1
        turn = int(tc.turn_number or 0)
        ss_path = None
        if not ok and resolve_screenshot is not None:
            try:
                ss_path = resolve_screenshot(turn)
            except Exception:
                ss_path = None
        logs.append(
            {
                "step_id": None,
                "level": "info" if ok else "error",
                "message": render_step_message(
                    tc.tool_name, tc.tool_args, tc.error_message
                ),
                "screenshot_path": ss_path,
            }
        )
    return logs
