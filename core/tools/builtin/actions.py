# core/tools/builtin/actions.py
# 035 内置工具：对话 7 能力工具化（包装 034 服务处理器，行为零回退）。
#
# 工具 → 既有服务映射（同源：产物卡片/审计/留痕与决策协议路径完全一致）：
#   generate_cases  → chat_gen.generate_handler        （草稿生成，mutate/提议）
#   revise_drafts   → chat_gen.revise_drafts_handler   （草稿补丁，mutate/提议）
#   edit_case       → chat_edit.edit_case_handler      （修改对照卡，mutate/提议）
#   run_case        → chat_run.run_case_handler        （发起执行，execute）
#   explain_failure → chat_evidence.explain_handler    （失败归因，read）
#   set_refs        → chat_refs.set_refs_handler       （会话引用，mutate/会话态）
#   browse_site     → chat_browse.browse_generate_handler（浏览器探索，execute）
from __future__ import annotations

from core.chat import contracts
from core.tools.registry import ToolContext, register_tool
from core.tools.result import ToolResult, error_result


async def _run_action(ctx: ToolContext, action: dict) -> ToolResult:
    """构建动作 → 契约校验 → 调既有 handler → ToolResult（卡片/任务 id 交循环提取）。"""
    from app.services import (  # 触发动作处理器注册（幂等）
        chat_browse,
        chat_edit,
        chat_evidence,
        chat_gen,
        chat_refs,
        chat_run,
    )

    norm, err = contracts.validate_action(action)
    if err:
        return error_result(err)
    handler_map = {
        "generate": chat_gen.generate_handler,
        "revise_drafts": chat_gen.revise_drafts_handler,
        "edit_case": chat_edit.edit_case_handler,
        "run_case": chat_run.run_case_handler,
        "explain": chat_evidence.explain_handler,
        "set_refs": chat_refs.set_refs_handler,
        "browse_generate": chat_browse.browse_generate_handler,
    }
    handler = handler_map.get(norm["type"])
    if handler is None:
        return error_result(f"{norm['type']} 能力尚未开放")
    try:
        outcome = await handler(ctx.db, ctx.conversation, norm, {"user_id": ctx.user_id, "llm": ctx.llm})
    except Exception as exc:
        return error_result(f"工具执行失败：{exc}")
    data: dict = {}
    if outcome.card:
        data["card"] = outcome.card
        data["card_type"] = outcome.card.get("type")
    if outcome.task_id:
        data["task_id"] = str(outcome.task_id)
    summary = (outcome.note or "").strip() or ("完成" if outcome.ok else "失败")
    return ToolResult(ok=bool(outcome.ok), summary=summary, data=data or None,
                      error=None if outcome.ok else summary)


@register_tool(
    name="generate_cases",
    description="生成测试用例草稿（UI 或功能）。输入需求描述文本；产出草稿卡（提议态），用户在界面确认后导入。",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "需求描述全文（用于生成用例）"},
            "kind": {"type": "string", "enum": ["ui", "functional"], "description": "用例类型，默认 ui"},
        },
        "required": ["description"],
    },
    side_effect="mutate",
)
async def generate_cases(ctx: ToolContext, description: str, kind: str = "ui"):
    return await _run_action(ctx, {
        "type": "generate", "kind": kind or "ui", "source": "text",
        "payload": {"description": description},
    })


@register_tool(
    name="revise_drafts",
    description="对当前草稿集做增量修订（增/改/删单条）。只输出差异补丁，禁止整批重写。",
    parameters={
        "type": "object",
        "properties": {
            "gen_session_id": {"type": "string", "description": "草稿集会话 id（见上下文【当前草稿集】）"},
            "patch": {
                "type": "array", "items": {"type": "object"},
                "description": "补丁数组：{op:'add',case:{title,steps,expected},after_index?} | {op:'update',draft_id,fields:{...}} | {op:'remove',draft_id}",
            },
        },
        "required": ["gen_session_id", "patch"],
    },
    side_effect="mutate",
)
async def revise_drafts(ctx: ToolContext, gen_session_id: str, patch: list):
    return await _run_action(ctx, {
        "type": "revise_drafts", "gen_session_id": gen_session_id, "patch": patch,
    })


