# core/tools/builtin/queries.py
# 035 内置工具：只读查询（US2 数据问答直达）。
# 全部 read：权限过滤（项目/所有权）在服务层强制；分页 limit/offset；有界结果。
from __future__ import annotations

from app.services import tool_queries
from core.tools.registry import ToolContext, register_tool


@register_tool(
    name="list_projects",
    description="列出当前用户可见的项目（可按名称/描述关键词过滤，分页）。回答“有哪些项目”类问题。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "名称/描述关键词（可选）"},
            "limit": {"type": "integer", "description": "返回条数上限（默认 20，最大 100）"},
            "offset": {"type": "integer", "description": "偏移量（翻页用，默认 0）"},
        },
        "required": [],
    },
    side_effect="read",
)
async def list_projects(ctx: ToolContext, query: str = "", limit: int = 20, offset: int = 0):
    return await tool_queries.list_projects(ctx.db, ctx.user_id, query=query, limit=limit, offset=offset)


@register_tool(
    name="search_cases",
    description="按项目搜索测试用例（可按名称/描述关键词与类型过滤，分页）。返回 id/名称/类型/步骤数。",
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "项目 id（必填）"},
            "query": {"type": "string", "description": "名称/描述关键词（可选）"},
            "case_kind": {"type": "string", "enum": ["ui", "functional", "api"], "description": "用例类型（可选）"},
            "limit": {"type": "integer", "description": "返回条数上限（默认 20，最大 100）"},
            "offset": {"type": "integer", "description": "偏移量（翻页用，默认 0）"},
        },
        "required": ["project_id"],
    },
    side_effect="read",
)
async def search_cases(ctx: ToolContext, project_id: int, query: str = "", case_kind: str = "",
                       limit: int = 20, offset: int = 0):
    return await tool_queries.search_cases(ctx.db, ctx.user_id, project_id=project_id, query=query,
                                           case_kind=case_kind, limit=limit, offset=offset)


@register_tool(
    name="get_case",
    description="获取单个用例详情（含逐步骤描述与预期结果）。修改用例前先用它读取原始步骤。",
    parameters={
        "type": "object",
        "properties": {"case_id": {"type": "integer", "description": "用例 id"}},
        "required": ["case_id"],
    },
    side_effect="read",
)
async def get_case(ctx: ToolContext, case_id: int):
    return await tool_queries.get_case(ctx.db, ctx.user_id, case_id=case_id)


@register_tool(
    name="list_suites",
    description="列出项目的用例集（含名称/类型/用例数）。执行用例集前先用它确认 suite_id。",
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "项目 id（必填）"},
            "case_kind": {"type": "string", "enum": ["ui", "functional", "api"], "description": "用例集类型（可选）"},
            "limit": {"type": "integer", "description": "返回条数上限（默认 20，最大 100）"},
            "offset": {"type": "integer", "description": "偏移量（翻页用，默认 0）"},
        },
        "required": ["project_id"],
    },
    side_effect="read",
)
async def list_suites(ctx: ToolContext, project_id: int, case_kind: str = "",
                      limit: int = 20, offset: int = 0):
    return await tool_queries.list_suites(ctx.db, ctx.user_id, project_id=project_id,
                                          case_kind=case_kind, limit=limit, offset=offset)


@register_tool(
    name="list_batches",
    description="列出执行批次（可按项目/状态过滤，分页）。回答“最近的失败批次”类问题。",
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "项目 id（可选；缺省=全部可见项目）"},
            "status": {"type": "string", "enum": ["running", "passed", "failed", "cancelled", "paused"],
                       "description": "批次状态过滤（可选）"},
            "limit": {"type": "integer", "description": "返回条数上限（默认 20，最大 100）"},
            "offset": {"type": "integer", "description": "偏移量（翻页用，默认 0）"},
        },
        "required": [],
    },
    side_effect="read",
)
async def list_batches(ctx: ToolContext, project_id: int | None = None, status: str = "",
                       limit: int = 20, offset: int = 0):
    return await tool_queries.list_batches(ctx.db, ctx.user_id, project_id=project_id, status=status,
                                           limit=limit, offset=offset)


@register_tool(
    name="get_batch",
    description="获取批次详情（状态/通过失败统计/各用例运行结果/失败用例清单）。归因分析前先用它取证。",
    parameters={
        "type": "object",
        "properties": {"batch_id": {"type": "integer", "description": "批次 id"}},
        "required": ["batch_id"],
    },
    side_effect="read",
)
async def get_batch(ctx: ToolContext, batch_id: int):
    return await tool_queries.get_batch(ctx.db, ctx.user_id, batch_id=batch_id)
