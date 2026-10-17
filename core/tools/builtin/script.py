# core/tools/builtin/script.py
# 036 US3：诊断脚本工具（run_script）——受控沙箱 + 只读 SDK（http/db/platform/files）。
#
# 用途（FR-016/FR-019）：执行失败取证——先跑脚本拿到证据（查库/调接口/读文件/平台查询），
# 再下结论。门控 chat_script_enabled（默认关）；开启后全体对话用户可用、免逐次审批（Q1）。
# 沙箱口径沿用 core/script_sandbox.py 白名单（无 import/def/class/try；for 有界；超时/输出上限）。
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from core.tools.registry import ToolContext, register_tool
from core.tools.result import ToolResult, error_result, shape_result

# 工具结果内输出节选上限（工具循环预算默认 16KB；超出显式截断标注）
OUTPUT_EXCERPT_CHARS = 12000
# 前端工具行展开区节选（live 事件）
LIVE_EXCERPT_CHARS = 2000


def _script_gate_enabled() -> bool:
    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "chat_script_enabled", False))
    except Exception:
        return False


@register_tool(
    name="run_script",
    description=(
        "执行受控诊断脚本（Python 沙箱）用于取证：可 http 请求（单次≤10s、响应≤2MB）、"
        "db 只读查询（仅 SELECT/WITH/EXPLAIN、≤200 行、敏感表拒绝）、platform 平台查询"
        "（list_projects/search_cases/get_case/list_suites/list_batches/get_batch/count_cases）、"
        "files 读取（data/、reports/，≤1MB）。语法受白名单限制：无 import/def/class/try，"
        "for 有界（range 字面量）。示例：`r = db.query('SELECT id, status FROM run_batches ORDER BY id DESC LIMIT 5'); print(r['rows'])`。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "Python 语句（沙箱白名单语法）"},
            "title": {"type": "string", "description": "可读标题（工具行展示）"},
        },
        "required": ["script"],
    },
    side_effect="execute",
    requires_confirmation=False,
)
async def run_script_tool(ctx: ToolContext, script: str, title: str = "") -> ToolResult:
    # 门控双检（清单过滤之外的硬防线）
    if not _script_gate_enabled():
        return error_result("诊断脚本功能未开启（chat_script_enabled=false，管理员可在系统设置开启）")

    text = str(script or "")
    if not text.strip():
        return error_result("script 不能为空")

    # 共享执行入口（036+：对话工具与执行链 run_script 动作同源）
    from app.services.exec_script import execute_script

    result = await execute_script(user_id=ctx.user_id, script=text, title=title)

    output = result.get("output") or ""
    data = {
        "title": result.get("title"),
        "output": output,
        "error": result.get("error"),
        "duration_ms": result.get("duration_ms"),
        "db_queries": result.get("db_queries"),
        "http_calls": result.get("http_calls"),
        "truncated": bool(result.get("truncated")),
    }
    state_txt = "成功" if result.get("ok") else "失败"
    label = (title or "").strip() or "诊断脚本"
    summary = (
        f"{label}：{state_txt}（{result.get('duration_ms')}ms，"
        f"DB {result.get('db_queries')} / HTTP {result.get('http_calls')}）"
    )
    if not result.get("ok"):
        err = result.get("error") or "脚本执行失败"
        # 036 契约：输出超沙箱硬上限（64KB）→ 如实标注截断（脚本可减少打印后重跑）
        if "输出超过上限" in err:
            data["output"] = "（输出超过沙箱 64KB 上限，未返回全文；请减少打印后重跑）"
            return ToolResult(
                ok=False, summary=summary, data=data, truncated=True, error=err,
            )
        return ToolResult(
            ok=False, summary=summary, data=data, truncated=bool(result.get("truncated")), error=err,
        )
    shaped = shape_result(data, summary=summary)
    shaped.ok = True
    shaped.error = None
    return shaped


__all__ = ["run_script_tool", "OUTPUT_EXCERPT_CHARS", "LIVE_EXCERPT_CHARS"]