@register_tool(
    name="edit_case",
    description="修改已导入的用例（UI/功能）。只输出需要变化的步骤补丁（index 为 0-based），产出对照卡待用户确认。",
    parameters={
        "type": "object",
        "properties": {
            "case_id": {"type": "integer", "description": "用例 id"},
            "patch": {
                "type": "object",
                "description": "步骤补丁：{steps:[{op:'update',index,fields:{description,expected}}|{op:'add',step:{description,expected},after_index}|{op:'remove',index}], case_fields:{name?,priority?}}",
            },
            "summary": {"type": "string", "description": "一句话变更摘要"},
        },
        "required": ["case_id", "patch"],
    },
    side_effect="mutate",
)
async def edit_case(ctx: ToolContext, case_id: int, patch: dict, summary: str = ""):
    return await _run_action(ctx, {
        "type": "edit_case", "case_id": case_id, "patch": patch, "summary": summary,
    })


@register_tool(
    name="run_case",
    description="发起用例执行（用例清单或用例集）。返回运行卡与批次 id；执行形态默认取对话设置。",
    parameters={
        "type": "object",
        "properties": {
            "case_ids": {"type": "array", "items": {"type": "integer"}, "description": "用例 id 清单"},
            "suite_id": {"type": "integer", "description": "用例集 id（与 case_ids 二选一）"},
            "mode": {"type": "string", "enum": ["server", "client"], "description": "执行形态（默认按对话设置）"},
            "agent_id": {"type": "integer", "description": "客户端 id（client 形态）"},
            "environment_id": {"type": "integer", "description": "执行环境 id（默认按对话设置）"},
        },
        "required": [],
    },
    side_effect="execute",
)
async def run_case(ctx: ToolContext, case_ids: list | None = None, suite_id: int | None = None,
                   mode: str | None = None, agent_id: int | None = None,
                   environment_id: int | None = None):
    return await _run_action(ctx, {
        "type": "run_case", "case_ids": case_ids, "suite_id": suite_id,
        "mode": mode, "agent_id": agent_id, "environment_id": environment_id,
    })


@register_tool(
    name="explain_failure",
    description="分析执行批次失败原因（基于失败步骤/console/截图证据）。证据不足时明确说明，不臆造。",
    parameters={
        "type": "object",
        "properties": {
            "batch_id": {"type": "integer", "description": "批次 id"},
            "case_id": {"type": "integer", "description": "聚焦的用例 id（可选）"},
        },
        "required": ["batch_id"],
    },
    side_effect="read",
)
async def explain_failure(ctx: ToolContext, batch_id: int, case_id: int | None = None):
    return await _run_action(ctx, {"type": "explain", "batch_id": batch_id, "case_id": case_id})


@register_tool(
    name="set_refs",
    description="管理当前会话的引用（用例/批次/环境/执行 Agent）。",
    parameters={
        "type": "object",
        "properties": {
            "add": {"type": "array", "items": {"type": "object"},
                    "description": "新增引用：[{type:'case|batch|environment|agent|project',id,label}]"},
            "remove": {"type": "array", "items": {"type": "object"}, "description": "移除引用（同结构）"},
        },
        "required": [],
    },
    side_effect="mutate",
)
async def set_refs(ctx: ToolContext, add: list | None = None, remove: list | None = None):
    return await _run_action(ctx, {"type": "set_refs", "add": add, "remove": remove})


@register_tool(
    name="browse_site",
    description="打开网站进行探索性测试/按描述操作（浏览器真实执行），完成后生成 UI 用例草稿。需要站点 URL。",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "站点地址（含 host:port，无协议补 http://）"},
            "goal": {"type": "string", "description": "探索目标或操作步骤描述（中文）"},
            "username": {"type": "string", "description": "登录账号（用户消息中给出时；系统会替换占位符，不会泄露）"},
            "password": {"type": "string", "description": "登录密码（同上）"},
            "kind": {"type": "string", "enum": ["ui", "functional"], "description": "生成用例类型，默认 ui"},
        },
        "required": ["url"],
    },
    side_effect="execute",
)
async def browse_site(ctx: ToolContext, url: str, goal: str = "", username: str = "",
                      password: str = "", kind: str = "ui"):
    return await _run_action(ctx, {
        "type": "browse_generate", "url": url, "goal": goal,
        "username": username, "password": password, "kind": kind,
    })
