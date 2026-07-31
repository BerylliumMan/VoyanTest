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
        async def _ddl(sql: str, label: str) -> None:
            """Run one DDL statement with AUTOCOMMIT; log real errors."""
            try:
                async with engine.connect() as conn:
                    await conn.execution_options(isolation_level="AUTOCOMMIT")
                    await conn.execute(text(sql))
            except Exception:
                logger.warning("%s 失败（非关键，继续）: %s", label, sql, exc_info=True)

        await _ddl(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(255)",
            "users.nickname 迁移",
        )
        await _ddl(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
            "users.email 迁移",
        )
        await _ddl(
            "ALTER TABLE run_batches ADD COLUMN IF NOT EXISTS triggered_by VARCHAR(255)",
            "run_batches.triggered_by 迁移",
        )
        await _ddl(
            "ALTER TABLE recording_sessions ADD COLUMN IF NOT EXISTS events_data TEXT",
            "recording_sessions.events_data 迁移",
        )
        await _ddl(
            "ALTER TABLE test_runs ALTER COLUMN case_id DROP NOT NULL",
            "test_runs.case_id DROP NOT NULL 迁移",
        )
        for _col_sql in (
            "ALTER TABLE gen_sessions ADD COLUMN IF NOT EXISTS user_id INTEGER",
            "ALTER TABLE gen_sessions ADD COLUMN IF NOT EXISTS progress INTEGER DEFAULT 0",
            "ALTER TABLE gen_sessions ADD COLUMN IF NOT EXISTS progress_message VARCHAR(500)",
            "ALTER TABLE gen_test_cases ADD COLUMN IF NOT EXISTS validation_errors TEXT",
        ):
            await _ddl(_col_sql, "gen 表字段迁移")
        await _ddl(
            "ALTER TABLE ai_configs ADD COLUMN IF NOT EXISTS max_context_tokens INTEGER DEFAULT 131072",
            "ai_configs.max_context_tokens 迁移",
        )
        await _ddl(
            "ALTER TABLE test_steps ADD COLUMN IF NOT EXISTS learned_locator JSONB",
            "test_steps.learned_locator 迁移",
        )
        await _ddl(
            "ALTER TABLE test_steps ADD COLUMN IF NOT EXISTS structured_step JSONB",
            "test_steps.structured_step 迁移",
        )
        await _ddl(
            "ALTER TABLE test_steps ADD COLUMN IF NOT EXISTS cacheable BOOLEAN DEFAULT TRUE",
            "test_steps.cacheable 迁移",
        )
        await _ddl(
            "ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS case_kind VARCHAR(32) DEFAULT 'functional'",
            "test_cases.case_kind 迁移",
        )
        await _ddl(
            "ALTER TABLE test_cases ALTER COLUMN case_kind SET DEFAULT 'functional'",
            "test_cases.case_kind 默认值改为 functional",
        )
        await _ddl(
            "ALTER TABLE gen_sessions ADD COLUMN IF NOT EXISTS case_kind VARCHAR(32) DEFAULT 'ui'",
            "gen_sessions.case_kind 迁移",
        )
        await _ddl(
            """
            CREATE TABLE IF NOT EXISTS schema_patches (
                id VARCHAR(64) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "schema_patches 表",
        )
        # 一次性：拆菜单前加列默认 ui，导致历史用例全进「UI自动化」；改归「功能」
        try:
            if AsyncSessionLocal is not None:
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import text as _t
                    done = (
                        await db.execute(
                            _t("SELECT 1 FROM schema_patches WHERE id = 'case_kind_history_to_functional'")
                        )
                    ).scalar()
                    if not done:
                        result = await db.execute(
                            _t("UPDATE test_cases SET case_kind = 'functional' WHERE case_kind = 'ui'")
                        )
                        await db.execute(
                            _t(
                                "INSERT INTO schema_patches (id) VALUES ('case_kind_history_to_functional') "
                                "ON CONFLICT (id) DO NOTHING"
                            )
                        )
                        await db.commit()
                        logger.info(
                            "历史用例 case_kind 已迁到 functional，rowcount=%s",
                            result.rowcount,
                        )
                    # 一次性：生成页 P0/P1/P2 导入时未映射，全落成 medium；按 gen_test_cases 标题回填
                    done_pri = (
                        await db.execute(
                            _t("SELECT 1 FROM schema_patches WHERE id = 'priority_backfill_from_gen'")
                        )
                    ).scalar()
                    if not done_pri:
                        from app.gen.adapter import normalize_priority_to_storage
                        rows = (
                            await db.execute(
                                _t(
                                    "SELECT tc.id, g.priority FROM test_cases tc "
                                    "JOIN gen_test_cases g ON g.title = tc.name "
                                    "WHERE g.priority IS NOT NULL AND TRIM(g.priority) <> ''"
                                )
                            )
                        ).all()
                        updated = 0
                        for tc_id, gen_pri in rows:
                            stored = normalize_priority_to_storage(gen_pri)
                            await db.execute(
                                _t("UPDATE test_cases SET priority = :p WHERE id = :id"),
                                {"p": stored, "id": tc_id},
                            )
                            updated += 1
                        await db.execute(
                            _t(
                                "INSERT INTO schema_patches (id) VALUES ('priority_backfill_from_gen') "
                                "ON CONFLICT (id) DO NOTHING"
                            )
                        )
                        await db.commit()
                        logger.info(
                            "用例优先级已按生成结果回填，matched=%s",
                            updated,
                        )
        except Exception:
            logger.warning("case_kind 历史数据回填失败（非关键，继续）", exc_info=True)
        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
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
            logger.warning("prompt_templates 表创建失败（非关键错误，继续）", exc_info=True)
        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
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
            logger.warning("agent_definitions 表创建失败（非关键错误，继续）", exc_info=True)

        # ── agent_definitions 列迁移 ────────────────────────────────────────
        await _ddl(
            "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS system_prompt TEXT",
            "agent_definitions.system_prompt 迁移",
        )
        await _ddl(
            "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS tools JSONB DEFAULT '[]'",
            "agent_definitions.tools 迁移",
        )
        await _ddl(
            "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS goal TEXT DEFAULT ''",
            "agent_definitions.goal 迁移",
        )
        await _ddl(
            "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS constraints JSONB DEFAULT '[]'",
            "agent_definitions.constraints 迁移",
        )
        await _ddl(
            "ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS thinking_config JSONB DEFAULT '{}'",
            "agent_definitions.thinking_config 迁移",
        )

        # ── agent_runs 相关表创建 ────────────────────────────────────────────
        for _label, _sql in (
            ("agent_runs 表创建", """
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
            """),
            ("agent_messages 表创建", """
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
            """),
            ("agent_tool_calls 表创建", """
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
            """),
            ("agent_run_snapshots 表创建", """
                CREATE TABLE IF NOT EXISTS agent_run_snapshots (
                    id              SERIAL PRIMARY KEY,
                    run_id          INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    turn_number     INTEGER NOT NULL,
                    context_json    JSONB NOT NULL,
                    compressed_count INTEGER DEFAULT 0,
                    token_count     INTEGER DEFAULT 0,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """),
        ):
            await _ddl(_sql, _label)

        # ── 种子提示词 + 默认 Agent（与参考环境一致）────────────────────────
        try:
            async with AsyncSessionLocal() as _prompt_db:
                from app.seed_defaults import seed_defaults
                await seed_defaults(_prompt_db)
        except Exception:
            logger.warning("种子提示词/Agent 写入失败（非关键错误，继续）", exc_info=True)

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

            # 补种缺失的提示词 / Agent（幂等；首次 setup 已种过则跳过）
            from app.seed_defaults import seed_defaults
            await seed_defaults(_init_db)
            await _init_db.commit()
        except Exception:
            await _init_db.rollback()
            raise

    # Check for missing columns across all models and add them
    try:
        # 勿在此函数内再 import text：会遮蔽模块级 text，导致前面全部 DDL 报 UnboundLocalError
        from sqlalchemy import inspect
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
