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
    import app.db_models  # noqa: F401 — 确保所有 ORM model 注册到 Base.metadata
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
def client(db, ensure_admin_user):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_async_db

    def override_get_async_db():
        yield db

    app.dependency_overrides[get_async_db] = override_get_async_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_async_db, None)


@pytest.fixture
def ensure_admin_user(db):
    """确保 admin 用户存在于测试数据库（用于需要认证的测试）。"""
    import asyncio
    asyncio.run(_do_ensure_admin_user(db))
    yield


async def _do_ensure_admin_user(db):
    """确保 admin 用户存在（公共异步实现，可被 test 直接调用来重置 admin）。"""
    from app.auth import hash_password
    from sqlalchemy import select
    from app import db_models

    result = await db.execute(
        select(db_models.User).where(db_models.User.username == "admin")
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.password_hash = hash_password("Admin@2024")
        existing.must_change_password = False
        await db.commit()
        return
    admin = db_models.User(
        username="admin",
        password_hash=hash_password("Admin@2024"),
        role="admin",
        status="active",
        must_change_password=False,
    )
    db.add(admin)
    await db.commit()


@pytest.fixture
def admin_user(db, ensure_admin_user):
    """返回数据库中的 admin 用户对象。"""
    from app.auth import hash_password
    import asyncio
    from app import db_models

    async def _get():
        from sqlalchemy import select
        result = await db.execute(
            select(db_models.User).where(db_models.User.username == "admin")
        )
        return result.scalar_one_or_none()
    return asyncio.run(_get())


@pytest.fixture
def admin_cookies(client, ensure_admin_user):
    """登录管理员，返回 session cookie。"""
    # 全量测试时 rate limiter 配额可能已耗尽，每次登录前重置
    from app.rate_limiter import limiter
    if hasattr(limiter, "_storage"):
        limiter._storage.reset()
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


@pytest.fixture
def sample_testcase(client, admin_cookies, sample_project):
    """创建示例测试用例，返回 testcase dict。"""
    resp = client.post("/api/testcases/", json={
        "project_id": sample_project["id"],
        "name": "示例用例",
        "steps": [{"step_order": 1, "description": "打开页面", "expected_result": "页面加载成功"}],
    }, cookies=admin_cookies)
    assert resp.status_code == 200, resp.text
    return resp.json()
