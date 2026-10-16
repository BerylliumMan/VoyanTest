# app/routers/mcp/server.py
# 035 US4：MCP 服务端（外部智能体接入出口）。
#
# 契约（contracts C3）：
#   · 传输：streamable HTTP（stateless），挂载 /mcp；Bearer vt_ 令牌鉴权（401/403）
#   · 工具面 = 全局工具注册表（schema 自动）；read 范围令牌不列出写/执行类工具
#   · 影子会话：写/执行类调用惰性建「外部接入 · <令牌名>」对话（source=external），只读不建
#   · 同源：产物/批次/审计与对话路径完全一致（外部批次 source='external'）
#   · 并发：活跃外部会话全局上限 chat_external_max_sessions
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any, Optional

from mcp import types
from mcp.server import Server
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from core.tools.registry import REGISTRY, ToolContext

import core.tools.builtin  # noqa: F401 —— 导入即注册内置工具（确保 MCP 工具面就绪）

logger = logging.getLogger(__name__)

# 当前请求的接入身份（BearerAuthASGI 按请求写入；stateless 模式下请求内联处理，ContextVar 可靠）
_MCP_IDENTITY: ContextVar[Optional[dict]] = ContextVar("vt_mcp_identity", default=None)

_SHADOW_TITLE_PREFIX = "外部接入 · "
_ACTIVE_WINDOW_MINUTES = 30


def scope_allows_tool(scope: Optional[str], spec) -> bool:
    """read 范围令牌仅允许 read 工具；write/NULL（历史令牌）全允许。"""
    if str(scope or "write").strip().lower() != "read":
        return True
    return spec.side_effect == "read"


def _visible_specs(scope: Optional[str]):
    from core.tools.loop import default_tool_filter

    return [
        s for s in REGISTRY.all()
        if scope_allows_tool(scope, s) and default_tool_filter(s.name)
    ]


# ----------------------------
# 影子会话（T039）
# ----------------------------

def _shadow_title(token_name: str) -> str:
    return (_SHADOW_TITLE_PREFIX + (token_name or "令牌"))[:200]


async def _count_active_external(db) -> int:
    from sqlalchemy import func, select

    from app import db_models
    from app.tz import now as tz_now
    from datetime import timedelta

    cutoff = tz_now() - timedelta(minutes=_ACTIVE_WINDOW_MINUTES)
    q = select(func.count(db_models.ChatConversation.id)).where(
        db_models.ChatConversation.source == "external",
        db_models.ChatConversation.status == "active",
        db_models.ChatConversation.updated_at >= cutoff,
    )
    return int((await db.execute(q)).scalar_one() or 0)


async def _resolve_shadow_project(db, ident: dict, spec, args: dict) -> Optional[int]:
    """影子会话项目绑定：令牌项目 → 参数 project_id → 目标资源项目 → None。"""
    if ident.get("project_scope") is not None:
        return int(ident["project_scope"])
    if args.get("project_id") is not None:
        try:
            return int(args["project_id"])
        except (TypeError, ValueError):
            pass
    from app import db_models

    for key in ("case_id",):
        cid = args.get(key)
        if cid is not None:
            case = await db.get(db_models.TestCase, int(cid))
            if case is not None:
                return case.project_id
    for cid in args.get("case_ids") or []:
        case = await db.get(db_models.TestCase, int(cid))
        if case is not None:
            return case.project_id
    if args.get("batch_id") is not None:
        batch = await db.get(db_models.RunBatch, int(args["batch_id"]))
        if batch is not None:
            return batch.project_id
    return None


async def _get_or_create_shadow(db, ident: dict, spec, args: dict):
    """写/执行类调用：找到或创建该令牌的影子会话（T039 + T041 上限）。"""
    from sqlalchemy import select

    from app import db_models
    from app.config import get_settings

    title = _shadow_title(ident.get("token_name") or "")
    q = (
        select(db_models.ChatConversation)
        .where(
            db_models.ChatConversation.user_id == ident["user_id"],
            db_models.ChatConversation.source == "external",
            db_models.ChatConversation.status == "active",
            db_models.ChatConversation.title == title,
        )
        .order_by(db_models.ChatConversation.id.desc())
        .limit(1)
    )
    existing = (await db.execute(q)).scalars().first()
    if existing is not None:
        return existing

    cap = int(getattr(get_settings(), "chat_external_max_sessions", 4) or 4)
    active = await _count_active_external(db)
    if active >= cap:
        raise ValueError(f"外部接入会话数已达上限（{cap}），请稍后再试或等待现有会话过期")

    project_id = await _resolve_shadow_project(db, ident, spec, args)
    from app.crud import chat as chat_crud

    conv = await chat_crud.create_conversation(
        db,
        user_id=ident["user_id"],
        project_id=project_id,
        title=title,
        case_kind="ui",
        source="external",
    )
    logger.info("external shadow conversation created id=%s token=%s", conv.id, ident.get("token_id"))
    return conv


