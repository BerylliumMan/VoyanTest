# app/main.py
import asyncio
from contextlib import asynccontextmanager
import json as _json
import logging
import os
import uuid

from fastapi import FastAPI, Request, Response
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
from .routers import auth_router, user_router, audit_router, agent_router as mgmt_agent_router, gen_router, recordings_router
from app.config import get_settings
from app.websocket import websocket_logs

settings = get_settings()
APP_HOST = settings.app_host
APP_PORT = settings.app_port

# 生产环境检查：session_secret_key 不可为空
if not settings.session_secret_key:
    logger.warning(
        "SESSION_SECRET_KEY 未设置！生产环境中请务必设置该值，"
        "否则 session 签名的安全性无法保证。开发环境可忽略此警告。"
    )
from app.database import engine
from app import db_models
from app.db_models import Base
import uvicorn

# Import agent router
try:
    from agent.router import router as agent_router
    AGENT_SUPPORT = True
except ImportError:
    AGENT_SUPPORT = False
    logger.warning("Agent support not available")

def _run_startup_init():
    """Run synchronous DB initialization at startup (not at import time).

    Called once during FastAPI lifespan startup.
    """
    # Schema management: create_all as dev fallback
    if os.getenv("DISABLE_CREATE_ALL", "false").lower() != "true":
        Base.metadata.create_all(bind=engine)
    else:
        logger.info("DISABLE_CREATE_ALL=true，跳过 create_all（生产模式，请确保已执行 alembic upgrade head）")

    # Fallback column migration: environments.cookies
    try:
        import sqlalchemy as _sa
        _sa_text = _sa.text
        with engine.connect() as conn:
            rows = conn.execute(_sa_text("PRAGMA table_info(environments)")).fetchall()
            has_cookies = any(r[1] == "cookies" for r in rows)
            if not has_cookies:
                conn.execute(_sa_text("ALTER TABLE environments ADD COLUMN cookies JSON"))
                conn.commit()
                logger.info("已为 environments 表补充 cookies 列（启动兜底迁移）")
    except SQLAlchemyError as _exc:
        logger.warning("启动兜底迁移 environments.cookies 失败: %s", _exc, exc_info=True)

    # Admin init + AI config seed + prompt templates
    from app.database import SessionLocal as _SessionLocal
    from app.auth import hash_password
    _init_db = _SessionLocal()
    try:
        existing = _init_db.query(db_models.User).filter(
            db_models.User.username == settings.default_admin_username
        ).first()
        if not existing:
            admin = db_models.User(
                username=settings.default_admin_username,
                password_hash=hash_password(settings.default_admin_password),
                role="admin",
                status="active",
                must_change_password=True,
            )
            _init_db.add(admin)
            _init_db.commit()
            logger.info(f"默认管理员已创建: {settings.default_admin_username}")

        # One-shot migration: seed ai_configs from config.json if DB is empty.
        if not _init_db.query(db_models.AIConfig).first():
            _config_json_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
            )
            try:
                with open(_config_json_path) as _f:
                    _ai = _json.load(_f).get("ai", {})
                if _ai.get("model") and _ai.get("api_key") and _ai.get("api_base"):
                    _init_db.add(db_models.AIConfig(
                        id=1,
                        model=_ai["model"],
                        api_key=_ai["api_key"],
                        api_base=_ai["api_base"],
                        temperature=float(_ai.get("temperature", 0.1)),
                    ))
                    _init_db.commit()
                    logger.info(f"AI 配置已从 config.json 迁移到数据库: model={_ai['model']}")
            except FileNotFoundError:
                logger.warning("config.json 不存在且数据库无 AI 配置 — 启动后必须通过 API 配置 AI")
            except (_json.JSONDecodeError, KeyError) as _e:
                logger.warning("config.json 格式错误，AI 配置未迁移: %s", _e, exc_info=True)

        # Seed default prompt templates
        from app.gen.analyzer import get_default_prompts
        _default_prompts = get_default_prompts()
        for key, d in _default_prompts.items():
            existing = _init_db.query(db_models.PromptTemplate).filter(
                db_models.PromptTemplate.template_key == key
            ).first()
            if not existing:
                _init_db.add(db_models.PromptTemplate(
                    template_key=key,
                    label=d["label"],
                    template_content=d["content"],
                    is_custom=False,
                ))
                logger.info(f"默认提示词模板已创建: {key}")
            elif not existing.is_custom:
                existing.template_content = d["content"]
                existing.label = d["label"]
                logger.info(f"默认提示词模板已更新: {key}")
        _init_db.commit()
    finally:
        _init_db.close()

    # Clean up expired sessions at startup
    _cleanup_db = _SessionLocal()
    try:
        from app.auth import cleanup_expired_sessions
        cleanup_expired_sessions(_cleanup_db)
        logger.info("过期会话清理完成")
    except SQLAlchemyError as _e:
        logger.warning("过期会话清理失败: %s", _e, exc_info=True)
    finally:
        _cleanup_db.close()


