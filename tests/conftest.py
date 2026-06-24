# tests/conftest.py
import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from fastapi.testclient import TestClient
from tempfile import mkstemp

# 使用临时文件 SQLite（NullPool + 文件数据库支持跨连接共享）
_fd, _DB_PATH = mkstemp(suffix=".test.db")
os.close(_fd)  # mkstemp opens a FD we don't need; release it immediately
TEST_DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

# setdefault 使 e2e 等子 conftest 可切到 PG
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def event_loop():
    import asyncio
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def engine():
    from app.database import Base
    eng = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    from app.database import Base
    import app.database as db_mod
    from app import db_models

    TestAsyncSessionLocal = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    original_session_local = db_mod.AsyncSessionLocal
    db_mod.AsyncSessionLocal = TestAsyncSessionLocal

    # 同步 patch core modules
    import core.runner._orchestrator as _orch
    if hasattr(_orch, "AsyncSessionLocal"):
        _orch.AsyncSessionLocal = TestAsyncSessionLocal

    session = TestAsyncSessionLocal()

    # Seed AI config
    try:
        from sqlalchemy import select
        result = await session.execute(
            select(db_models.AIConfig).where(db_models.AIConfig.id == 1)
        )
        if not result.scalar_one_or_none():
            session.add(db_models.AIConfig(
                id=1, model="gpt-4o",
                api_key="test-key", api_base="https://api.example.com",
                temperature=0.1,
            ))
            await session.commit()
    except Exception:
        await session.rollback()

    yield session

    try:
        from sqlalchemy import text
        await session.execute(text("PRAGMA foreign_keys = OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())
        await session.commit()
    except Exception:
        await session.rollback()
    finally:
        await session.close()
        db_mod.AsyncSessionLocal = original_session_local


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_async_db

    def override_get_async_db():
        yield db

    app.dependency_overrides[get_async_db] = override_get_async_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_async_db, None)


@pytest.fixture
def admin_cookies(client):
    """登录管理员，返回 session cookie。"""
    resp = client.post("/api/auth/login", json={
        "username": "admin", "password": "Admin@2024",
    })
    assert resp.status_code == 200, resp.text
    cookies = resp.cookies
    assert "session_id" in cookies
    return cookies


@pytest.fixture
def sample_project(client, admin_cookies):
    """创建示例项目，返回 project dict。"""
    resp = client.post("/api/projects/", json={
        "name": "测试项目",
        "base_url": "https://example.com",
        "browser": "chromium",
        "headless": True,
    }, cookies=admin_cookies)
    assert resp.status_code == 200, resp.text
    return resp.json()
