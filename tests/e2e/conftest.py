"""E2E 测试共享 fixtures — 真实启动服务+初始化数据库（async PostgreSQL）。"""
import os
import asyncio

import pytest
from sqlalchemy import select

# E2E 使用独立的 PG 数据库
E2E_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15435/uitest_e2e"
os.environ["DATABASE_URL"] = E2E_DB_URL


@pytest.fixture(scope="session", autouse=True)
def e2e_init_db():
    """在所有 E2E 测试开始前初始化真实数据库结构。"""
    import asyncio
    from app.database import init_db_engine, Base
    from app.main import _run_startup_init

    async def _init():
        # 初始化数据库引擎
        ok = await init_db_engine()
        if not ok:
            raise RuntimeError("E2E 数据库引擎初始化失败")
        from app.database import engine
        # 1) 删除旧表 + 建新表
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # 2) 跑应用启动初始化
        await _run_startup_init()

        # 3) 重置 admin 密码 + 关闭强制改密标志
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.database import engine as eng
        async with AsyncSession(bind=eng) as session:
            from app import db_models
            from app.auth import hash_password
            result = await session.execute(
                select(db_models.User).where(db_models.User.username == "admin")
            )
            admin = result.scalar_one_or_none()
            if admin:
                admin.password_hash = hash_password("Admin@2024")
                admin.must_change_password = False
                admin.role = "admin"
                admin.status = "active"
                await session.commit()

    asyncio.run(_init())
    yield


@pytest.fixture
def api_client():
    """已认证的 API 客户端（每次测试都新登录）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin@2024"},
    )
    assert r.status_code == 200, f"admin 登录失败: {r.status_code} {r.text}"
    return client


@pytest.fixture
def e2e_db():
    """直接访问 E2E 数据库 session（同步包装，用于构造前置数据）。"""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.database import engine

    class _SyncSession:
        """Wrap async queries into sync calls for E2E convenience."""
        def __init__(self):
            self._session = None

        def __enter__(self):
            async def _get_session():
                self._session = AsyncSession(bind=engine)
                return self._session
            asyncio.run(_get_session())
            return self

        def __exit__(self, *args):
            async def _close():
                if self._session:
                    await self._session.close()
            asyncio.run(_close())

    with _SyncSession() as sync_wrapper:
        yield sync_wrapper
