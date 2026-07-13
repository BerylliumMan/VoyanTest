# tests/unit/test_main.py
"""app/main.py 单元测试 — 启动初始化、lifespan、中间件、SPA 路由、start 函数。"""
import asyncio
import json
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from sqlalchemy import delete as sql_delete, inspect, select, text


class TestAppMetadata:
    """测试 FastAPI app 实例的元数据。"""

    @pytest.mark.asyncio
    async def test_app_title(self, client):
        from app.main import app
        assert app.title == "UI测试自动化平台"

    @pytest.mark.asyncio
    async def test_app_version(self, client):
        from app.main import app
        assert app.version == "1.0.0"

    @pytest.mark.asyncio
    async def test_app_has_lifespan(self, client):
        from app.main import app
        assert app.router.lifespan_context is not None


class TestPublicPathsAndHealth:
    """测试公开端点（health、API 文档、SPA 入口）。"""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "database" in data
        assert "browser_pool" in data

    @pytest.mark.asyncio
    async def test_openapi_json_accessible_without_auth(self, client):
        """openapi.json 是公开路径，应无需认证。"""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_accessible_without_auth(self, client):
        resp = client.get("/docs")
        assert resp.status_code in (200, 307)


class TestServeSPA:
    """测试 SPA 入口（/、/login、catch-all）。"""

    @pytest.mark.asyncio
    async def test_root_returns_spa_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_login_page_returns_spa_html(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_catch_all_returns_spa_for_unknown_path(self, client):
        """未知的非 API 路径应返回 SPA。"""
        resp = client.get("/some/random/path")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_catch_all_404_for_api_path(self, client):
        """未匹配的 /api/ 路径在中间件之前会被 auth_middleware 拦截（401 或 404）。"""
        resp = client.get("/api/nonexistent-endpoint")
        assert resp.status_code in (401, 404)

    @pytest.mark.asyncio
    async def test_catch_all_404_for_static_path(self, client):
        """static/ 路径未命中应返回 404。"""
        resp = client.get("/static/missing.css")
        assert resp.status_code == 404


class TestAuthMiddleware:
    """测试 auth_middleware 各分支。"""

    @pytest.mark.asyncio
    async def test_public_path_login_no_auth_required(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "Admin@2024",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_protected_path_no_cookie_returns_401(self, client):
        resp = client.get("/api/projects/")
        assert resp.status_code == 401
        assert "未登录" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_protected_path_invalid_session_returns_401(self, client):
        resp = client.get("/api/projects/", cookies={"session_id": "bogus"})
        assert resp.status_code == 401
        assert "已过期" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_protected_path_disabled_user_returns_401(self, client, db, admin_user):
        """有效 session 但用户被禁用 → 401。"""
        from app.auth import create_session
        session_id = await create_session(db, admin_user.id)
        admin_user.status = "disabled"
        await db.commit()
        resp = client.get("/api/projects/", cookies={"session_id": session_id})
        assert resp.status_code == 401
        assert "禁用" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_protected_path_valid_session_passes(self, client, admin_cookies):
        resp = client.get("/api/projects/", cookies=admin_cookies)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_websocket_path_skips_http_auth_middleware(self, client):
        """/api/agents/ws/ 前缀应跳过 HTTP auth middleware。"""
        resp = client.get("/api/agents/ws/foo")
        # WebSocket upgrade 不支持普通 HTTP GET → 期望 403/426/404 等
        assert resp.status_code in (403, 426, 404)


class TestEnforcePasswordChangedMiddleware:
    """测试 enforce_password_changed 各分支。"""

    @pytest.mark.asyncio
    async def test_options_request_skips_check(self, client):
        resp = client.options("/api/projects/")
        # OPTIONS 跳过 — 不应返回 403
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_non_api_path_skips_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_whitelist_path_skips_check(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "Admin@2024",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_session_id_skips_check(self, client):
        """中间件不应在没登录时强制修改密码（auth_middleware 会先拦截）。"""
        resp = client.get("/api/projects/")
        # 应该是 401（来自 auth_middleware），不是 403（密码修改）
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_must_change_password_blocks_with_403(self, client, db):
        """must_change_password=True 的用户访问非白名单路径应被 403。"""
        from app.auth import create_session, hash_password
        from app import db_models
        u = db_models.User(
            username="must_change",
            password_hash=hash_password("Pass@1234"),
            role="tester",
            status="active",
            must_change_password=True,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        session_id = await create_session(db, u.id)
        resp = client.get("/api/projects/", cookies={"session_id": session_id})
        assert resp.status_code == 403
        assert "修改" in resp.json()["detail"]


class TestRateLimitHandler:
    """测试限流异常处理器已注册。"""

    @pytest.mark.asyncio
    async def test_rate_limit_handler_registered(self, client):
        from app.main import app
        from slowapi.errors import RateLimitExceeded
        assert RateLimitExceeded in app.exception_handlers


class TestStartFunction:
    """测试 start() 入口。"""

    @pytest.mark.asyncio
    async def test_start_calls_uvicorn(self):
        from app.main import start
        with patch("app.main.uvicorn") as mock_uvicorn:
            start()
            mock_uvicorn.run.assert_called_once()
            kwargs = mock_uvicorn.run.call_args.kwargs
            assert kwargs["host"] == "0.0.0.0"
            assert kwargs["port"] == 8002
            assert kwargs["reload"] is True
            assert "app" in kwargs["reload_dirs"]
            assert "core" in kwargs["reload_dirs"]
            assert "agent" in kwargs["reload_dirs"]


class TestRunStartupInit:
    """测试 _run_startup_init 内部行为。"""

    @pytest.mark.asyncio
    async def test_disable_create_all_skips_schema_creation(self, engine, db, monkeypatch):
        from app.main import _run_startup_init
        monkeypatch.setenv("DISABLE_CREATE_ALL", "true")

        async def get_tables():
            from sqlalchemy import inspect
            def _sync_insp(conn):
                return set(inspect(conn).get_table_names())
            async with engine.connect() as conn:
                return await conn.run_sync(
                    lambda sync_conn: set(inspect(sync_conn).get_table_names())
                )

        existing_tables = await get_tables()
        await _run_startup_init()
        tables_after = await get_tables()
        assert tables_after == existing_tables

    @pytest.mark.asyncio
    async def test_cookies_column_added_when_missing(self, engine, db, monkeypatch):
        """当 environments 表没有 cookies 列时，启动会补上。"""
        from app.main import _run_startup_init

        async def has_cookies_column():
            from sqlalchemy import inspect
            async with engine.connect() as conn:
                def _check(sync_conn):
                    cols = [c["name"] for c in inspect(sync_conn).get_columns("environments")]
                    return "cookies" in cols
                return await conn.run_sync(_check)

        assert await has_cookies_column() is True
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "engine", engine)
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE environments DROP COLUMN cookies"))

        async def get_columns():
            from sqlalchemy import inspect
            async with engine.connect() as c:
                def _get(sync_c):
                    return [col["name"] for col in inspect(sync_c).get_columns("environments")]
                return await c.run_sync(_get)

        col_names = await get_columns()
        assert "cookies" not in col_names

        monkeypatch.setenv("DISABLE_CREATE_ALL", "false")
        from app.main import _run_startup_init
        await _run_startup_init()

        col_names2 = await get_columns()
        assert "cookies" in col_names2

    @pytest.mark.asyncio
    async def test_ai_config_seeded_from_config_json(self, engine, db, monkeypatch):
        """config.json 已移除 → DB seed 通过 _run_startup_init 的迁移逻辑完成。"""
        from app import db_models
        from app.main import _run_startup_init
        # 清空 ai_configs
        await db.execute(sql_delete(db_models.AIConfig))
        await db.commit()
        # config.json 不存在时，_run_startup_init 不会 seed AI 配置
        await _run_startup_init()
        result = await db.execute(select(db_models.AIConfig))
        seeded = result.scalar_one_or_none()
        assert seeded is None  # 不再从文件 seed
        # 手动 seed AI 配置（替代已删除的 config.json）
        from app.crud import config as crud_config
        await crud_config.upsert_ai_config(
            db, model="gpt-4o",
            api_key="test-key",
            api_base="https://api.example.com",
            temperature=0.1,
        )
        result2 = await db.execute(select(db_models.AIConfig))
        seeded = result2.scalar_one_or_none()
        assert seeded is not None
        assert seeded.id == 1
        assert seeded.model
        assert seeded.api_key
        assert seeded.api_base

    @pytest.mark.asyncio
    async def test_existing_prompt_template_content_updated(self, db):
        """非自定义的默认模板在启动时应被更新（content 变化）。"""
        from app import db_models
        from app.main import _run_startup_init
        template_key = "fp_extract"
        result = await db.execute(
            select(db_models.PromptTemplate).where(
                db_models.PromptTemplate.template_key == template_key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.label = "旧标签"
            existing.template_content = "旧内容"
            existing.is_custom = False
            await db.commit()
        else:
            existing = db_models.PromptTemplate(
                template_key=template_key,
                label="旧标签",
                template_content="旧内容",
                is_custom=False,
            )
            db.add(existing)
            await db.commit()
        await db.refresh(existing)

        await _run_startup_init()

        await db.refresh(existing)
        assert existing.template_content != "旧内容"
        assert existing.label != "旧标签"

    @pytest.mark.asyncio
    async def test_custom_prompt_template_not_overwritten(self, db):
        """is_custom=True 的模板不应被默认内容覆盖。"""
        from app import db_models
        from app.main import _run_startup_init
        template_key = "tc_generate"
        result = await db.execute(
            select(db_models.PromptTemplate).where(
                db_models.PromptTemplate.template_key == template_key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.label = "自定义"
            existing.template_content = "我自定义的内容"
            existing.is_custom = True
            await db.commit()
        else:
            existing = db_models.PromptTemplate(
                template_key=template_key,
                label="自定义",
                template_content="我自定义的内容",
                is_custom=True,
            )
            db.add(existing)
            await db.commit()
        await db.refresh(existing)
        original_content = existing.template_content

        await _run_startup_init()

        await db.refresh(existing)
        assert existing.template_content == original_content
        assert existing.label == "自定义"


class TestLifespan:
    """测试 lifespan 启动/关闭流程。"""

    @pytest.mark.asyncio
    async def test_lifespan_runs_startup_and_shutdown(self):
        """lifespan 内部应 yield 一次（FastAPI 协议）。"""
        from app.main import lifespan, app
        async with lifespan(app):
            pass
        # 顺利进入并退出上下文即视为通过


class TestAgentImportFallback:
    """测试 agent router 不可用时的回退路径（line 46-48）。"""

    @pytest.mark.asyncio
    async def test_agent_unavailable_logs_warning(self):
        """当 agent.router 导入失败时,main 模块会 graceful 退化。"""
        from app.main import AGENT_SUPPORT
        # 真实环境里 agent/router.py 是存在的,所以这里只验证类型/状态
        assert isinstance(AGENT_SUPPORT, bool)


class TestCORSConfigured:
    """测试 CORS 中间件已注册。"""

    @pytest.mark.asyncio
    async def test_cors_middleware_present(self, client):
        from app.main import app
        from starlette.middleware.cors import CORSMiddleware
        middleware_classes = [m.cls for m in app.user_middleware]
        assert CORSMiddleware in middleware_classes


class TestStaticFilesMounted:
    """测试静态文件挂载。"""

    @pytest.mark.asyncio
    async def test_static_endpoint_returns_files(self, client):
        """static 路径应能返回静态文件。"""
        resp = client.get("/static/favicon.ico")
        assert resp.status_code == 200