# ----------------------------
# MCP Server（低层 SDK：schema 直取注册表）
# ----------------------------

async def _enforce_token_project(db, ident: dict, spec, args: dict) -> dict:
    """令牌项目范围强制（MCP 路径手动版）：解析目标项目 → 校验/注入。

    - 显式 project_id 不匹配 → 拒绝；
    - 资源型参数（case_id/case_ids/suite_id/batch_id/environment_id）解析所属项目比对；
    - 支持 project_id 但缺省的工具 → 注入令牌项目（防全项目数据泄漏）。
    返回（可能已注入的）新 args；越权抛 ValueError。
    """
    scope = ident.get("project_scope")
    if scope is None:
        return args
    scope = int(scope)
    from app import db_models

    if args.get("project_id") is not None:
        try:
            pid = int(args["project_id"])
        except (TypeError, ValueError):
            raise ValueError(f"非法项目 id：{args['project_id']}")
        if pid != scope:
            raise ValueError(f"该令牌仅限项目 #{scope} 使用（请求项目 #{pid}）")
        return args

    async def _check(entity, label, cid):
        if entity is not None and int(getattr(entity, "project_id", 0) or 0) != scope:
            raise ValueError(
                f"该令牌仅限项目 #{scope} 使用（{label} #{cid} 属于项目 #{getattr(entity, 'project_id', '?')}）"
            )

    if args.get("case_id") is not None:
        cid = int(args["case_id"])
        await _check(await db.get(db_models.TestCase, cid), "用例", cid)
    for cid in args.get("case_ids") or []:
        cid = int(cid)
        await _check(await db.get(db_models.TestCase, cid), "用例", cid)
    if args.get("suite_id") is not None:
        sid = int(args["suite_id"])
        await _check(await db.get(db_models.TestSuite, sid), "用例集", sid)
    if args.get("batch_id") is not None:
        bid = int(args["batch_id"])
        await _check(await db.get(db_models.RunBatch, bid), "批次", bid)
    if args.get("environment_id") is not None:
        eid = int(args["environment_id"])
        await _check(await db.get(db_models.Environment, eid), "环境", eid)

    if spec.name in ("search_cases", "list_suites", "list_batches", "count_cases"):
        args = {**args, "project_id": scope}
    return args


def _filter_projects_by_scope(result, ident: dict):
    """list_projects 结果按令牌项目范围过滤（只留绑定项目）。"""
    scope = ident.get("project_scope")
    if scope is None or not result.ok or not isinstance(result.data, dict):
        return result
    from core.tools.result import shape_result

    items = [it for it in (result.data.get("items") or []) if it.get("id") == int(scope)]
    return shape_result(
        {**result.data, "items": items},
        summary=f"共 {len(items)} 个项目（令牌范围 #{scope}）",
    )


