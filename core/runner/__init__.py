# core/runner/__init__.py
"""测试执行引擎 — 拆分为专注的小模块。

公共 API:
    run_test_case          – 单用例完整执行（自动创建/销毁浏览器，UI 走 OTA）
    run_batch_test_cases   – 批量用例在共享浏览器中顺序执行
    run_test_case_via_agent – 在已有 MCP 上走 AgentRunner OTA
    save_run_results       – 将执行结果持久化到数据库

    run_test_case_in_browser – 已移除；调用将 raise NotImplementedError（请用 OTA）

私有 helper 同样 re-export（tests/test_runner.py 直接验证这些函数）:
    _validate_nav_url, _resolve_env_cookies, _inject_auth_cookies

测试通过 patch("core.runner.X") mock 子模块内的依赖 — 必须把这些名字
(AsyncSessionLocal / crud / tz_now) 在包级也暴露出来。
"""

from core.runner._persistence import save_run_results
from core.runner._orchestrator import (
    run_batch_test_cases,
    run_test_case,
    run_test_case_via_agent,
)
from core.runner._validators import (
    _inject_auth_cookies,
    _resolve_env_cookies,
    _validate_nav_url,
)


async def run_test_case_in_browser(*args, **kwargs):
    """Legacy MCP 逐步执行已移除；请使用 OTA（run_test_case / run_test_case_via_agent）。"""
    raise NotImplementedError(
        "run_test_case_in_browser is removed; use OTA via "
        "run_test_case / run_test_case_via_agent"
    )


# Re-export 子模块依赖 — 让 patch("core.runner.AsyncSessionLocal", ...) 仍能命中
from app import crud  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.tz import now as tz_now  # noqa: E402

# Re-export urlparse — tests patch core.runner.urlparse
from urllib.parse import urlparse  # noqa: E402

# Re-export LLM — tests 可能直接 patch
from core.llm_wrapper import _resolve_config as _resolve_llm_config, create_openai_client  # noqa: E402

__all__ = [
    "run_test_case",
    "run_batch_test_cases",
    "run_test_case_via_agent",
    "run_test_case_in_browser",
    "save_run_results",
    # Private helpers (re-exported for unit tests)
    "_validate_nav_url",
    "_resolve_env_cookies",
    "_inject_auth_cookies",
    # Re-exported dependencies (for mock patching)
    "AsyncSessionLocal",
    "crud",
    "tz_now",
    "urlparse",
    "create_openai_client",
    "_resolve_llm_config",
]
