# app/main.py
import asyncio
from contextlib import asynccontextmanager
import json as _json
import logging
import os
import uuid

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi import WebSocketDisconnect
from sqlalchemy import select, text, func
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from core.log_setup import setup_logging, set_request_id, get_request_id

_settings = get_settings()
setup_logging(level=_settings.log_level, fmt=_settings.log_format)

logger = logging.getLogger(__name__)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse

from .routers import project_router, testcase_router, module_router, report_router, config_router, environment_router, scheduler_router, agent_router
from .routers import auth_router, user_router, audit_router, agent_router as mgmt_agent_router, gen_router, recordings_router, notification_router, setup_router, agent_definition_router, agent_run_router
from app.config import get_settings
from app.websocket import websocket_logs

settings = get_settings()
APP_HOST = settings.app_host
APP_PORT = settings.app_port

if not settings.session_secret_key:
    logger.warning(
        "SESSION_SECRET_KEY 未设置！生产环境中请务必设置该值，"
        "否则 session 签名的安全性无法保证。开发环境可忽略此警告。"
    )
import app.database as db_mod
from app.database import Base, init_db_engine
from app import db_models
import uvicorn

try:
    from agent.router import router as agent_router
    AGENT_SUPPORT = True
except ImportError:
    AGENT_SUPPORT = False
    logger.warning("Agent support not available")


# ── Agent Run WebSocket 管理器（T012）────────────────────────────────────────


class AgentRunWSManager:
    """Agent 运行实时观察 WebSocket 管理器。

    与 LogWebSocketManager 模式一致，但专门用于 agent_runs：
    - 客户端通过 /ws/agent-runs/{run_id} 连接
    - 后端通过 send_status() 推送状态更新
    """

    def __init__(self):
        self._connections: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, run_id: int) -> None:
        await websocket.accept()
        async with self._lock:
            if run_id not in self._connections:
                self._connections[run_id] = set()
            self._connections[run_id].add(websocket)
        await self._send_to(websocket, {"type": "connected", "run_id": run_id})

    async def disconnect(self, websocket: WebSocket, run_id: int) -> None:
        async with self._lock:
            s = self._connections.get(run_id)
            if s:
                s.discard(websocket)
                if not s:
                    del self._connections[run_id]

    async def send_status(self, run_id: int, data: dict) -> None:
        async with self._lock:
            conns = list(self._connections.get(run_id, set()))
        for ws in conns:
            await self._send_to(ws, data)

    async def _send_to(self, ws: WebSocket, data: dict) -> None:
        try:
            await ws.send_text(_json.dumps(data, ensure_ascii=False))
        except (RuntimeError, ConnectionError):
            await self.disconnect(ws, data.get("run_id", 0))


agent_run_ws = AgentRunWSManager()


async def _recover_orphaned_agent_runs():
    """启动时恢复孤儿 agent_runs：将卡在 'running' 状态的记录标记为 'failed'。

    该函数在 _run_startup_init 中调用，确保服务重启后不会遗留假 running 状态的记录。
    """
    try:
        from app.database import AsyncSessionLocal
        if AsyncSessionLocal is None:
            return
        async with AsyncSessionLocal() as db:
            from sqlalchemy import update as sa_update
            from app import db_models
            result = await db.execute(
                sa_update(db_models.AgentRun)
                .where(db_models.AgentRun.status == "running")
                .values(
                    status="failed",
                    error="服务重启，运行被中断",
                    completed_at=func.now(),
                )
            )
            await db.commit()
            count = result.rowcount
            if count:
                logger.info("已恢复 %d 条孤儿 agent_run 记录（running → failed）", count)
    except Exception as exc:
        logger.warning("孤儿 agent_run 恢复失败: %s", exc, exc_info=True)


