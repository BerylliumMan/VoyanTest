# app/database.py — async SQLAlchemy + asyncpg
# 引擎懒初始化，支持启动时无数据库连接（配置模式）
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import get_settings
import json
import logging
import os
import asyncio

logger = logging.getLogger(__name__)

Base = declarative_base()

engine = None
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SETUP_CONFIG_FILE = os.path.join(DATA_DIR, ".db_config.json")
os.makedirs(DATA_DIR, exist_ok=True)


class _LazySessionMaker:
    """惰性 sessionmaker — 始终可调用，避免模块导入时捕获 None。

    ``from app.database import AsyncSessionLocal`` 在任何时机导入都
    拿到此实例（不是 None），调用 ``async with AsyncSessionLocal() as db:``
    在 engine 未初始化时抛出清晰的 RuntimeError。
    """

    def __init__(self):
        self._maker: async_sessionmaker | None = None

    def configure(self, maker: async_sessionmaker) -> None:
        self._maker = maker

    def __call__(self) -> async_sessionmaker:
        if self._maker is None:
            raise RuntimeError(
                "数据库未配置，请先通过 /setup 页面配置。"
                "如需在 E2E 测试中使用，请设置 DATABASE_URL 环境变量。"
            )
        return self._maker()

    @property
    def is_ready(self) -> bool:
        return self._maker is not None


AsyncSessionLocal = _LazySessionMaker()


def _resolve_database_url() -> str | None:
    """按优先级获取 DATABASE_URL：.db_config.json > 环境变量 > config.py 默认。"""
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    cfg_path = SETUP_CONFIG_FILE
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                return json.load(f).get("database_url")
        except Exception as e:
            logger.warning("读取数据库配置失败: %s", e)
    return get_settings().database_url

_engine_init_lock = asyncio.Lock()


async def init_db_engine(db_url: str | None = None) -> bool:
    """初始化数据库引擎。成功返回 True。会测试连接是否可用。"""
    async with _engine_init_lock:
        global engine
        url = db_url or _resolve_database_url()
        if not url:
            engine = None
            return False
        try:
            new_engine = create_async_engine(url, echo=False, pool_pre_ping=True, pool_size=5, max_overflow=10)
            new_maker = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)
            # 测试连接是否可用
            try:
                async with new_maker() as sess:
                    await sess.execute(__import__("sqlalchemy").text("SELECT 1"))
            except Exception as conn_err:
                logger.warning("数据库连接测试失败，进入配置模式: %s", conn_err)
                await new_engine.dispose()
                engine = None
                return False
            old_engine = engine
            engine = new_engine
            # Configure session maker (handles both _LazySessionMaker and test's async_sessionmaker)
            if hasattr(AsyncSessionLocal, '_maker'):
                AsyncSessionLocal._maker = new_maker
            else:
                AsyncSessionLocal.configure(bind=new_engine)
            if old_engine:
                await old_engine.dispose()
            masked = url.split("://")[0] + "://***@" + url.split("@")[-1] if "@" in url else url
            logger.info("数据库引擎已初始化: %s", masked)
            return True
        except Exception as e:
            logger.error("数据库引擎初始化失败: %s", e)
            engine = None
            return False


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：获取异步数据库会话"""
    if not AsyncSessionLocal.is_ready and not await init_db_engine():
        raise RuntimeError("数据库未配置，请先通过 /setup 页面配置")
    async with AsyncSessionLocal() as session:
        yield session


get_db = get_async_db
