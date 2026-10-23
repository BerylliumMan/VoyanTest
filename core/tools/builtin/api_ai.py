"""040 US6：接口测试 AI 对话工具（失败归因/契约检测/自然语言生成）。"""
from __future__ import annotations

from app import db_models
from app.auth import get_user_project_filter
from app.services.api_ai import ApiAiUnavailable
from core.tools.registry import ToolContext, register_tool
from core.tools.result import error_result, shape_result


async def _project_allowed(ctx: ToolContext, project_id: int) -> bool:
    user = await ctx.db.get(db_models.User, ctx.user_id) if ctx.user_id else None
    if user is None:
        return False
    allowed = get_user_project_filter(user)
    return allowed is None or project_id in allowed


async def _run_project(ctx: ToolContext, run_id: int):
    run = await ctx.db.get(db_models.TestRun, run_id)
    if run is None:
        return None, None
    project_id = None
    if run.case_id:
        case = await ctx.db.get(db_models.TestCase, run.case_id)
        project_id = case.project_id if case else None
    if project_id is None:
        return None, None
    if not await _project_allowed(ctx, project_id):
        return None, None
    return run, project_id


@register_tool(
    name="analyze_api_failure",
    description="分析一次失败的接口运行，返回根因、证据与修复建议（只读）",
    parameters={
        "type": "object",
        "properties": {"run_id": {"type": "integer", "description": "失败运行 ID"}},
        "required": ["run_id"],
    },
    side_effect="read",
)
async def analyze_api_failure(ctx: ToolContext, run_id: int = 0, **kwargs):
    run, project_id = await _run_project(ctx, int(run_id or 0))
    if run is None:
        return error_result("运行记录不存在或无权访问")
    if run.status not in ("failed", "error"):
        return error_result("仅失败运行可分析")
    from app.services.api_ai_analysis import analyze_failure

    try:
        row, _ = await analyze_failure(
            ctx.db, run=run, project_id=project_id, user_id=ctx.user_id,
        )
    except ApiAiUnavailable as exc:
        return error_result(str(exc))
    return shape_result(row.result or {}, summary=f"失败归因：{(row.result or {}).get('summary', '')}")


@register_tool(
    name="check_api_contract",
    description="对比接口运行响应与当前接口定义，返回契约差异（只读）",
    parameters={
        "type": "object",
        "properties": {
            "run_id": {"type": "integer"},
            "definition_id": {"type": "integer"},
        },
        "required": ["run_id"],
    },
    side_effect="read",
)
async def check_api_contract(ctx: ToolContext, run_id: int = 0, definition_id: int | None = None, **kwargs):
    run, project_id = await _run_project(ctx, int(run_id or 0))
    if run is None:
        return error_result("运行记录不存在或无权访问")
    from app.services.api_ai_contract import check_contract

    try:
        row = await check_contract(
            ctx.db, run=run, project_id=project_id,
            definition_id=definition_id, user_id=ctx.user_id,
        )
    except ApiAiUnavailable as exc:
        return error_result(str(exc))
    return shape_result(row.result or {}, summary=f"契约检测：{(row.result or {}).get('summary', '')}")


@register_tool(
    name="generate_api_from_text",
    description="根据一句业务描述生成接口用例/场景草稿（预览，不直接落库）",
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer"},
            "text": {"type": "string"},
            "target": {"type": "string", "enum": ["case", "scenario"]},
        },
        "required": ["project_id", "text"],
    },
    side_effect="mutate",
)
async def generate_api_from_text(ctx: ToolContext, project_id: int = 0, text: str = "", target: str = "case", **kwargs):
    project_id = int(project_id or 0)
    if not await _project_allowed(ctx, project_id):
        return error_result("无权访问该项目")
    from app.services.api_ai_generate import generate_from_text

    try:
        result = await generate_from_text(
            ctx.db, project_id=project_id, text=str(text or ""),
            target=str(target or "case"), user_id=ctx.user_id,
        )
    except ApiAiUnavailable as exc:
        return error_result(str(exc))
    return shape_result(result, summary=f"生成 {len((result.get('candidates') or []))} 个候选")
