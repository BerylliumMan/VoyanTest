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

    # 接口测试增强（031）：测试文件上限与脚本沙箱超时（宪法：Settings 为配置唯一来源）
    api_test_file_max_mb: int = 50
    api_test_file_dir: str = "data/api_test_files"  # 卷持久化目录（部署挂载 /app/data）
    api_script_timeout_ms: int = 2000
    # 编译先行（Phase A）：客户端链路在没有可用固化脚本时，先"编译一段脚本"直跑，
    # 失败自动回落 OTA。可用环境变量 COMPILE_FIRST_ENABLED=false 关闭。
    compile_first_enabled: bool = True
    # 宏动作单元（Phase B）：允许决策一次携带多个机械子动作（逐步入账、失败原地停止）。
    action_unit_enabled: bool = True
    action_unit_max: int = 6
    # 确定性自动成组：剩余连续机械步骤且目标在当前候选里时，自动组成 unit 执行（不依赖模型）
    action_unit_auto: bool = True
    # 宏动作单元失败后允许 DOM 探针回退（确定性定位，不消耗模型轮次）
    action_unit_probe: bool = True
    # 032 执行证据与可靠性增强：
    # - 运行收尾采集 console error（含未捕获异常）写入报告
    exec_console_evidence: bool = True
    exec_console_max_entries: int = 20
    # - 失败截图优先元素级（不支持时自动回退整页）
    exec_element_screenshot: bool = True
    # - 目标未找到时执行含隐藏元素诊断（区分「存在但不可见」/「不在 DOM」）
    exec_hidden_probe: bool = True
    # - browser_find 能力探测与使用（不可用时自动降级快照路径）
    exec_find_probe: bool = True
    # 033 接口测试成熟度增强：
    # - 发送前体检 Inspections（关闭=入口隐藏，/inspect 端点返回 disabled）
    api_inspections_enabled: bool = True
    # - OAuth2 令牌缓存提前过期缓冲（秒）
    api_oauth_token_buffer_s: int = 60
    # - 批量执行用例间间隔上限（毫秒）
    api_batch_delay_max_ms: int = 60000
    # - 允许 post_script cookie 读写函数（关闭=函数未注入）
    api_post_script_cookies: bool = True
    # 034 智能体对话工作台：
    # - 总开关（关闭=对话端点 403 + 前端菜单隐藏；既有链路零影响）
    chat_enabled: bool = True
    # - 上下文字符预算：超出触发历史摘要压缩（关键引用不参与压缩）
    chat_context_max_chars: int = 24000
    # - 摘要后保留最近 N 条消息原文
    chat_summary_keep_messages: int = 12
    # - 单对话并行执行上限（生成类同对话串行，不受此限）
    chat_max_parallel_runs: int = 3
    # - 证据汇总 console 条目上限
    chat_evidence_console_max: int = 20
    # - 浏览器探索生成（browse_generate）：开关 / 步数上限 / 总体超时（秒）
    #   步数上限 22：描述驱动测试 + 验证码/登录重试等重流程需要更大预算（2026-10-14 实测）
    chat_browse_enabled: bool = True
    chat_browse_max_steps: int = 22
    chat_browse_timeout_s: int = 300
    # - 探索/登录时的图形验证码自动识别（ddddocr；关闭=引擎拒绝 solve_captcha 动作）
    chat_browse_captcha_enabled: bool = True
    # - 探索视觉（每轮决策附当前页面截图；deepseek-v4.1-flash 等多模态模型可直接读图，
    #   判断布局/遮挡/验证码。关闭=纯文本快照；模型不支持图片时自动降级）
    chat_browse_vision: bool = True
    # 生成后处理的默认登录账号（留空则自动从需求正文里找「账号：xx / yy」）
    gen_autologin_username: str = ""
    gen_autologin_password: str = ""

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
    session_expire_minutes: int = 720  # 12 hours; sliding refresh on each request
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
