# core/tools/builtin/demo.py
# 035 US5：扩展点演示工具——"新增能力 = 注册一个工具"（零改编排核心）。
#
# 本文件同时作为《extension-guide.md》的活样例：ToolSpec 字段 / 副作用标注 /
# 结果契约（ToolResult + 有界）/ 权限（项目过滤在服务层强制）。
from __future__ import annotations

from sqlalchemy import func, select

from app import db_models
from core.tools.registry import ToolContext, register_tool
from core.tools.result import error_result, shape_result


@register_tool(
    name="count_cases",
    description="统计项目用例数（可按类型过滤）。示例：项目 30 有多少 UI 用例？",
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "项目 id（必填）"},
            "case_kind": {"type": "string", "enum": ["ui", "functional", "api"], "description": "用例类型（可选）"},
        },
        "required": ["project_id"],
    },
    side_effect="read",
)
async def count_cases(ctx: ToolContext, project_id: int, case_kind: str = ""):
    from app.auth import get_user_project_filter
    from app.crud.user import get_user_by_id

    user = await get_user_by_id(ctx.db, ctx.user_id)
    allowed = get_user_project_filter(user) if user is not None else []
    pid = int(project_id)
    if allowed is not None and pid not in allowed:
        return error_result(f"无权访问项目 #{pid}")

    conditions = [db_models.TestCase.project_id == pid]
    if case_kind in ("ui", "functional", "api"):
        conditions.append(db_models.TestCase.case_kind == case_kind)
    total = (await ctx.db.execute(select(func.count(db_models.TestCase.id)).where(*conditions))).scalar_one()
    label = case_kind or "全部"
    return shape_result(
        {"project_id": pid, "case_kind": case_kind or None, "count": int(total)},
        summary=f"项目 #{pid} 的{label}用例共 {int(total)} 条",
    )
