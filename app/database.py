# app/database.py — async SQLAlchemy + asyncpg
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.config import get_settings

DATABASE_URL = get_settings().database_url

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

Base = declarative_base()


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：获取异步数据库会话"""
    async with AsyncSessionLocal() as session:
        yield session


# 后向兼容别名（router 逐步迁移到 get_async_db）
get_db = get_async_db
