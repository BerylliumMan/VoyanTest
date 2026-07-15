# app/main.py
import asyncio
from contextlib import asynccontextmanager
import json as _json
import logging
import os
import uuid

from fastapi import FastAPI, Request, Response
from sqlalchemy import select
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
from .routers import auth_router, user_router, audit_router, agent_router as mgmt_agent_router, gen_router, recordings_router, notification_router, setup_router
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

    # 字段迁移（始终执行，不与 DISABLE_CREATE_ALL 关联）
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
    try:
        from app.scheduler import start_scheduler
        await start_scheduler()
        logger.info("定时调度器已启动")
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

if AGENT_SUPPORT:
    app.include_router(agent_router)

app.websocket("/ws/logs/{run_id}")(websocket_logs)

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
