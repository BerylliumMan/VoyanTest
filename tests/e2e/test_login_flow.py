"""E2E test: login flow with a real Playwright browser.

Self-contained — starts its own uvicorn server on port 8002 backed by an
isolated SQLite database (``login_flow_e2e.db``), then drives a real
Chromium browser through the login page and the admin login flow.

Run:
    source venv/bin/activate
    python3 -m pytest tests/e2e/test_login_flow.py -v --tb=long -m e2e

The test proves the minimum-viable end-to-end story: the web interface
loads, the login page renders the expected inputs, an admin can sign in,
and the dashboard (sidebar) becomes reachable after redirect.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Browser, Page, sync_playwright


# 端口和路径常量 — 与 app/config.py 中默认配置保持一致
BASE_URL = "http://localhost:8002"
HEALTH_URL = f"{BASE_URL}/health"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGIN_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15435/uitest_e2e_login"
SCREENSHOT_DIR = PROJECT_ROOT / "reports" / "e2e_screenshots"

# Arco Design 登录表单的稳定选择器 — 通过 form field ID 定位，避免依赖文本
USERNAME_INPUT = "#userName_input"
PASSWORD_INPUT = "#password_input"
LOGIN_BUTTON = "button:has-text('登录')"
SIDEBAR_MENU = ".arco-menu"


def _build_server_env() -> dict:
    env = os.environ.copy()
    env["DATABASE_URL"] = LOGIN_DB_URL
    return env


def _wait_for_server(url: str, timeout_s: int = 30) -> None:
    """轮询健康检查端点，直到 server 就绪或超时。"""
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                return
        except requests.RequestException as exc:
            last_err = exc
        time.sleep(0.5)
    raise RuntimeError(f"Server failed to become healthy at {url}: {last_err}")


@pytest.fixture(scope="module")
def server() -> str:
    """启动真实的 FastAPI server（uvicorn）作为子进程。

    使用 module scope 确保同一测试模块只启动一次。
    端口固定 8002 — 与 app/config.py 默认一致。
    """
    # PG 下无文件残留，直接继续
    pass

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,                      # 使用当前 Python（含 venv）
        "-m", "uvicorn",
        "app.main:app",
        "--host", "127.0.0.1",
        "--port", "8002",
        "--log-level", "warning",            # 减少测试输出噪音
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=_build_server_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # 新进程组 — 便于清理时一并杀掉 uvicorn 派生的 worker
        preexec_fn=os.setsid if os.name == "posix" else None,
    )

    try:
        _wait_for_server(HEALTH_URL, timeout_s=60)
    except Exception:
        # 启动失败时 dump 子进程输出辅助排错
        try:
            proc.terminate()
            out, _ = proc.communicate(timeout=5)
            sys.stderr.write(
                f"\n[server-startup-failed] uvicorn output:\n"
                f"{(out or b'').decode('utf-8', errors='replace')}\n"
            )
        except Exception:
            pass
        raise

    yield BASE_URL

    # Teardown — 优雅关闭整个进程组
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass


@pytest.fixture(scope="module")
def browser() -> Browser:
    """共享的 Playwright Chromium 浏览器实例。"""
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser) -> Page:
    """每个测试独立 browser context + page，避免 cookie 状态污染。"""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    yield page
    context.close()


# ──────────────────────── 测试用例 ────────────────────────


@pytest.mark.e2e
def test_login_page_loads_and_renders_form(server: str, page: Page) -> None:
    """最小可用断言：登录页能加载、用户名/密码输入框可见、登录按钮可见。

    证明前端部署正确 — 这是 E2E 测试的核心价值主张。
    """
    page.goto(f"{server}/login", wait_until="networkidle")

    # 等待 React + Arco Design 异步挂载完成
    page.wait_for_selector(USERNAME_INPUT, timeout=15000)
    page.wait_for_selector(PASSWORD_INPUT, timeout=15000)
    page.wait_for_selector(LOGIN_BUTTON, timeout=15000)

    assert page.locator(USERNAME_INPUT).count() == 1
    assert page.locator(PASSWORD_INPUT).count() == 1
    assert page.locator(LOGIN_BUTTON).count() >= 1


@pytest.mark.e2e
def test_login_flow_redirects_to_dashboard(server: str, page: Page) -> None:
    """完整登录流程：填写凭证 → 提交 → 跳转后出现侧边栏菜单。

    默认管理员账号 admin / Admin@2024 由 app/main.py 的 _run_startup_init 自动创建，
    但 must_change_password=True 会拦截后续请求 — 这里通过 API 直接重置该标志。
    """
    # 重置 admin 强制改密标志 — 用同步方式连接 PG
    from sqlalchemy import create_engine, text
    sync_url = "postgresql+asyncpg://postgres:postgres@localhost:15435/uitest_e2e_login".replace("+asyncpg", "")
    _eng = create_engine(sync_url)
    with _eng.begin() as c:
        c.execute(text("UPDATE users SET must_change_password = false WHERE username = 'admin'"))
    _eng.dispose()

    page.goto(f"{server}/login", wait_until="networkidle")
    page.wait_for_selector(USERNAME_INPUT, timeout=15000)

    page.fill(USERNAME_INPUT, "admin")
    page.fill(PASSWORD_INPUT, "Admin@2024")
    page.click(LOGIN_BUTTON)

    # 登录成功后 SPA 跳转：等待侧边栏出现或 URL 离开 /login
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
    except Exception:
        # 部分构建下 SPA 不会改变 pathname，依赖侧边栏判断
        pass

    # 侧边栏出现 = 已登录且 dashboard 渲染成功
    page.wait_for_selector(SIDEBAR_MENU, timeout=15000)
    assert page.locator(SIDEBAR_MENU).count() >= 1

    # 截图存档 — 验证前端资源 + 布局都正确加载
    screenshot_path = SCREENSHOT_DIR / "login_flow_success.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    assert screenshot_path.exists() and screenshot_path.stat().st_size > 0