# 周期性清理过期会话（每15分钟）
async def _periodic_session_cleanup():
    """后台周期任务：每 900 秒清理一次过期会话。"""
    while True:
        await asyncio.sleep(900)
        try:
            from app.auth import cleanup_expired_sessions
            from app.database import SessionLocal
            _db = SessionLocal()
            try:
                cleanup_expired_sessions(_db)
                logger.info("周期性过期会话清理完成")
            finally:
                _db.close()
        except SQLAlchemyError as e:
            logger.warning("周期性过期会话清理失败: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: runs DB init on startup, cleanup on shutdown."""
    _run_startup_init()
    cleanup_task = asyncio.create_task(_periodic_session_cleanup())
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

# CORS 中间件
from fastapi.middleware.cors import CORSMiddleware
origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods.split(",") if settings.cors_allow_methods != "*" else ["*"],
    allow_headers=settings.cors_allow_headers.split(",") if settings.cors_allow_headers != "*" else ["*"],
)

# CSRF 保护由 SameSite=Lax Cookie 提供，无需额外中间件

# 全局异常处理
from app.exception_handlers import unhandled_exception_handler
app.add_exception_handler(Exception, unhandled_exception_handler)

# WebSocket 路径前缀 — 这些路径在 WebSocket handler 内部自行处理认证
WS_AUTH_SKIP_PREFIXES = ["/api/agents/ws/"]

# T016: 全局认证中间件 — 所有 /api/ 路径需登录（白名单除外）
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/login-form", "/api/auth/logout", "/health", "/docs", "/openapi.json"}
# 需要认证的路径前缀（生产环境建议由 nginx/reverse proxy 处理静态文件认证）
PROTECTED_PREFIXES = ["/api/", "/reports/"]

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_protected = any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)
    if is_protected and path not in PUBLIC_PATHS:
        # WebSocket 路径由 handler 内部自行认证，跳过 HTTP 中间件
        if any(path.startswith(skip) for skip in WS_AUTH_SKIP_PREFIXES):
            return await call_next(request)
        session_id = request.cookies.get("session_id")
        if not session_id:
            return JSONResponse(status_code=401, content={"detail": "未登录"})
        from app.auth import get_session
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            session = get_session(db, session_id)
            if not session:
                return JSONResponse(status_code=401, content={"detail": "会话已过期"})
            user = db.query(db_models.User).filter(db_models.User.id == session.user_id).first()
            if not user or user.status == "disabled":
                return JSONResponse(status_code=401, content={"detail": "账号已禁用"})
        finally:
            db.close()
    return await call_next(request)

# P0-C: 强制修改默认密码中间件 — must_change_password=True 的用户只能访问白名单路径
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
    # WebSocket 路径由 handler 内部自行认证
    if any(path.startswith(skip) for skip in WS_AUTH_SKIP_PREFIXES):
        return await call_next(request)
    session_id = request.cookies.get("session_id")
    if not session_id:
        return await call_next(request)
    from app.auth import get_session
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        session = get_session(db, session_id)
        if not session:
            return await call_next(request)
        user = db.query(db_models.User).filter(db_models.User.id == session.user_id).first()
        if user and user.must_change_password:
            return JSONResponse(status_code=403, content={"detail": "请先修改默认密码"})
    finally:
        db.close()
    return await call_next(request)

# ── request_id 注入（最后定义 = 最外层，确保所有响应都带该头） ──
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or request.cookies.get("request_id") or uuid.uuid4().hex[:12]
    set_request_id(rid)
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# Ensure runtime directories exist (relative to project root)
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(_root, "reports"), exist_ok=True)
os.makedirs(os.path.join(_root, "logs"), exist_ok=True)

# 挂载静态文件（保留旧 CSS/JS，构建后的新文件通过 /assets 挂载）
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# 挂载 Vite 构建产物的 assets 目录
_assets_dir = os.path.join(_root, "app", "static", "assets")
if os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

# 挂载报告目录（用于截图等报告文件）
# 注意：生产环境下建议由 nginx / reverse proxy 直接提供静态文件服务，
# 并在反向代理层配置认证，以避免 FastAPI 中间件处理大文件的开销。
app.mount("/reports", StaticFiles(directory="reports"), name="reports")

# 设置模板
templates = Jinja2Templates(directory="app/templates")

# 包含API路由
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

# Include agent router if available
if AGENT_SUPPORT:
    app.include_router(agent_router)

app.websocket("/ws/logs/{run_id}")(websocket_logs)

_SPA_INDEX = os.path.join(_root, "app", "static", "index.html")


def _serve_spa():
    """返回 React SPA 入口 HTML。"""
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
def health_check():
    return {"status": "ok"}


# Catch-all: 所有非 API/静态文件请求返回 SPA
@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, path: str):
    if path.startswith("api/") or path.startswith("static/") or path.startswith("assets/") or path.startswith("reports/"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return _serve_spa()

def start():
    """
    由 uvicorn 运行器调用以启动服务器。
    """
    logger.info(f"在 http://{APP_HOST}:{APP_PORT} 启动服务器")
    uvicorn.run(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=True,
        reload_dirs=["app", "core", "agent"],  # 只监控源码目录
        reload_excludes=[                       # 排除数据文件
            "*.db", "*.db-wal", "*.db-shm",
            "reports/*", "logs/*",
            "*.pyc", "__pycache__/*",
            "frontend/*", "node_modules/*",
        ],
    )

if __name__ == "__main__":
    # 这允许通过 `python app/main.py` 直接运行应用程序
    start()
