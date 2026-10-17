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
    # 036：页面 API 调用
    "fetch": "页面 API",
}


def _fmt_size(size) -> str:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{int(n)}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def render_step_message(
    tool_name: str,
    tool_args: dict | None,
    error_message: str | None = None,
) -> str:
    """把一次工具调用渲染为一行步骤描述（含错误摘要与 uncertain 标注）。"""
    tool = str(tool_name or "")
    args = tool_args if isinstance(tool_args, dict) else {}
    lbl = _ACTION_LABELS.get(tool, tool)

    # 036：fetch 行按 api 元数据渲染（method/url/status/size + 「以 API 方式完成」如实标注）
    api = args.get("api")
    if tool == "fetch" and isinstance(api, dict):
        msg = f"{lbl} {api.get('method') or 'GET'} {api.get('url') or ''}".rstrip()
        if api.get("status") is not None:
            msg += f" → {api['status']}"
        _sz = _fmt_size(api.get("size"))
        if _sz:
            msg += f" · {_sz}"
        msg += "（以 API 方式完成）"
        if args.get("evidence_path"):
            msg += f" · 存证 {args['evidence_path']}"
            if args.get("evidence_truncated"):
                msg += "（已截断）"
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

    # 036+：诊断脚本行按元数据渲染（标题/耗时/DB·HTTP 计数；失败带原因）
    if tool == "run_script":
        sc = args.get("script") if isinstance(args.get("script"), dict) else {}
        title = str(args.get("title") or args.get("element_desc") or "").strip()
        head = f"诊断脚本「{title}」" if title else "诊断脚本"
        ok = sc.get("ok")
        msg = head + ("：成功" if ok else ("：失败" if ok is False else ""))
        if sc.get("duration_ms") is not None:
            msg += f"（{sc.get('duration_ms')}ms，DB {sc.get('db_queries', 0)} / HTTP {sc.get('http_calls', 0)}）"
        if ok is False and sc.get("error"):
            msg += f" — {str(sc['error'])[:120]}"
        if not ok and error_message:
            msg += f" | {str(error_message)[:160]}"
        return msg

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
    tool_names: Optional[list[str]] = None,
) -> list[dict]:
    """读取 ``agent_tool_calls``（按 agent_run id）构建步骤日志行。

    - success=1 → level=info；否则 level=error（前端渲染为通过/失败徽标）；
    - 失败行可通过 ``resolve_screenshot(turn_number)`` 附带现场截图；
    - ``tool_names``：仅保留指定工具名（如 ``["fetch"]`` 取页面 API 证据行）；
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
    allow = {str(n) for n in tool_names} if tool_names else None
    logs: list[dict] = []
    for tc in rows:
        if allow is not None and str(tc.tool_name) not in allow:
            continue
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
                "tool": str(tc.tool_name or ""),
                "turn": turn,
            }
        )
    return logs


def fetch_evidence_rows(tool_rows: list[dict]) -> list[dict]:
    """页面 API（fetch）证据行：同一渲染内容重复调用去重（保留首次）。

    实测（agent_run 461）：模型对同一导出接口连续 fetch 3 次"确认"，报告只应保留一条。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in tool_rows or []:
        if r.get("tool") != "fetch":
            continue
        key = str(r.get("message") or "")
        if key and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def script_evidence_rows(tool_rows: list[dict]) -> list[dict]:
    """诊断脚本（run_script）证据行：同渲染内容去重（保留首次）。

    执行链 run_script 是调查/取证动作，不作为步骤行；以证据行呈现
    （标题/耗时/DB·HTTP 计数/失败原因），完整脚本与输出在 agent_tool_calls。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in tool_rows or []:
        if r.get("tool") != "run_script":
            continue
        key = str(r.get("message") or "")
        if key and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def merge_fetch_evidence_into_steps(
    step_rows: list[dict],
    fetch_rows: list[dict],
    journal: list[dict] | None,
) -> list[dict]:
    """把 fetch 证据（含存证路径）并入其覆盖的**步骤行**（2026-10-16 用户反馈）。

    映射：journal 条目（action=fetch, turn, checklist_index）→ 步骤序号。
    未能映射的 fetch 行原样返回（由调用方追加展示，不丢证据）。
    """
    turn_map: dict[int, int] = {}
    for e in journal or []:
        if str(e.get("action")) != "fetch":
            continue
        try:
            idx = int(e.get("checklist_index"))
            turn = int(e.get("turn"))
        except (TypeError, ValueError):
            continue
        turn_map[turn] = idx
    by_order = {
        int(r.get("step_order") or 0): r for r in step_rows if r.get("step_order")
    }
    leftovers: list[dict] = []
    for fr in fetch_rows or []:
        try:
            turn = int(fr.get("turn"))
        except (TypeError, ValueError):
            leftovers.append(fr)
            continue
        target = by_order.get(turn_map.get(turn, -1))
        if target is None:
            leftovers.append(fr)
            continue
        snippet = str(fr.get("message") or "").replace("（以 API 方式完成）", "").strip()
        if snippet.startswith("页面 API "):
            snippet = snippet[len("页面 API "):]
        if snippet:
            target["message"] = f"{target.get('message') or ''}（{snippet}）"
    return leftovers


def build_case_step_logs(
    steps: list[dict],
    covered_orders,
    run_ok: bool,
    failure_reason: Optional[str] = None,
) -> list[dict]:
    """OTA 路径报告步骤：按**用例步骤**渲染（与固化回放同口径，2026-10-16）。

    用户口径（批次 512/517/520 实测）：报告展开用例应显示用例步骤
    （打开/点击/断言…），而非 goto/click 等原始动作行。

    - 已覆盖步骤 → info ``"<desc> — 通过"``
    - 失败跑的首个未覆盖步骤 → error ``"<desc> — 失败：<reason>"``
    - 其余未覆盖步骤 → warning ``"<desc> — 未执行"``（前端渲染为「信息」徽标）
    """
    covered: set[int] = set()
    for o in covered_orders or []:
        try:
            covered.add(int(o))
        except (TypeError, ValueError):
            continue
    # 失败原因取核心片段（长 MCP 错误仅保留前两行/200 字；完整日志在工具调用与 console 证据中）
    reason = str(failure_reason or "").split("### Error")[0].strip().rstrip("—-:： ")
    reason = "\n".join(reason.splitlines()[:2])[:200]
    rows: list[dict] = []
    failed_marked = False
    for i, s in enumerate(steps or []):
        try:
            order = int(s.get("step_order") or 0) or (i + 1)
        except (TypeError, ValueError):
            order = i + 1
        desc = str(s.get("description") or "").strip() or f"步骤 {order}"
        if order in covered:
            rows.append({
                "step_id": None, "level": "info",
                "message": f"{desc} — 通过", "screenshot_path": None,
                "step_order": order,
            })
        elif not run_ok and not failed_marked:
            failed_marked = True
            msg = f"{desc} — 失败：{reason}" if reason else f"{desc} — 失败"
            rows.append({
                "step_id": None, "level": "error",
                "message": msg, "screenshot_path": None,
                "step_order": order,
            })
        else:
            rows.append({
                "step_id": None, "level": "warning",
                "message": f"{desc} — 未执行", "screenshot_path": None,
                "step_order": order,
            })
    return rows
