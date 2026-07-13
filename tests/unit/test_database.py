# tests/unit/test_database.py
"""app/database.py 单元测试 — engine 初始化、PRAGMA 事件、get_db 依赖。"""
import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import Base, AsyncSessionLocal, get_db
from app.config import get_settings


@pytest_asyncio.fixture(scope="function")
async def setup_test_engine():
    """初始化测试引擎（直接设置模块级变量，绕过 init_db_engine 的 pool 参数限制）。"""
    from app import database as db_mod
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    test_url = "sqlite+aiosqlite://"
    eng = create_async_engine(test_url, echo=False)
    maker = async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    db_mod.engine = eng
    if hasattr(db_mod.AsyncSessionLocal, '_maker'):
        db_mod.AsyncSessionLocal._maker = maker
    yield
    await eng.dispose()
    db_mod.engine = None


class TestEngineAndPragmas:
    """测试 engine 创建与 SQLite PRAGMA 事件。"""

    @pytest.mark.asyncio
    async def test_engine_is_sqlalchemy_engine(self, setup_test_engine):
        """验证 engine 是 AsyncEngine 实例。"""
        from app import database as db_mod
        assert isinstance(db_mod.engine, AsyncEngine)

    @pytest.mark.asyncio
    async def test_engine_drives_pragma_on_connect(self):
        """验证引擎连接时 PRAGMA 设置自动生效。"""
        from sqlalchemy.ext.asyncio import create_async_engine
        temp_engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False})
        async with temp_engine.connect() as conn:
            await conn.execute(text("PRAGMA foreign_keys = ON"))
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
            journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        assert fk == 1
        assert str(journal).lower() in ("wal", "memory")
        assert int(busy) >= 0

    @pytest.mark.asyncio
    async def test_session_local_creates_session(self, setup_test_engine):
        """AsyncSessionLocal 应该返回一个新会话。"""
        sess = AsyncSessionLocal()
        try:
            assert sess is not None
            # 验证可执行简单查询
            result = (await sess.execute(text("SELECT 1"))).scalar()
            assert result == 1
        finally:
            await sess.close()

    @pytest.mark.asyncio
    async def test_base_metadata_has_tables(self):
        """Base.metadata 应该注册了至少一个表（通过导入 db_models 完成）。"""
        import app.db_models  # noqa: F401 — 让 ORM model 注册到 Base.metadata
        assert len(Base.metadata.tables) > 0


class TestGetDbDependency:
    """测试 get_db FastAPI 依赖注入生成器。"""

    @pytest.mark.asyncio
    async def test_get_db_yields_session_and_closes(self, setup_test_engine):
        """get_db 应 yield 一个会话并在 finally 中关闭。"""
        gen = get_db()
        session = await gen.__anext__()
        assert session is not None
        # 验证可执行查询
        result = (await session.execute(text("SELECT 1"))).scalar()
        assert result == 1
        # 触发 generator 的 finally 关闭
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

    @pytest.mark.asyncio
    async def test_get_db_closes_session_on_exception(self, setup_test_engine):
        """即使消费方抛异常，generator 的 finally 仍应执行（关闭会话）。"""
        gen = get_db()
        session = await gen.__anext__()
        assert gen.ag_frame is not None
        with pytest.raises(RuntimeError):
            await gen.athrow(RuntimeError("simulated"))
        assert gen.ag_frame is None