async def _run_startup_init():
    """Run async DB initialization at startup (not at import time)."""
    import app.database as db_mod
    if db_mod.engine is None:
        if not await db_mod.init_db_engine():
            logger.warning("数据库未配置，跳过初始化（进入配置模式）")
            return

    engine = db_mod.engine
    AsyncSessionLocal = db_mod.AsyncSessionLocal

    if os.getenv("DISABLE_CREATE_ALL", "false").lower() != "true":
        async with engine.begin() as conn:
            await conn.run_sync(db_mod.Base.metadata.create_all)
    else:
        logger.info("DISABLE_CREATE_ALL=true，跳过 create_all（请确保已执行 alembic upgrade head）")

    # ========== 数据库字段迁移 ==========
    # 这些迁移在引擎就绪后始终执行（不与 DISABLE_CREATE_ALL 关联）
    engine = db_mod.engine
    if engine is None:
        # 防御：引擎仍未就绪时直接读取配置创建独立连接
        url = db_mod._resolve_database_url()
        if url:
            _tmp_engine = None
            try:
                from sqlalchemy.ext.asyncio import create_async_engine
                _tmp_engine = create_async_engine(url)
                engine = _tmp_engine
            except Exception:
                pass
    if engine is None:
        logger.warning("数据库引擎不可用，跳过字段迁移")
    else:
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(255)"))
                await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)"))
        except Exception:
            logger.warning("users 表 nickname/email 列迁移失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE run_batches ADD COLUMN IF NOT EXISTS triggered_by VARCHAR(255)"))
        except Exception:
            logger.warning("run_batches 表 triggered_by 列迁移失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE recording_sessions ADD COLUMN IF NOT EXISTS events_data TEXT"))
        except Exception:
            logger.warning("recording_sessions 表 events_data 列迁移失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE test_runs ALTER COLUMN case_id DROP NOT NULL"))
        except Exception:
            logger.warning("test_runs 表 case_id 列 NOT NULL 约束迁移失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE gen_sessions ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        except Exception:
            logger.warning("gen_sessions 表 user_id 列迁移失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE ai_configs ADD COLUMN IF NOT EXISTS max_context_tokens INTEGER DEFAULT 131072"))
        except Exception:
            logger.warning("ai_configs 表 max_context_tokens 列迁移失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS prompt_templates (
                        id              SERIAL PRIMARY KEY,
                        key             VARCHAR(100) NOT NULL,
                        name            VARCHAR(200) NOT NULL,
                        category        VARCHAR(50) NOT NULL,
                        content         TEXT NOT NULL,
                        variables       JSONB NOT NULL DEFAULT '[]',
                        version         INTEGER NOT NULL DEFAULT 1,
                        is_active       BOOLEAN NOT NULL DEFAULT false,
                        description     TEXT,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE(key, version)
                    )
                """))
        except Exception:
            logger.warning("prompt_templates 表创建失败（非关键错误，继续）")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS agent_definitions (
                        id              SERIAL PRIMARY KEY,
                        name            VARCHAR(100) NOT NULL UNIQUE,
                        agent_type      VARCHAR(50) NOT NULL,
                        description     TEXT DEFAULT '',
                        skills          JSONB NOT NULL DEFAULT '[]',
                        llm_config      JSONB NOT NULL DEFAULT '{}',
                        prompt_overrides JSONB NOT NULL DEFAULT '{}',
                        is_active       INTEGER NOT NULL DEFAULT 0,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
        except Exception:
            logger.warning("agent_definitions 表创建失败（非关键错误，继续）")

        # ── agent_definitions 列迁移 ────────────────────────────────────────
        try:
            await conn.execute(text(
                "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS system_prompt TEXT"
            ))
        except Exception:
            logger.warning("agent_definitions.system_prompt 列迁移失败（非关键错误，继续）")

        try:
            await conn.execute(text(
                "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS tools JSONB DEFAULT '[]'"
            ))
        except Exception:
            logger.warning("agent_definitions.tools 列迁移失败（非关键错误，继续）")

        try:
            await conn.execute(text(
                "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS goal TEXT DEFAULT ''"
            ))
        except Exception:
            logger.warning("agent_definitions.goal 列迁移失败（非关键错误，继续）")

        try:
            await conn.execute(text(
                "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS constraints JSONB DEFAULT '[]'"
            ))
        except Exception:
            logger.warning("agent_definitions.constraints 列迁移失败（非关键错误，继续）")

        try:
            await conn.execute(text(
                "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS thinking_config JSONB DEFAULT '{}'"
            ))
        except Exception:
            logger.warning("agent_definitions.thinking_config 列迁移失败（非关键错误，继续）")

        # ── agent_runs 相关表创建 ────────────────────────────────────────────
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id                  SERIAL PRIMARY KEY,
                    agent_definition_id INTEGER NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
                    case_id             INTEGER REFERENCES test_cases(id) ON DELETE SET NULL,
                    status              VARCHAR(50) NOT NULL DEFAULT 'pending',
                    goal                JSONB NOT NULL DEFAULT '{}',
                    result              JSONB,
                    partial_results     JSONB NOT NULL DEFAULT '[]',
                    turns_used          INTEGER NOT NULL DEFAULT 0,
                    started_at          TIMESTAMPTZ,
                    completed_at        TIMESTAMPTZ,
                    duration_ms         INTEGER,
                    error               TEXT,
                    idempotency_key     VARCHAR(255) UNIQUE,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        except Exception:
            logger.warning("agent_runs 表创建失败（非关键错误，继续）")

        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id              SERIAL PRIMARY KEY,
                    run_id          INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    turn_number     INTEGER NOT NULL,
                    role            VARCHAR(20) NOT NULL,
                    content         TEXT NOT NULL,
                    tool_calls      JSONB,
                    token_count     INTEGER DEFAULT 0,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        except Exception:
            logger.warning("agent_messages 表创建失败（非关键错误，继续）")

        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agent_tool_calls (
                    id              SERIAL PRIMARY KEY,
                    run_id          INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    turn_number     INTEGER NOT NULL,
                    tool_name       VARCHAR(200) NOT NULL,
                    tool_args       JSONB NOT NULL DEFAULT '{}',
                    tool_result     JSONB,
                    success         INTEGER NOT NULL DEFAULT 1,
                    error_message   TEXT,
                    duration_ms     INTEGER,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        except Exception:
            logger.warning("agent_tool_calls 表创建失败（非关键错误，继续）")

        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agent_run_snapshots (
                    id              SERIAL PRIMARY KEY,
                    run_id          INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    turn_number     INTEGER NOT NULL,
                    context_json    JSONB NOT NULL,
                    compressed_count INTEGER DEFAULT 0,
                    token_count     INTEGER DEFAULT 0,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        except Exception:
            logger.warning("agent_run_snapshots 表创建失败（非关键错误，继续）")

        # ── 种子提示词数据 ────────────────────────────────────────────────────
        try:
            async with AsyncSessionLocal() as _prompt_db:
                from app.models.prompt_template import PromptTemplate
                existing = await _prompt_db.execute(
                    select(PromptTemplate).limit(1)
                )
                if not existing.scalar_one_or_none():
                    _seeds = [
                        PromptTemplate(
                            key="fp_extract", name="功能点提取", category="generation",
                            content=(
                                "你是一个资深测试分析专家。请按照以下分步推理流程，从需求文档中提取功能点：\n"
                                "\n"
                                "【推理流程】\n"
                                "步骤1：扫描需求全文，识别所有显式和隐式功能描述。\n"
                                "步骤2：将每个功能抽象为可独立测试的最小单元，合并语义相同的描述。\n"
                                "步骤3：按业务模块归类（如：登录注册、订单管理、数据报表）。\n"
                                "步骤4：为每个功能点评定优先级并输出结构化JSON。\n"
                                "\n"
                                "【优先级标注规则】\n"
                                "P0：核心业务流程，缺失将导致系统不可用。\n"
                                "P1：重要功能，影响主要用户场景但不阻塞核心流程。\n"
                                "P2：辅助功能或体验优化，可在后续版本实现。\n"
                                "\n"
                                "【输出格式 — 严格JSON】\n"
                                "{\n"
                                '  "functional_points": [\n'
                                "    {\n"
                                '      "module": "所属业务模块名称",\n'
                                '      "name": "功能点名称（简洁、可测试）",\n'
                                '      "category": "功能分类（如：增删改查 | 校验规则 | 交互反馈 | 权限控制 | 数据展示）",\n'
                                '      "desc": "功能的简明描述（1-2句话）",\n'
                                '      "priority": "P0 | P1 | P2"\n'
                                "    }\n"
                                "  ]\n"
                                "}\n"
                                "\n"
                                "【示例1 — 登录功能需求】\n"
                                "输入：\"用户可通过手机号+验证码或邮箱+密码两种方式登录，密码错误超过5次锁定30分钟。\"\n"
                                "输出：\n"
                                "{\n"
                                '  "functional_points": [\n'
                                '    {"module":"登录注册","name":"手机号验证码登录","category":"增删改查","desc":"用户输入手机号和短信验证码完成登录","priority":"P0"},\n'
                                '    {"module":"登录注册","name":"邮箱密码登录","category":"增删改查","desc":"用户输入邮箱和密码完成登录","priority":"P0"},\n'
                                '    {"module":"登录注册","name":"登录失败锁定","category":"校验规则","desc":"同一账号密码错误超过5次后锁定30分钟","priority":"P0"}\n'
                                "  ]\n"
                                "}\n"
                                "\n"
                                "【示例2 — 模糊需求处理】\n"
                                "输入：\"需要做一个列表页面，可以增删改查。\"\n"
                                "输出：\n"
                                "{\n"
                                '  "functional_points": [\n'
                                '    {"module":"数据管理","name":"列表数据展示","category":"数据展示","desc":"以表格形式分页展示数据列表，包含排序和筛选","priority":"P1"},\n'
                                '    {"module":"数据管理","name":"新增数据","category":"增删改查","desc":"通过表单新增一条数据记录","priority":"P0"},\n'
                                '    {"module":"数据管理","name":"编辑数据","category":"增删改查","desc":"点击编辑按钮修改已有数据","priority":"P0"},\n'
                                '    {"module":"数据管理","name":"删除数据","category":"增删改查","desc":"删除单条或多条数据，需二次确认","priority":"P0"}\n'
                                "  ]\n"
                                "}\n"
                                "\n"
                                "【边界情况处理】\n"
                                "- 需求模糊时：根据常见业务场景合理推断并标注，不要返回空列表。\n"
                                "- 需求跨多子系统时：在module字段中保留子系统前缀如\"订单系统-退款\"。\n"
                                "- 需求仅包含UI描述时：同时提取背后的数据和交互逻辑。\n"
                                "- 需求文档为空或无法理解时：返回{\"functional_points\":[],\"warning\":\"无法从输入中提取功能点，请提供更详细的需求描述。\"}\n"
                                "\n"
                                "需求文档：\n"
                                "{text}"
                            ),
                            variables=["text"], version=1, is_active=True,
                            description="从需求文档提取功能点列表并结构化输出",
                        ),
                        PromptTemplate(
                            key="tc_generate", name="测试用例生成", category="generation",
                            content=(
                                "你是一个测试用例设计专家，严格遵循等价类划分和边界值分析原则。请按以下分步推理流程生成测试用例：\n"
                                "\n"
                                "【推理流程】\n"
                                "步骤1：逐一分析每个功能点，识别所有可能的输入域和状态组合。\n"
                                "步骤2：对每个输入域应用等价类划分（有效等价类/无效等价类）推导测试场景。\n"
                                "步骤3：对数值/长度类输入应用边界值分析（最小值、最小值-1、最大值、最大值+1、中间值）。\n"
                                "步骤4：设计每个场景的具体操作步骤和断言点，输出结构化JSON。\n"
                                "\n"
                                "【设计约束】\n"
                                "- 每个功能点至少覆盖：1个正常流程 + 1个异常流程 + 1个边界场景。\n"
                                "- 步骤描述必须使用可执行的客观语言（\"在输入框中输入\" 而非 \"正常输入\"）。\n"
                                "- 预期结果必须可验证（\"页面跳转到首页\" 而非 \"系统正常响应\"）。\n"
                                "- P0功能点至少生成3条用例，P1至少2条，P2至少1条。\n"
                                "\n"
                                "【输出格式 — 严格JSON数组】\n"
                                "[\n"
                                "  {\n"
                                '    "title": "用例标题（简洁，如：登录成功-正确用户名密码）",\n'
                                '    "module": "所属业务模块",\n'
                                '    "priority": "P0 | P1 | P2",\n'
                                '    "precondition": "前置条件（如：已注册账号 admin/admin123）",\n'
                                '    "steps": ["步骤1：打开登录页", "步骤2：输入用户名admin", "步骤3：输入密码admin123", "步骤4：点击登录按钮"],\n'
                                '    "expected": ["预期1：页面跳转到首页", "预期2：右上角显示用户名"],\n'
                                '    "scenario_type": "正常流程 | 异常流程 | 边界场景"\n'
                                "  }\n"
                                "]\n"
                                "\n"
                                "【示例1 — 登录表单（正常+异常+边界）】\n"
                                "功能点：\"邮箱密码登录，用户名6-20字符，密码8-16字符\"\n"
                                "输出：\n"
                                "[\n"
                                '  {"title":"登录成功-正确用户名密码","module":"登录注册","priority":"P0","precondition":"已注册账号 test@mail.com / Pass1234","steps":["打开登录页","输入邮箱test@mail.com","输入密码Pass1234","点击登录"],"expected":["页面跳转到首页","右上角显示用户头像"],"scenario_type":"正常流程"},\n'
                                '  {"title":"登录失败-密码错误","module":"登录注册","priority":"P0","precondition":"同上账号","steps":["打开登录页","输入邮箱test@mail.com","输入错误密码WrongPass1","点击登录"],"expected":["页面显示\"密码错误\"提示","停留在登录页","密码输入框清空"],"scenario_type":"异常流程"},\n'
                                '  {"title":"登录失败-用户名为空","module":"登录注册","priority":"P1","precondition":"无","steps":["打开登录页","不输入任何用户名","输入密码Pass1234","点击登录"],"expected":["用户名输入框下方显示\"用户名不能为空\""],"scenario_type":"异常流程"},\n'
                                '  {"title":"用户名边界-6字符最小值","module":"登录注册","priority":"P1","precondition":"已注册6字符账号 a@b.co","steps":["打开登录页","输入用户名a@b.co","输入密码Pass1234","点击登录"],"expected":["登录成功，跳转到首页"],"scenario_type":"边界场景"},\n'
                                '  {"title":"用户名边界-5字符无效","module":"登录注册","priority":"P1","precondition":"无","steps":["打开登录页","输入用户名a@b.c","点击登录"],"expected":["显示\"用户名至少6个字符\""],"scenario_type":"边界场景"}\n'
                                "]\n"
                                "\n"
                                "【示例2 — 数值输入边界值分析】\n"
                                "功能点：\"商品数量输入框，允许1-999的整数\"\n"
                                "输出：\n"
                                "[\n"
                                '  {"title":"数量输入-正常值50","module":"订单管理","priority":"P1","precondition":"进入商品详情页","steps":["在数量输入框输入50","点击加入购物车"],"expected":["购物车显示该商品数量为50"],"scenario_type":"正常流程"},\n'
                                '  {"title":"数量输入-最小值1","module":"订单管理","priority":"P1","precondition":"进入商品详情页","steps":["在数量输入框输入1","点击加入购物车"],"expected":["购物车显示该商品数量为1"],"scenario_type":"边界场景"},\n'
                                '  {"title":"数量输入-最大值999","module":"订单管理","priority":"P1","precondition":"进入商品详情页","steps":["在数量输入框输入999","点击加入购物车"],"expected":["购物车显示该商品数量为999"],"scenario_type":"边界场景"},\n'
                                '  {"title":"数量输入-超上限1000","module":"订单管理","priority":"P1","precondition":"进入商品详情页","steps":["在数量输入框输入1000","点击加入购物车"],"expected":["显示\"数量不能超过999\"提示","购物车不更新"],"scenario_type":"边界场景"},\n'
                                '  {"title":"数量输入-下边界0","module":"订单管理","priority":"P1","precondition":"进入商品详情页","steps":["在数量输入框输入0","点击加入购物车"],"expected":["显示\"数量至少为1\"提示"],"scenario_type":"边界场景"}\n'
                                "]\n"
                                "\n"
                                "功能点：\n"
                                "{fps}"
                            ),
                            variables=["fps"], version=1, is_active=True,
                            description="根据功能点生成结构化测试用例（含等价类/边界值覆盖）",
                        ),
                        PromptTemplate(
                            key="operation_translate", name="操作指令翻译", category="execution",
                            content=(
                                "你是一个浏览器自动化操作翻译器。将自然语言测试步骤翻译为精确的浏览器操作指令。\n"
                                "\n"
                                "【可用操作类型】\n"
                                "click：点击元素（按钮/链接/菜单项）。\n"
                                "fill：向输入框填充文本（非 type，直接 set value）。\n"
                                "type：逐字键盘输入（用于触发实时搜索/自动补全）。\n"
                                "select：下拉选择框选取选项（按 value 或 label）。\n"
                                "hover：鼠标悬停在元素上（触发 tooltip/下拉菜单）。\n"
                                "scroll：滚动页面或元素内部滚动条（参数：x, y 或 selector）。\n"
                                "press_key：按下键盘按键（如 Enter、Escape、Tab、ArrowDown）。\n"
                                "goto：导航到指定URL。\n"
                                "wait：等待指定条件（参数：selector | ms | url_contains）。\n"
                                "assert：验证断言（子类型：url_contains | element_visible | text_present | element_count | input_value）。\n"
                                "\n"
                                "【输出格式 — 严格JSON】\n"
                                "{\n"
                                '  "action": "操作类型（click | fill | type | select | hover | scroll | press_key | goto | wait | assert）",\n'
                                '  "selector": "CSS/XPath选择器，复合操作可为null",\n'
                                '  "value": "操作参数（填充文本、URL、按键名等），无参数时为null",\n'
                                '  "options": {"可选的操作选项": "值"},\n'
                                '  "confidence": 0.0-1.0（对本次翻译的置信度）,\n'
                                '  "fallback_actions": [\n'
                                "    {\n"
                                '      "action": "后备操作类型",\n'
                                '      "selector": "后备选择器（更宽泛匹配）",\n'
                                '      "value": "后备参数",\n'
                                '      "reason": "触发该后备策略的原因"\n'
                                "    }\n"
                                "  ]\n"
                                "}\n"
                                "\n"
                                "【边界与异常处理规则】\n"
                                "- 超时：所有等待操作默认超时30秒（wait时添加 options.timeout=30000ms）。\n"
                                "- 弹窗/对话框：翻译前先判断步骤是否可能触发弹窗，若可能则添加 wait:selector=弹窗关闭按钮 或 press_key:Escape 作为 fallback。\n"
                                "- 元素过时重试（stale element）：遇到动态刷新列表时，优先用文本匹配而非索引选择器（如 text=\"确定\" 而非 nth=0）。\n"
                                "- iframe 感知：如果步骤涉及 iframe 内元素，selector 添加 frame_locator 前缀标注。\n"
                                "- 动态加载：涉及异步加载内容时，操作前自动插入 wait:selector 等待目标元素出现。\n"
                                "\n"
                                "【示例 — 成功翻译】\n"
                                "步骤：\"在搜索框输入手机，然后点击搜索按钮\"\n"
                                "URL：https://shop.example.com\n"
                                "输出：[\n"
                                '  {"action":"fill","selector":"input[placeholder*=\'搜索\']","value":"手机","options":null,"confidence":0.95,"fallback_actions":[{"action":"fill","selector":"input[type=\'search\']","value":"手机","reason":"备选搜索框选择器"}]},\n'
                                '  {"action":"wait","selector":null,"value":null,"options":{"ms":500},"confidence":0.8,"fallback_actions":[]},\n'
                                '  {"action":"click","selector":"button:has-text(\'搜索\')","value":null,"options":null,"confidence":0.9,"fallback_actions":[{"action":"press_key","selector":null,"value":"Enter","reason":"直接按回车触发搜索"}]}\n'
                                "]\n"
                                "\n"
                                "【示例 — 失败处理（元素定位失败）】\n"
                                "步骤：\"点击页面顶部的优惠券弹窗关闭按钮\"\n"
                                "URL：https://shop.example.com/products\n"
                                "输出：[\n"
                                '  {"action":"wait","selector":"[class*=\'coupon\'], [class*=\'popup\'], [class*=\'modal\']","value":null,"options":{"timeout":5000},"confidence":0.7,"fallback_actions":[]},\n'
                                '  {"action":"click","selector":"[class*=\'coupon\'] [class*=\'close\'], [class*=\'popup\'] button[class*=\'close\'], .modal .close-btn","value":null,"options":null,"confidence":0.55,"fallback_actions":[{"action":"press_key","selector":null,"value":"Escape","reason":"弹窗关闭按钮未找到，尝试Esc键关闭"},{"action":"click","selector":"body","value":null,"reason":"最后尝试点击页面空白区域关闭弹窗"}]}\n'
                                "]\n"
                                "\n"
                                "用户步骤：\n"
                                "{step}\n"
                                "\n"
                                "页面 URL：\n"
                                "{url}"
                            ),
                            variables=["step", "url"], version=1, is_active=True,
                            description="将自然语言步骤翻译为浏览器操作JSON（含回退策略）",
                        ),
                        PromptTemplate(
                            key="verify_expected", name="预期结果验证", category="verification",
                            content=(
                                "你是一个测试结果验证专家。请按三级验证策略判断预期结果是否达成。\n"
                                "\n"
                                "【三级验证策略 — 按优先级递减尝试】\n"
                                "第1级「精确匹配」：预期值与实际值字面一致（如\"页面标题为首页\" → 实际标题=\"首页\"）。最高置信度。\n"
                                "第2级「语义匹配」：预期描述与实际含义等价但表述不同（如\"显示用户名\" → 实际显示\"欢迎回来，张三\"）。检查核心关键词和语义。\n"
                                "第3级「存在性检测」：仅验证某元素/文本是否存在（如\"出现错误提示\" → 页面存在包含\"错误\"的文本）。最低置信度，仅用于宽泛断言。\n"
                                "\n"
                                "【容忍规则 — 以下情况不算失败】\n"
                                "- 时间戳/日期：预期中带有\"当前时间\"→接受任意合法时间字符串。如\"登录时间：YYYY-MM-DD HH:mm:ss\"。\n"
                                "- 动态ID/Token：\"order_id=ABC123\" 实际显示 \"order_id=XYZ789\" → 仅比对格式，不比对具体值。\n"
                                "- 数字范围：\"约100条记录\" 实际 98条 → 容差 ±5% 内视为通过。\n"
                                "- 异步加载：页面仍在渲染中，给出结论时标注\"页面可能未完全加载\"并降级置信度。\n"
                                "\n"
                                "【证据链格式】\n"
                                "输出时必须引用DOM快照中的具体行号作为证据：\n"
                                "- 若从DOM快照验证：标注\"见DOM行N：<原文>\"。\n"
                                "- 若从截图验证：标注\"截图显示XXX区域存在/不存在目标内容\"。\n"
                                "- 不可凭空断言真实性，必须绑定到具体观测。\n"
                                "\n"
                                "【输出格式】\n"
                                "{\n"
                                '  "verdict": "pass | fail | partial",\n'
                                '  "confidence": 0.0-1.0,\n'
                                '  "matched_level": "exact | semantic | presence",\n'
                                '  "reason": "验证结论的中文说明（引用具体证据）",\n'
                                '  "evidence": ["证据1：见DOM行15 — 页面标题为\\"首页\\"", "证据2：见DOM行23 — 用户名span包含\\"张三\\""]\n'
                                "}\n"
                                "\n"
                                "【中文验证示例1 — 精确匹配通过】\n"
                                "操作：goto https://example.com\n"
                                "预期：\"页面标题显示为'示例网站首页'\"\n"
                                "DOM快照：第5行 <title>示例网站首页</title>\n"
                                "输出：{\"verdict\":\"pass\",\"confidence\":0.98,\"matched_level\":\"exact\",\"reason\":\"页面标题与预期完全一致\",\"evidence\":[\"见DOM行5：<title>示例网站首页</title>\"]}\n"
                                "\n"
                                "【中文验证示例2 — 语义匹配通过】\n"
                                "操作：click 登录按钮后 fill 用户名\n"
                                "预期：\"登录成功后右上角显示用户名\"\n"
                                "DOM快照：第12行 <span class=\"user-name\">欢迎，admin@test.com</span>\n"
                                "输出：{\"verdict\":\"pass\",\"confidence\":0.85,\"matched_level\":\"semantic\",\"reason\":\"用户名admin@test.com出现在右上角用户信息区域，语义符合\\\"显示用户名\\\"\",\"evidence\":[\"见DOM行12：<span class=\\\"user-name\\\">欢迎，admin@test.com</span>\"]}\n"
                                "\n"
                                "【中文验证示例3 — 存在性检测失败】\n"
                                "操作：提交空表单\n"
                                "预期：\"用户名输入框下方出现'必填'红色提示\"\n"
                                "DOM快照：无任何包含\"必填\"的文本节点，input标签无aria-invalid属性\n"
                                "输出：{\"verdict\":\"fail\",\"confidence\":0.92,\"matched_level\":\"presence\",\"reason\":\"DOM中未找到\\\"必填\\\"提示文本，input元素缺少表单校验标记\",\"evidence\":[\"遍历全部DOM文本节点，未匹配到\\\"必填\\\"关键词\",\"input元素未设置aria-invalid=\\\"true\\\"属性\"]}\n"
                                "\n"
                                "【中文验证示例4 — 时间戳容忍通过】\n"
                                "操作：创建订单后查看订单详情\n"
                                "预期：\"创建时间显示为当前时间\"\n"
                                "DOM快照：第45行 <span class=\"create-time\">2026-07-24 15:32:18</span>\n"
                                "输出：{\"verdict\":\"pass\",\"confidence\":0.78,\"matched_level\":\"semantic\",\"reason\":\"订单创建时间格式正确，符合当前时间上下文（容忍规则-时间戳）\",\"evidence\":[\"见DOM行45：时间格式YYYY-MM-DD HH:mm:ss正确\"]}\n"
                                "\n"
                                "操作：\n"
                                "{action}\n"
                                "\n"
                                "预期结果：\n"
                                "{expected}"
                            ),
                            variables=["action", "expected"], version=1, is_active=True,
                            description="用三级策略验证测试步骤的预期结果（含时间戳容忍）",
                        ),
                    ]
                    _prompt_db.add_all(_seeds)
                    await _prompt_db.commit()
                    logger.info("种子提示词数据写入完成")
        except Exception:
            logger.warning("种子提示词数据写入失败（非关键错误，继续）")

    from app.auth import hash_password

    async with AsyncSessionLocal() as _init_db:
        try:
            existing = await _init_db.execute(
                select(db_models.User).where(
                    db_models.User.username == settings.default_admin_username
                )
            )
            if not existing.scalar_one_or_none():
                _admin_password = "Admin@2024"
                admin = db_models.User(
                    username=settings.default_admin_username,
                    password_hash=hash_password(_admin_password),
                    role="admin",
                    status="active",
                    must_change_password=True,
                )
                _init_db.add(admin)
                await _init_db.commit()
                logger.info("默认管理员已创建: %s", settings.default_admin_username)

            # Seed prompt templates
            from app.gen.analyzer import get_default_prompts
            _default_prompts = get_default_prompts()
            for key, d in _default_prompts.items():
                result = await _init_db.execute(
                    select(db_models.PromptTemplate).where(
                        db_models.PromptTemplate.template_key == key
                    )
                )
                existing = result.scalar_one_or_none()
                if not existing:
                    _init_db.add(db_models.PromptTemplate(
                        template_key=key,
                        label=d["label"],
                        template_content=d["content"],
                        is_custom=False,
                    ))
                    logger.info("默认提示词模板已创建: %s", key)
                elif not existing.is_custom:
                    existing.template_content = d["content"]
                    existing.label = d["label"]
                    logger.info("默认提示词模板已更新: %s", key)
            await _init_db.commit()
        except Exception:
            await _init_db.rollback()
            raise

    # Check for missing columns across all models and add them
    try:
        from sqlalchemy import inspect, text
        from app.models.project import Environment
        async with engine.connect() as _conn:
            def _check_cols(sync_conn):
                insp = inspect(sync_conn)
                cols = {c["name"] for c in insp.get_columns("environments")}
                missing = []
                for col in Environment.__table__.c:
                    if col.name not in cols:
                        missing.append(col.name)
                for name in missing:
                    col_type = "JSON"  # cookies is JSON
                    sync_conn.execute(text(
                        f"ALTER TABLE environments ADD COLUMN {name} {col_type}"
                    ))
                    logger.info("已补列: environments.%s", name)
            await _conn.run_sync(_check_cols)
    except Exception as _e:
        logger.warning("列迁移失败: %s", _e, exc_info=True)

    # Clean up expired sessions at startup
    from app.auth import cleanup_expired_sessions
    async with db_mod.AsyncSessionLocal() as _cleanup_db:
        try:
            await cleanup_expired_sessions(_cleanup_db)
            logger.info("过期会话清理完成")
        except SQLAlchemyError as _e:
            logger.warning("过期会话清理失败: %s", _e, exc_info=True)

    # 恢复孤儿 agent_runs（服务重启后清理假 running 状态）
    await _recover_orphaned_agent_runs()


async def _periodic_session_cleanup():
    """后台周期任务：每 900 秒清理一次过期会话。"""
    while True:
        await asyncio.sleep(900)
        try:
            from app.auth import cleanup_expired_sessions
            async with db_mod.AsyncSessionLocal() as _db:
                await cleanup_expired_sessions(_db)
                logger.info("周期性过期会话清理完成")
        except SQLAlchemyError as e:
            logger.warning("周期性过期会话清理失败: %s", e, exc_info=True)
        try:
            from app.routers.recordings.state import cleanup_stale_sessions
            await cleanup_stale_sessions()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await _run_startup_init()
    except Exception as e:
        logger.warning("数据库初始化失败，进入配置模式: %s", e)
        logger.warning("请通过 /setup 页面配置 PostgreSQL 数据库后重启")
    cleanup_task = asyncio.create_task(_periodic_session_cleanup())

    # ── 启动 agent 待执行轮询（跨 worker 调度） ──
    try:
        from agent.manager import agent_manager
        asyncio.create_task(agent_manager.start_poller())
        logger.info("Agent Mgmt: cross-worker pending execution poller started")
    except Exception:
        pass

    try:
        from app.scheduler import scheduler, start_scheduler

        async def _scheduled_task_executor(task) -> None:
            """将定时任务分发到用例/模块/项目执行入口。"""
            from app import crud
            from app.database import AsyncSessionLocal
            from app.routers.testcase import execution as _exec

            task_type = getattr(task, "task_type", None)
            target_id = getattr(task, "target_id", None)
            if not task_type or target_id is None:
                logger.error("定时任务缺少 task_type/target_id: %s", getattr(task, "id", "?"))
                return

            async with AsyncSessionLocal() as db:
                if task_type == "testcase":
                    tc = await crud.get_test_case(db, int(target_id))
                    if not tc:
                        logger.error("定时任务用例不存在: %s", target_id)
                        return
                    batch = await crud.create_run_batch(
                        db, project_id=tc.project_id, total_cases=1,
                        triggered_by=f"scheduler:{getattr(task, 'name', task.id)}",
                    )
                    batch_id, case_id = batch.id, tc.id
                    await db.commit()
                    await _exec.run_test_case(case_id, batch_id)
                elif task_type == "module":
                    mod = await crud.get_module(db, int(target_id))
                    if not mod:
                        logger.error("定时任务模块不存在: %s", target_id)
                        return
                    cases = await crud.get_all_test_cases_for_module(db, int(target_id))
                    if not cases:
                        logger.warning("定时任务模块无用例: %s", target_id)
                        return
                    case_ids = [c.id for c in cases]
                    batch = await crud.create_run_batch(
                        db, project_id=mod.project_id, total_cases=len(case_ids),
                        triggered_by=f"scheduler:{getattr(task, 'name', task.id)}",
                    )
                    batch_id, project_id = batch.id, mod.project_id
                    await db.commit()
                    await _exec.run_batch_test_cases(case_ids, project_id, batch_id=batch_id)
                elif task_type == "project":
                    proj = await crud.get_project(db, int(target_id))
                    if not proj:
                        logger.error("定时任务项目不存在: %s", target_id)
                        return
                    cases = await crud.get_all_test_cases_for_project(db, int(target_id))
                    if not cases:
                        logger.warning("定时任务项目无用例: %s", target_id)
                        return
                    case_ids = [c.id for c in cases]
                    batch = await crud.create_run_batch(
                        db, project_id=int(target_id), total_cases=len(case_ids),
                        triggered_by=f"scheduler:{getattr(task, 'name', task.id)}",
                    )
                    batch_id = batch.id
                    await db.commit()
                    await _exec.run_batch_test_cases(case_ids, int(target_id), batch_id=batch_id)
                else:
                    logger.error("未知定时任务类型: %s", task_type)

        scheduler.set_executor(_scheduled_task_executor)
        await start_scheduler()
        logger.info("定时调度器已启动（executor 已接线）")
    except Exception as e:
        logger.warning("定时调度器启动失败: %s", e)
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="UI测试自动化平台",
    description="用于管理和运行Playwright UI测试的Web平台。",
    version="1.0.0",
    lifespan=lifespan,
)

# Rate limiting
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.rate_limiter import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
from fastapi.middleware.cors import CORSMiddleware
origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods.split(",") if settings.cors_allow_methods != "*" else ["*"],
    allow_headers=settings.cors_allow_headers.split(",") if settings.cors_allow_headers != "*" else ["*"],
)

from app.exception_handlers import unhandled_exception_handler
app.add_exception_handler(Exception, unhandled_exception_handler)

# CSRF
from app.middleware.csrf import CSRFMiddleware, generate_csrf_token
app.add_middleware(CSRFMiddleware)

WS_AUTH_SKIP_PREFIXES = ["/api/agents/ws/"]
SETUP_PATHS = {"/setup", "/api/setup/status", "/api/setup/database"}
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/login-form", "/api/auth/logout", "/health", "/docs", "/openapi.json", *SETUP_PATHS}
PROTECTED_PREFIXES = ["/api/", "/reports/"]


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_protected = any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)
    if is_protected and path not in PUBLIC_PATHS:
        if any(path.startswith(skip) for skip in WS_AUTH_SKIP_PREFIXES):
            return await call_next(request)
        session_id = request.cookies.get("session_id")
        if not session_id:
            return JSONResponse(status_code=401, content={"detail": "未登录"})
        if db_mod.AsyncSessionLocal is None:
            return JSONResponse(status_code=503, content={"detail": "数据库未配置"})
        from app.auth import get_session
        async with db_mod.AsyncSessionLocal() as db:
            try:
                session = await get_session(db, session_id)
                if not session:
                    return JSONResponse(status_code=401, content={"detail": "会话已过期"})
                result = await db.execute(
                    select(db_models.User).where(db_models.User.id == session.user_id)
                )
                user = result.scalar_one_or_none()
                if not user or user.status == "disabled":
                    return JSONResponse(status_code=401, content={"detail": "账号已禁用"})
            except SQLAlchemyError:
                logger.exception("Database error in auth_middleware — denying request")
                return JSONResponse(status_code=503, content={"detail": "服务暂时不可用"})
    return await call_next(request)


PASSWORD_CHANGE_WHITELIST = {
    "/api/auth/login",
    "/api/auth/login-form",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/change-password",
}


@app.middleware("http")
async def enforce_password_changed(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in PASSWORD_CHANGE_WHITELIST:
        return await call_next(request)
    if any(path.startswith(skip) for skip in WS_AUTH_SKIP_PREFIXES):
        return await call_next(request)
    session_id = request.cookies.get("session_id")
    if not session_id:
        return await call_next(request)
    from app.auth import get_session
    async with db_mod.AsyncSessionLocal() as db:
        try:
            session = await get_session(db, session_id)
            if not session:
                return await call_next(request)
            result = await db.execute(
                select(db_models.User).where(db_models.User.id == session.user_id)
            )
            user = result.scalar_one_or_none()
            if user and user.must_change_password:
                return JSONResponse(status_code=403, content={"detail": "请先修改默认密码"})
        except SQLAlchemyError:
            logger.exception("Database error in enforce_password_changed — allowing request to proceed with caution")
            pass
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or request.cookies.get("request_id") or uuid.uuid4().hex[:12]
    set_request_id(rid)
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(_root, "reports"), exist_ok=True)
os.makedirs(os.path.join(_root, "logs"), exist_ok=True)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

_assets_dir = os.path.join(_root, "app", "static", "assets")
if os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

app.mount("/reports", StaticFiles(directory="reports"), name="reports")

templates = Jinja2Templates(directory="app/templates")

app.include_router(project_router.router)
app.include_router(testcase_router.router)
app.include_router(module_router.router)
app.include_router(report_router.router)
app.include_router(auth_router.router)
app.include_router(user_router.router)
app.include_router(config_router.router)
app.include_router(environment_router.router)
app.include_router(scheduler_router.router)
app.include_router(mgmt_agent_router.router)
app.include_router(audit_router.router)
app.include_router(gen_router.router)
app.include_router(recordings_router.router)
app.include_router(notification_router.router)
app.include_router(setup_router.router)
app.include_router(agent_definition_router.router)
app.include_router(agent_run_router.router)

if AGENT_SUPPORT:
    app.include_router(agent_router)

app.websocket("/ws/logs/{run_id}")(websocket_logs)


async def _agent_run_websocket(websocket: WebSocket, run_id: int):
    """Agent 运行实时观察 WebSocket 端点。

    与 /ws/logs/{run_id} 模式一致：
    - 客户端连接后持续接收 agent_run 的状态推送
    - 支持 ping/pong 心跳保持连接
    """
    await agent_run_ws.connect(websocket, run_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = _json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(_json.dumps({"type": "pong"}))
            except _json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await agent_run_ws.disconnect(websocket, run_id)
    except (RuntimeError, ConnectionError) as exc:
        logger.warning("Agent run WebSocket 错误 (run_id=%s): %s", run_id, exc)
        await agent_run_ws.disconnect(websocket, run_id)


app.websocket("/ws/agent-runs/{run_id}")(_agent_run_websocket)


_SPA_INDEX = os.path.join(_root, "app", "static", "index.html")


def _serve_spa():
    if os.path.isfile(_SPA_INDEX):
        with open(_SPA_INDEX, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Frontend not built</h1>", status_code=503)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return _serve_spa()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _serve_spa()


@app.get("/health")
async def health_check():
    """增强型健康检查 — 包含 DB + BrowserPool 探活。"""
    db_status = "ok"
    browser_status = "unknown"
    if db_mod.AsyncSessionLocal is not None:
        try:
            from sqlalchemy import text
            async with db_mod.AsyncSessionLocal() as _hc_db:
                await _hc_db.execute(text("SELECT 1"))
        except Exception as e:
            db_status = f"error: {e}"
    else:
        db_status = "not configured"

    try:
        from core.browser_pool import BrowserPool
        async with BrowserPool._lock:
            active = len(BrowserPool._instances)
        browser_status = f"{active} active"
    except Exception as e:
        browser_status = f"error: {e}"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "database": db_status,
        "browser_pool": browser_status,
    }


@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, path: str):
    if path.startswith("api/") or path.startswith("static/") or path.startswith("assets/") or path.startswith("reports/"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return _serve_spa()


def start():
    logger.info("在 http://%s:%s 启动服务器", APP_HOST, APP_PORT)
    uvicorn.run(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=True,
        reload_dirs=["app", "core", "agent"],
        reload_excludes=[
            "*.db", "*.db-wal", "*.db-shm",
            "reports/*", "logs/*",
            "*.pyc", "__pycache__/*",
            "frontend/*", "node_modules/*",
        ],
    )


if __name__ == "__main__":
    start()
