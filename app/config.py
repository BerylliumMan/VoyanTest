from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:15435/uitest"

    # Application
    app_host: str = "0.0.0.0"
    app_port: int = 8002

    # Playwright defaults
    browser_type: str = "chromium"
    headless: bool = True

    # Playwright MCP
    playwright_browser_type: str = "chromium"
    playwright_headless: bool = True
    playwright_step_timeout_ms: int = 120000
    playwright_max_consecutive_failures: int = 3

    # Agent
    agent_heartbeat_interval: int = 30

    # Auth
    cookie_secure: bool = False
    session_secret_key: str = ""
    session_expire_minutes: int = 30
    max_login_attempts: int = 5
    lock_duration_minutes: int = 15
    default_admin_username: str = "admin"
    default_admin_password: str = ""  # 启动时创建管理员时硬编码在 main.py 中

    # CORS
    cors_allow_origins: str = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"
    cors_allow_credentials: bool = True
    cors_allow_methods: str = "GET,POST,PUT,DELETE,PATCH,OPTIONS"
    cors_allow_headers: str = "Content-Type,Authorization,X-Requested-With,Accept"

    # CSRF 保护 — 当前依赖 SameSite=Lax Cookie 的浏览器自动保护机制。
    # 未实现 CSRF token 中间件。如需额外保护，请实现 Origin/Referer 校验。
    csrf_enabled: bool = False
    csrf_exclude_paths: str = "/api/auth/login,/api/auth/login-form,/api/auth/logout"

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "text" 或 "json"

    # 使用系统环境变量配置。不依赖 .env 文件。
    model_config = {"env_prefix": ""}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