def build_server() -> Server:
    server = Server("voyantest")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        ident = _MCP_IDENTITY.get() or {}
        specs = _visible_specs(ident.get("scope"))
        return [
            types.Tool(
                name=s.name,
                description=(s.description or "")[:1000],
                inputSchema=s.parameters or {"type": "object", "properties": {}},
            )
            for s in specs
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[types.ContentBlock]:
        ident = _MCP_IDENTITY.get() or {}
        spec = REGISTRY.get(name)
        if spec is None:
            raise ValueError(f"未知工具：{name}")
        if not scope_allows_tool(ident.get("scope"), spec):
            raise ValueError("该令牌为只读范围（read），禁止调用写/执行类工具；请在平台重新签发 write 令牌")

        args = dict(arguments or {})
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            args = await _enforce_token_project(db, ident, spec, args)
            conversation = None
            if spec.side_effect != "read":
                conversation = await _get_or_create_shadow(db, ident, spec, args)
            ctx = ToolContext(
                db=db,
                user_id=ident.get("user_id"),
                conversation=conversation,
                source="external",
                token_id=ident.get("token_id"),
                token_scope=ident.get("scope"),
            )
            override = None
            if spec.side_effect != "read":
                from app.crud import run as run_crud

                override = run_crud.set_batch_source_override("external", ident.get("token_id"))
            try:
                result = await spec.handler(ctx, **args)
            finally:
                if override is not None:
                    from app.crud import run as run_crud

                    run_crud.reset_batch_source_override(override)

            if name == "list_projects":
                result = _filter_projects_by_scope(result, ident)

            # 审计（与对话路径同口径：tool.<name> + 参数打码）
            from core.tools.loop import _audit_tool_call

            await _audit_tool_call(ctx, name, args, result)

        payload = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
        return [types.TextContent(type="text", text=payload)]

    return server


# ----------------------------
# ASGI：鉴权包装 + 单例 session manager（lifespan 管理）
# ----------------------------

_session_manager = None
_manager_exit_stack = None


def get_session_manager():
    """进程级单例（stateless streamable HTTP）。"""
    global _session_manager
    if _session_manager is None:
        _session_manager = StreamableHTTPSessionManager(
            app=build_server(), event_store=None, json_response=False, stateless=True,
        )
    return _session_manager


def get_mcp_asgi():
    """鉴权包装后的 ASGI 应用（直接注册为 /mcp 路由端点）。"""
    return BearerAuthASGI(StreamableHTTPASGIApp(get_session_manager()))


async def start_mcp_session_manager() -> None:
    """进入 manager.run()（FastAPI lifespan 启动时调用）。"""
    global _manager_exit_stack
    import contextlib

    _manager_exit_stack = contextlib.AsyncExitStack()
    await _manager_exit_stack.enter_async_context(get_session_manager().run())


async def stop_mcp_session_manager() -> None:
    global _manager_exit_stack
    if _manager_exit_stack is not None:
        await _manager_exit_stack.aclose()
        _manager_exit_stack = None


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class BearerAuthASGI:
    """MCP 端点鉴权 + 身份注入（纯 ASGI，包住 streamable HTTP 应用）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        from app.config import get_settings

        if not get_settings().chat_enabled:
            await _send_json(send, 403, {"error": "chat_disabled", "detail": "对话功能未启用"})
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        authorization = headers.get("authorization")
        from app.auth import authenticate_api_token_full
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            user, token = await authenticate_api_token_full(db, authorization)
        if user is None:
            await _send_json(send, 401, {
                "error": "invalid_token",
                "detail": "无效、已撤销或已过期的接入令牌（Authorization: Bearer vt_…）",
            })
            return

        reset = _MCP_IDENTITY.set({
            "user_id": user.id,
            "token_id": token.id,
            "token_name": token.name,
            "scope": token.scope or "write",
            "project_scope": token.project_id,
        })
        try:
            await self.app(scope, receive, send)
        finally:
            _MCP_IDENTITY.reset(reset)


def build_mcp_app_and_manager():
    """构建 (Starlette 应用, session manager)——测试可手动管理 lifespan。"""
    from starlette.applications import Starlette
    from starlette.routing import Route

    server = build_server()
    session_manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=False, stateless=True,
    )
    inner = StreamableHTTPASGIApp(session_manager)
    wrapped = BearerAuthASGI(inner)

    starlette_app = Starlette(
        routes=[Route("/", endpoint=wrapped, methods=["GET", "POST", "DELETE"])],
        lifespan=lambda app: session_manager.run(),
    )
    return starlette_app, session_manager


def build_mcp_starlette_app():
    """构建可挂载的 MCP ASGI 应用（测试/子应用场景；生产路径用 register_mcp_routes）。"""
    return build_mcp_app_and_manager()[0]


def register_mcp_routes(app) -> None:
    """在 FastAPI 应用上注册 /mcp 端点（精确路径；含尾斜杠兼容）。

    注意：Starlette Mount 只匹配 ``/mcp/...``（不带斜杠的精确路径不命中），
    因此这里直接注册两条 Route（与 SPA 兜底路由的顺序：须在兜底之前）。
    """
    from starlette.routing import Route

    endpoint = get_mcp_asgi()
    app.router.routes.append(Route("/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
    app.router.routes.append(Route("/mcp/", endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
