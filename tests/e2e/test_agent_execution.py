"""Agent 执行功能验证 — 自启动 uvicorn + Playwright + API。

每个 test module 启动独立的 uvicorn 子进程，端口 8002，
数据库使用隔离的 e2e_agent.db 避免污染开发库。

运行：
    source venv/bin/activate
    python3 -m pytest tests/e2e/test_agent_execution.py -v --tb=short -m e2e
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests as http_requests
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.e2e

# 路径常量
PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_DB_PATH = PROJECT_ROOT / "e2e_agent.db"
SERVER_PORT = 8002  # 与 app/config.py 默认端口一致
BASE_URL = f"http://localhost:{SERVER_PORT}"
HEALTH_URL = f"{BASE_URL}/health"
TS = str(int(time.time()))


def _build_server_env() -> dict:
    """构造 uvicorn 子进程的环境变量。"""
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{AGENT_DB_PATH}"
    return env


def _wait_for_server(url: str, timeout_s: int = 30) -> None:
    """轮询健康检查端点，直到 server 就绪或超时。"""
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            resp = http_requests.get(url, timeout=2)
            if resp.status_code == 200:
                return
        except http_requests.RequestException as exc:
            last_err = exc
        time.sleep(0.5)
    raise RuntimeError(f"Server failed to become healthy at {url}: {last_err}")


def _reset_admin_must_change_password() -> None:
    """重置 admin 用户的 must_change_password 标志。

    uvicorn 启动时 _run_startup_init 会把 admin 的 must_change_password 置为 True，
    这会拦截所有非白名单 API 请求（/api/agents/* 不在白名单内）。
    所以必须清掉这个标志，否则 sess fixture 登录后调任何 API 都会拿到 403。
    """
    from sqlalchemy import create_engine, text
    db_url = f"sqlite:///{AGENT_DB_PATH}".replace("\\", "/")
    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(text("UPDATE users SET must_change_password = 0 WHERE username = 'admin'"))
        conn.commit()
    engine.dispose()


@pytest.fixture(scope="module")
def server() -> str:
    """启动真实的 FastAPI server（uvicorn）作为子进程。"""
    # 清理上次可能残留的数据库 + WAL/SHM 文件
    for suffix in ("", "-wal", "-shm"):
        p = AGENT_DB_PATH.with_name(AGENT_DB_PATH.name + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    cmd = [
        sys.executable,
        "-m", "uvicorn",
        "app.main:app",
        "--host", "127.0.0.1",
        "--port", str(SERVER_PORT),
        "--log-level", "warning",
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=_build_server_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid if os.name == "posix" else None,
    )

    try:
        _wait_for_server(HEALTH_URL, timeout_s=30)
    except Exception:
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

    # Server 起来后，重置 admin 强制改密标志
    try:
        _reset_admin_must_change_password()
    except Exception as exc:
        sys.stderr.write(f"\n[admin-reset-failed] {exc}\n")

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
def browser():
    """共享的 Playwright Chromium 浏览器实例。"""
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """每个测试独立 browser context + page，避免 cookie 状态污染。"""
    ctx = browser.new_context()
    p = ctx.new_page()
    yield p
    ctx.close()


@pytest.fixture
def logged_in_page(page, server):
    """打开首页，登录 admin（自动处理强制改密弹窗），返回已登录的 page。"""
    page.goto(f"{server}/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    page.fill("#userName_input", "admin")
    page.fill("#password_input", "Admin@2024")
    page.click("button:has-text('登录')")
    page.wait_for_timeout(2000)
    if page.locator("input[placeholder*='新密码']").count() > 0:
        page.fill("input[placeholder*='新密码']", "Admin@2024")
        page.click("button:has-text('确定')")
        page.wait_for_timeout(2000)
    return page


@pytest.fixture(scope="module")
def sess(server):
    """已认证的 HTTP session — 一次登录，本模块所有测试复用。"""
    s = http_requests.Session()
    r = s.post(
        f"{server}/api/auth/login",
        json={"username": "admin", "password": "Admin@2024"},
        timeout=10,
    )
    assert r.ok, f"登录失败: {r.status_code} {r.text}"
    yield s


class TestAgentAPI:

    def test_register(self, sess, server):
        r = sess.post(f"{server}/api/agents/register", json={
            "name": f"ea-{TS}",
            "endpoint": "http://agent1:9000",
            "description": "E2E test",
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["name"] == f"ea-{TS}"
        assert d["id"] > 0

    def test_duplicate_fails(self, sess, server):
        n = f"ea-dup-{TS}"
        sess.post(f"{server}/api/agents/register", json={"name": n, "endpoint": "x", "description": ""})
        r = sess.post(f"{server}/api/agents/register", json={"name": n, "endpoint": "x", "description": ""})
        assert r.status_code == 400

    def test_list(self, sess, server):
        r = sess.get(f"{server}/api/agents")
        assert r.ok
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_heartbeat(self, sess, server):
        r = sess.post(f"{server}/api/agents/register", json={
            "name": f"ea-hb-{TS}", "endpoint": "x", "description": "",
        })
        assert r.ok, r.text
        aid = r.json()["id"]
        r = sess.post(f"{server}/api/agents/{aid}/heartbeat")
        assert r.ok, r.text
        d = r.json()
        assert d["status"] == "online"
        assert d["last_heartbeat"] is not None

    def test_update(self, sess, server):
        r = sess.post(f"{server}/api/agents/register", json={
            "name": f"ea-upd-{TS}", "endpoint": "x", "description": "old",
        })
        aid = r.json()["id"]
        r = sess.put(f"{server}/api/agents/{aid}", json={"description": "updated"})
        assert r.ok
        assert r.json()["description"] == "updated"

    def test_logs(self, sess, server):
        r = sess.post(f"{server}/api/agents/register", json={
            "name": f"ea-log-{TS}", "endpoint": "x", "description": "",
        })
        aid = r.json()["id"]
        r = sess.get(f"{server}/api/agents/{aid}/logs", params={"page": 1, "size": 10})
        assert r.ok
        d = r.json()
        assert "items" in d
        assert d["page"] == 1

    def test_delete(self, sess, server):
        r = sess.post(f"{server}/api/agents/register", json={
            "name": f"ea-del-{TS}", "endpoint": "x", "description": "",
        })
        aid = r.json()["id"]
        assert sess.delete(f"{server}/api/agents/{aid}").ok
        assert sess.get(f"{server}/api/agents/{aid}").status_code == 404

    def test_noauth(self, server):
        assert http_requests.get(f"{server}/api/agents").status_code == 401


class TestAgentUI:

    def test_page_loads(self, logged_in_page):
        p = logged_in_page
        p.locator(".arco-menu-item:has-text('Agent 管理')").first.click()
        p.wait_for_timeout(1500)
        assert p.locator(".arco-card").count() > 0
        assert p.locator("button:has-text('注册')").count() > 0

    def test_dialog_opens(self, logged_in_page):
        p = logged_in_page
        p.locator(".arco-menu-item:has-text('Agent 管理')").first.click()
        p.wait_for_timeout(1000)
        p.locator("button:has-text('注册')").first.click()
        p.wait_for_timeout(500)
        assert p.locator(".arco-modal").count() > 0
        p.locator(".arco-modal-close-icon").first.click()
        p.wait_for_timeout(500)
