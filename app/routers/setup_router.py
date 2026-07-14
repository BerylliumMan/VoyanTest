"""首次配置路由 — 数据库初始化设置。"""
from __future__ import annotations

import json
import os
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.database import DATA_DIR, SETUP_CONFIG_FILE

router = APIRouter(prefix="/api/setup", tags=["初始化"])


class DBConfigRequest(BaseModel):
    host: str = "localhost"
    port: int = 5432
    user: str = "voyantest"
    password: str = ""
    database: str = "voyantest"


def _get_db_url(cfg: DBConfigRequest) -> str:
    return f"postgresql+asyncpg://{cfg.user}:{cfg.password}@{cfg.host}:{cfg.port}/{cfg.database}"


def _is_setup_done() -> bool:
    """检查是否已完成 PG 配置（配置文件存在 或 环境变量已设）。"""
    if os.path.exists(SETUP_CONFIG_FILE):
        try:
            with open(SETUP_CONFIG_FILE) as f:
                data = json.load(f)
            return data.get("configured", False)
        except Exception:
            return False
    # 环境变量直接设置 DATABASE_URL 也算已配置（E2E 测试场景）
    if os.getenv("DATABASE_URL"):
        return True
    return False


@router.get("/status")
async def setup_status() -> dict:
    """返回初始化状态。"""
    return {
        "configured": _is_setup_done(),
        "config_file_exists": os.path.exists(SETUP_CONFIG_FILE),
    }


@router.post("/database")
async def configure_database(cfg: DBConfigRequest) -> dict:
    """测试并保存 PG 数据库配置，然后初始化。"""
    db_url = _get_db_url(cfg)

    # 测试连接
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        test_engine = create_async_engine(db_url, pool_size=1, max_overflow=0)
        async with test_engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await test_engine.dispose()
        logger.info("PG 连接测试成功: %s@%s:%d/%s", cfg.user, cfg.host, cfg.port, cfg.database)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"数据库连接失败: {e}")

    # 保存配置（先于初始化，防止初始化过程中断导致配置丢失）
    config = {
        "configured": True,
        "database_url": db_url,
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "database": cfg.database,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETUP_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    logger.info("数据库配置已保存到 %s", SETUP_CONFIG_FILE)

    # 初始化数据库（创建表 + 种子数据）
    try:
        from app.database import Base
        from app.auth import hash_password
        from app import db_models
        from app.config import get_settings
        from app.tz import now as tz_now
        from sqlalchemy import select
        from app.gen.analyzer import get_default_prompts

        settings = get_settings()
        engine = create_async_engine(db_url, pool_size=5, max_overflow=10)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 字段迁移：确保 nickname/email 列存在（兼容重试或旧版本表）
        try:
            async with engine.begin() as conn:
                await conn.execute(__import__("sqlalchemy").text("ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(255)"))
                await conn.execute(__import__("sqlalchemy").text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)"))
        except Exception:
            logger.warning("users 表 nickname/email 列迁移失败（非关键，继续）")

        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.ext.asyncio import AsyncSession

        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with async_session() as db:
            # 创建管理员
            existing = await db.execute(
                select(db_models.User).where(db_models.User.username == settings.default_admin_username)
            )
            if not existing.scalar_one_or_none():
                admin = db_models.User(
                    username=settings.default_admin_username,
                    password_hash=hash_password("Admin@2024"),
                    role="admin", status="active", must_change_password=True,
                )
                db.add(admin)
                await db.commit()
                logger.info("默认管理员已创建")

            # 种子提示词模板
            defaults = get_default_prompts()
            for key, d in defaults.items():
                existing = await db.execute(
                    select(db_models.PromptTemplate).where(db_models.PromptTemplate.template_key == key)
                )
                if not existing.scalar_one_or_none():
                    db.add(db_models.PromptTemplate(
                        template_key=key, label=d["label"],
                        template_content=d["content"], is_custom=False,
                    ))
            await db.commit()
            logger.info("数据库初始化完成")

        await engine.dispose()

        # 通知运行中的 app 重新初始化引擎
        from app.database import init_db_engine
        await init_db_engine(db_url)
        logger.info("运行中数据库引擎已切换")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据库初始化失败: {e}")

    return {
        "configured": True,
        "message": "数据库配置成功并已初始化",
        "host": cfg.host,
        "port": cfg.port,
        "database": cfg.database,
    }
