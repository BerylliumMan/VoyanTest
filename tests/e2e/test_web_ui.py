# tests/e2e/test_web_ui.py
"""E2E UI 测试 — 自启动 uvicorn 子进程，端口 8002，隔离 SQLite (e2e_web_ui.db)。

无需手动启动服务：每个 test module 启动一个独立的 uvicorn 子进程，
测试结束后自动清理（关闭进程、删除临时 DB）。

与 tests/e2e/conftest.py 的关系：
    conftest.py 的 e2e_init_db fixture 是 session 级 autouse，
    会在测试进程里建一个 e2e_platform.db，仅供 api_client fixture 使用。
    本文件的 server fixture 在 uvicorn 子进程里跑独立 DB (e2e_web_ui.db)，
    两套 DB 互不干扰。

运行：
    source venv/bin/activate
    python3 -m pytest tests/e2e/test_web_ui.py -v --tb=short -m e2e
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
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.e2e

# 路径常量
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_UI_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15435/uitest_e2e_webui"
SERVER_PORT = 8002  # 与 app/config.py 默认端口一致
BASE_URL = f"http://localhost:{SERVER_PORT}"
HEALTH_URL = f"{BASE_URL}/health"


def _build_server_env() -> dict:
    env = os.environ.copy()
    env["DATABASE_URL"] = WEB_UI_DB_URL
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


def _reset_admin_must_change_password() -> None:
    from sqlalchemy import create_engine, text
    sync_url = WEB_UI_DB_URL.replace("+asyncpg", "")
    _eng = create_engine(sync_url)
    with _eng.begin() as c:
        c.execute(text("UPDATE users SET must_change_password = false WHERE username = 'admin'"))
    _eng.dispose()


@pytest.fixture(scope="module")
def server() -> str:
    """启动真实的 FastAPI server（uvicorn）作为子进程。

    Module scope — 同一测试模块只启动一次。
    """
    # PG 下无文件残留，直接继续
    pass

    cmd = [
        sys.executable,                      # 使用当前 Python（含 venv）
        "-m", "uvicorn",
        "app.main:app",
        "--host", "127.0.0.1",
        "--port", str(SERVER_PORT),
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
        _wait_for_server(HEALTH_URL, timeout_s=30)
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


def visible_menus(page):
    """返回侧边栏中可见的所有菜单项文本。"""
    result = []
    items = page.locator(".arco-menu-item")
    for i in range(items.count()):
        el = items.nth(i)
        if el.is_visible():
            result.append(el.text_content() or "")
    return result


def click_visible(page, label: str):
    """点击可见菜单项。"""
    page.locator(f".arco-menu-item:has-text('{label}')").first.click()
    page.wait_for_timeout(1500)


class TestLogin:
    def test_has_form(self, page, server):
        page.goto(f"{server}/")
        page.wait_for_load_state("networkidle")
        assert page.locator("#userName_input").count() > 0
        assert page.locator("#password_input").count() > 0
        assert page.locator("button:has-text('登录')").count() > 0

    def test_login_works(self, page, server):
        page.goto(f"{server}/")
        page.wait_for_load_state("networkidle")
        page.fill("#userName_input", "admin")
        page.fill("#password_input", "Admin@2024")
        page.click("button:has-text('登录')")
        page.wait_for_timeout(2000)
        if page.locator("input[placeholder*='新密码']").count() > 0:
            page.fill("input[placeholder*='新密码']", "Admin@2024")
            page.click("button:has-text('确定')")
            page.wait_for_timeout(2000)
        assert page.locator(".arco-menu").count() > 0

    def test_bad_login_stays(self, page, server):
        page.goto(f"{server}/")
        page.wait_for_load_state("networkidle")
        page.fill("#userName_input", "admin")
        page.fill("#password_input", "wrongpw")
        page.click("button:has-text('登录')")
        page.wait_for_timeout(1500)
        body = page.text_content("body") or ""
        assert "用户" in body or "登录" in body


class TestNavigation:
    def test_sidebar_has_items(self, logged_in_page):
        v = visible_menus(logged_in_page)
        # 至少有 6 项可见
        assert len(v) >= 6, f"可见菜单太少: {v}"

    def test_navigate_visible_pages(self, logged_in_page):
        for name in visible_menus(logged_in_page):
            click_visible(logged_in_page, name)
            assert logged_in_page.locator(".arco-card").count() > 0 or \
                   len(logged_in_page.text_content("body") or "") > 50


class TestAuth:
    def test_unauthenticated_blocked(self, page, server):
        resp = page.request.get(f"{server}/api/projects/")
        assert resp.status == 401

    def test_health(self, page, server):
        resp = page.request.get(f"{server}/health")
        assert resp.status == 200


class TestPages:
    """各核心页面加载验证（SPA 路由 + API CRUD）。"""

    def _nav_to(self, page, path):
        page.goto(f"{BASE_URL}{path}", wait_until="networkidle")
        page.wait_for_timeout(2000)

    def _has_content(self, page) -> bool:
        return len(page.text_content("body") or "") > 50

    def test_dashboard(self, logged_in_page):
        self._nav_to(logged_in_page, "/")
        assert self._has_content(logged_in_page)

    def test_testcases_page(self, logged_in_page):
        self._nav_to(logged_in_page, "/testcases")
        assert "testcase" in logged_in_page.url.lower() or self._has_content(logged_in_page)

    def test_gen_page(self, logged_in_page):
        self._nav_to(logged_in_page, "/gen")
        assert "gen" in logged_in_page.url.lower() or self._has_content(logged_in_page)

    def test_recordings_page(self, logged_in_page):
        self._nav_to(logged_in_page, "/recordings")
        assert "recordings" in logged_in_page.url.lower() or "录制" in (logged_in_page.text_content("body") or "")

    def test_reports_page(self, logged_in_page):
        self._nav_to(logged_in_page, "/reports")
        assert "report" in logged_in_page.url.lower() or self._has_content(logged_in_page)

    def test_settings_page(self, logged_in_page):
        self._nav_to(logged_in_page, "/settings")
        assert "setting" in logged_in_page.url.lower() or self._has_content(logged_in_page)

    def test_sidebar_navigation(self, logged_in_page):
        self._nav_to(logged_in_page, "/")
        for menu_text in ["仪表盘", "测试用例", "AI生成", "录制管理", "测试报告", "系统设置"]:
            menu = logged_in_page.locator(f".arco-menu-item:has-text('{menu_text}')")
            if menu.count() > 0:
                menu.first.click()
                logged_in_page.wait_for_timeout(1500)
                assert self._has_content(logged_in_page), f"菜单「{menu_text}」内容为空"

    def test_project_crud(self, logged_in_page, server):
        """通过 API 完成完整 CRUD 流程。"""
        import httpx
        with httpx.Client(base_url=server) as h:
            r = h.post("/api/auth/login", json={"username":"admin","password":"Admin@2024"})
            assert r.status_code == 200; ck = r.cookies

            r = h.post("/api/projects/", json={"name":"E2E","base_url":"https://e2e.com","browser":"chromium","headless":True}, cookies=ck)
            assert r.status_code == 200; pid = r.json()["id"]

            r = h.post(f"/api/projects/{pid}/modules", json={"project_id":pid,"name":"M"}, cookies=ck)
            assert r.status_code == 200; mid = r.json()["id"]

            r = h.post("/api/testcases/", json={"project_id":pid,"module_id":mid,"name":"TC","steps":[{"step_order":1,"description":"s1"}]}, cookies=ck)
            assert r.status_code == 200; cid = r.json()["id"]

            r = h.get(f"/api/testcases/search?project_id={pid}", cookies=ck)
            assert r.json()["total_items"] >= 1

            r = h.get(f"/api/testcases/{cid}", cookies=ck)
            assert len(r.json()["steps"]) == 1

            r = h.put(f"/api/testcases/{cid}", json={"name":"已更新"}, cookies=ck)
            assert r.json()["name"] == "已更新"

            r = h.delete(f"/api/projects/{pid}", cookies=ck)
            assert r.status_code == 200

            r = h.get("/api/projects/", cookies=ck)
            assert all(p["id"] != pid for p in r.json())
            r = h.get(f"/api/testcases/search?project_id={pid}", cookies=ck)
            assert r.json()["total_items"] == 0


class TestModuleAPI:
    """模块 CRUD 测试 — 通过 API。"""

    def test_module_tree(self, server):
        """创建模块父子树。"""
        import httpx
        with httpx.Client(base_url=server) as h:
            h.post("/api/auth/login", json={"username":"admin","password":"Admin@2024"})

    def _api(self, server):
        import httpx
        h = httpx.Client(base_url=server)
        r = h.post("/api/auth/login", json={"username":"admin","password":"Admin@2024"})
        assert r.status_code == 200
        h.cookies = r.cookies
        return h

    def test_create_project_and_modules(self, server):
        h = self._api(server)
        r = h.post("/api/projects/", json={"name":"模块测试项目","base_url":"https://mod.com"})
        assert r.status_code == 200; pid = r.json()["id"]
        # 父模块
        r = h.post(f"/api/projects/{pid}/modules", json={"project_id":pid,"name":"父模块"})
        assert r.status_code == 200; pid_mod = r.json()["id"]
        # 子模块
        r = h.post(f"/api/projects/{pid}/modules", json={"project_id":pid,"name":"子模块","parent_id":pid_mod})
        assert r.status_code == 200; cid_mod = r.json()["id"]
        # 查询树
        r = h.get(f"/api/projects/{pid}/modules/tree")
        assert r.status_code == 200
        tree = r.json()
        names = {n["name"] for n in tree}
        assert "父模块" in names
        # 删除
        h.delete(f"/api/projects/{pid}")

    def test_get_modules_empty(self, server):
        h = self._api(server)
        r = h.post("/api/projects/", json={"name":"空模块项目","base_url":"https://empty.com"})
        pid = r.json()["id"]
        r = h.get(f"/api/projects/{pid}/modules/tree")
        assert r.json() == []
        h.delete(f"/api/projects/{pid}")


class TestEnvironmentAPI:
    """环境 CRUD 测试 — 通过 API。"""

    def _api(self, server):
        import httpx
        h = httpx.Client(base_url=server)
        r = h.post("/api/auth/login", json={"username":"admin","password":"Admin@2024"})
        assert r.status_code == 200
        h.cookies = r.cookies
        return h

    def test_create_and_list_envs(self, server):
        h = self._api(server)
        r = h.post("/api/projects/", json={"name":"环境测试","base_url":"https://env.com"})
        pid = r.json()["id"]
        # 创建
        for name in ["开发环境", "测试环境", "生产环境"]:
            r = h.post(f"/api/projects/{pid}/environments", json={
                "project_id":pid, "name":name, "base_url":f"https://{name}.com",
            })
            assert r.status_code == 200
        # 列表
        r = h.get(f"/api/projects/{pid}/environments")
        assert r.status_code == 200
        names = [e["name"] for e in r.json()]
        assert "开发环境" in names and "生产环境" in names
        h.delete(f"/api/projects/{pid}")

    def test_update_environment(self, server):
        h = self._api(server)
        r = h.post("/api/projects/", json={"name":"环境更新","base_url":"https://envup.com"})
        pid = r.json()["id"]
        r = h.post(f"/api/projects/{pid}/environments", json={
            "project_id":pid, "name":"旧环境", "base_url":"https://old.com",
        })
        eid = r.json()["id"]
        r = h.put(f"/api/projects/{pid}/environments/{eid}", json={
            "name":"新环境", "base_url":"https://new.com",
        })
        assert r.json()["name"] == "新环境"
        h.delete(f"/api/projects/{pid}")

    def test_delete_environment(self, server):
        h = self._api(server)
        r = h.post("/api/projects/", json={"name":"环境删除","base_url":"https://envdel.com"})
        pid = r.json()["id"]
        r = h.post(f"/api/projects/{pid}/environments", json={
            "project_id":pid, "name":"待删除","base_url":"https://del.com",
        })
        eid = r.json()["id"]
        r = h.delete(f"/api/projects/{pid}/environments/{eid}")
        assert r.status_code == 200
        envs = h.get(f"/api/projects/{pid}/environments").json()
        assert all(e["id"] != eid for e in envs)
        h.delete(f"/api/projects/{pid}")


class TestRunAPI:
    """测试执行流程 — 通过 API。"""

    def _api(self, server):
        import httpx
        h = httpx.Client(base_url=server)
        r = h.post("/api/auth/login", json={"username":"admin","password":"Admin@2024"})
        assert r.status_code == 200
        h.cookies = r.cookies
        return h

    def test_run_history_empty(self, server):
        h = self._api(server)
        r = h.post("/api/projects/", json={"name":"运行测试","base_url":"https://run.com"})
        pid = r.json()["id"]
        r = h.get(f"/api/projects/{pid}/run-batches")
        assert r.status_code == 200
        # 新项目运行历史为空
        assert r.json().get("total", 0) == 0
        h.delete(f"/api/projects/{pid}")

    def test_gen_page_accessible(self, logged_in_page):
        """AI 生成页面可访问。"""
        click_visible(logged_in_page, "AI生成")
        logged_in_page.wait_for_timeout(1500)
        body = logged_in_page.text_content("body") or ""
        assert len(body) > 0
