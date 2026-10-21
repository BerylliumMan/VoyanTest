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
    # - 附件内容提取预算（docx/pdf/xlsx/md → 文本进对话上下文 / 文档驱动探索；超限截断）
    chat_attachment_max_chars: int = 6000
    # - 单对话并行执行上限（生成类同对话串行，不受此限）
    chat_max_parallel_runs: int = 3
    # - 证据汇总 console 条目上限
    chat_evidence_console_max: int = 20
    # - 浏览器探索生成（browse_generate）：开关 / 步数上限 / 总体超时（秒）
    #   步数上限 22：描述驱动测试 + 验证码/登录重试等重流程需要更大预算（2026-10-14 实测）
    chat_browse_enabled: bool = True
    chat_browse_max_steps: int = 22
    chat_browse_timeout_s: int = 300
    # - 探索/登录时的图形验证码自动识别（模型读图分析；关闭=引擎拒绝 solve_captcha 动作）
    chat_browse_captcha_enabled: bool = True
    # - 探索视觉（每轮决策附当前页面截图；deepseek-v4.1-flash 等多模态模型可直接读图，
    #   判断布局/遮挡/验证码。关闭=纯文本快照；模型不支持图片时自动降级）
    chat_browse_vision: bool = True
    # - run_code 动作（browser_run_code_unsafe）：在 Playwright 服务端进程执行任意 JS。
    #   2026-10-16 起默认开启（用户决策：任意代码默认开）；关闭后执行链拦截（探索链永不使用）
    exec_run_code_enabled: bool = True
    # - B2 运行证据：网络问题请求摘要（默认开；激进的失败排查素材）
    exec_network_evidence: bool = True
    exec_network_max_entries: int = 10
    # - B2 运行证据：视频录制（默认关；开启后服务端/新版客户端执行时自动录屏，
    #   失败时章节标注，录像落入 run 证据目录并在报告可见）
    exec_video_evidence: bool = False
    # - B4 失败诊断：点击类动作失败时做遮挡/视口只读诊断（默认开；结果注入下一轮
    #   上下文与步骤日志，帮助模型绕开遮挡，不影响动作成败）
    exec_occlusion_probe: bool = True
    # - B5 网络模拟（route/unroute/network_state）：默认关（mock/断网会改变被测系统行为）
    exec_network_mock_enabled: bool = False
    # - B5 存储操作（cookie/localstorage/sessionstorage/storage_state）：默认关
    #   （直接读写 cookies/存储可能伪造登录态）
    exec_storage_actions_enabled: bool = False
    # - B5 探索链只读探针（js_probe：links/nav/storage_keys/frameworks 固定只读 JS）
    chat_browse_js_probe: bool = True
    # 036 页面 API 调用与沙箱诊断脚本：
    # - fetch 存证单文件上限（MB；超出截断并标注实际大小）
    exec_fetch_evidence_max_mb: int = 100
    # 035 智能体工具化与 MCP 外部接入：
    # - 对话原生工具调用循环（false=降级为既有决策 JSON 协议，用户无感）
    chat_tool_loop_enabled: bool = True
    # - 单轮工具循环轮次上限
    chat_tool_max_rounds: int = 8
    # - 单轮工具循环时长上限（秒）
    chat_tool_timeout_seconds: int = 180
    # - 只读工具并行上限（mutate/execute 恒串行）
    chat_tool_parallel_max: int = 4
    # - 工具结果序列化字节上限（超出→分页切片 + 显式截断标注）
    chat_tool_result_max_bytes: int = 16384
    # - 外部接入活跃会话全局上限（MCP /mcp）
    chat_external_max_sessions: int = 4
    # 036 页面 API 调用与沙箱诊断脚本：
    # - 诊断脚本工具总开关（2026-10-16 起默认开启；关闭后工具不可见/调用被拒）
    chat_script_enabled: bool = True
    # - 执行链诊断脚本动作（run_script：用例执行中写 Python 查库/调接口/核对数据）
    exec_script_enabled: bool = True
    # 037 执行链视觉与验证码：
    # - 执行链每轮决策附当前页面截图（多模态读图；探索链同款压缩口径；模型不支持图片自动降级）
    exec_vision: bool = True
    # - 视觉帧压缩长边上限（px；控 token）
    exec_vision_max_width: int = 1152
    # - 执行链验证码动作（solve_captcha：模型读图识别并填写；关闭=动作被拒绝）
    exec_captcha_enabled: bool = True
    # - 验证码断点混合执行（含验证码用例：登录/验证码段实时→尾段固化回放；关闭=全实时 OTA）
    exec_captcha_breakpoint: bool = True
    # - 诊断脚本执行超时上限（毫秒；接口测试脚本的既有 2000ms 语义不变）
    chat_script_timeout_ms: int = 30000
    # 038 质量兜底与性能优化：
    # - 坐标点击后回读命中元素（探索链 + 执行链；关闭=基线）
    exec_click_feedback: bool = True
    # - 连续点偏提示阈值（次）
    exec_click_feedback_max_misses: int = 3
    # - 批次停止状态轮询间隔（毫秒；越小停止越快，DB 查询越频繁）
    exec_stop_poll_ms: int = 1000
    # - 生成用例批次并发上限（1=串行基线）
    gen_parallel_max: int = 3
    # 039 UI 用例参数化与数据集驱动：
    # - 单次执行展开的数据组上限（行数 × 循环次数；超过在执行前拒绝）
    ui_dataset_max_groups: int = 100
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
