# app/database.py — async SQLAlchemy + asyncpg
# 引擎懒初始化，支持启动时无数据库连接（配置模式）
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import get_settings
import json
import logging
import os

logger = logging.getLogger(__name__)

Base = declarative_base()

engine = None
AsyncSessionLocal = None
SETUP_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", ".db_config.json")


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


def init_db_engine(db_url: str | None = None) -> bool:
    """初始化数据库引擎。成功返回 True。"""
    global engine, AsyncSessionLocal
    url = db_url or _resolve_database_url()
    if not url:
        engine = AsyncSessionLocal = None
        return False
    try:
        new_engine = create_async_engine(url, echo=False, pool_pre_ping=True, pool_size=5, max_overflow=10)
        new_maker = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)
        old_engine = engine
        engine = new_engine
        AsyncSessionLocal = new_maker
        if old_engine:
            import asyncio
            asyncio.ensure_future(old_engine.dispose())
        masked = url.split("://")[0] + "://***@" + url.split("@")[-1] if "@" in url else url
        logger.info("数据库引擎已初始化: %s", masked)
        return True
    except Exception as e:
        logger.error("数据库引擎初始化失败: %s", e)
        engine = AsyncSessionLocal = None
        return False


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：获取异步数据库会话"""
    if AsyncSessionLocal is None and not init_db_engine():
        raise RuntimeError("数据库未配置，请先通过 /setup 页面配置")
    async with AsyncSessionLocal() as session:
        yield session


get_db = get_async_db
