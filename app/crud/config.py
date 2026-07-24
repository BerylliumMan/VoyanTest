# app/crud/config.py - AI 配置与提示词模板 CRUD
#
# 提供对 AIConfig / PromptTemplate 表的纯数据库操作。
# API key 加密/解密、敏感字段脱敏、prompt 内容构造等业务逻辑由 router 负责。
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)


# ----------------------------
# AIConfig CRUD
# ----------------------------

async def get_ai_config(db: AsyncSession) -> db_models.AIConfig | None:
    """获取单行 AI 配置（约定 id=1）。不存在返回 None。"""
    result = await db.execute(
        select(db_models.AIConfig).where(db_models.AIConfig.id == 1)
    )
    return result.scalar_one_or_none()


async def upsert_ai_config(
    db: AsyncSession,
    model: str,
    api_key: Optional[str],
    api_base: str,
    temperature: float,
    max_context_tokens: int = 131072,
) -> db_models.AIConfig:
    """创建或更新单行 AI 配置（id=1 约定）。

    ``api_key`` 为 None 时不修改原值（与原路由 PUT 行为一致：未传 key 保持原值）。
    业务层面的 SQLAlchemy 错误捕获由 router 负责。
    """
    row = await get_ai_config(db)
    if row is None:
        row = db_models.AIConfig(id=1)
        db.add(row)

    row.model = model
    if api_key:
        row.api_key = api_key
    row.api_base = api_base
    row.temperature = temperature
    row.max_context_tokens = max_context_tokens

    await db.commit()
    await db.refresh(row)
    return row
