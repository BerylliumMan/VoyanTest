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

    await db.commit()
    await db.refresh(row)
    return row


# ----------------------------
# PromptTemplate CRUD
# ----------------------------

async def list_prompt_templates(db: AsyncSession) -> list[db_models.PromptTemplate]:
    """获取所有提示词模板行（不区分 default/custom）。"""
    result = await db.execute(select(db_models.PromptTemplate))
    return list(result.scalars().all())


async def get_prompt_template_by_key(
    db: AsyncSession,
    template_key: str,
) -> db_models.PromptTemplate | None:
    """通过 template_key 获取单条 PromptTemplate，不存在返回 None。"""
    result = await db.execute(
        select(db_models.PromptTemplate).where(
            db_models.PromptTemplate.template_key == template_key
        )
    )
    return result.scalar_one_or_none()


async def upsert_prompt_template(
    db: AsyncSession,
    template_key: str,
    label: str,
    template_content: str,
    is_custom: bool,
) -> db_models.PromptTemplate:
    """创建或更新单条 PromptTemplate。

    - 行不存在：创建新行（label 由 router 从 defaults 提供）
    - 行存在：仅覆盖 template_content 与 is_custom

    返回最新 ORM 对象。
    """
    row = await get_prompt_template_by_key(db, template_key)
    if row is None:
        row = db_models.PromptTemplate(
            template_key=template_key,
            label=label,
            template_content=template_content,
            is_custom=is_custom,
        )
        db.add(row)
    else:
        row.template_content = template_content
        row.is_custom = is_custom
    await db.commit()
    await db.refresh(row)
    return row


async def restore_prompt_template(
    db: AsyncSession,
    template_key: str,
    default_content: str,
) -> db_models.PromptTemplate | None:
    """恢复提示词模板为默认内容（仅当行存在时；行不存在返回 None）。

    复制自原 config_router.restore_prompt 行为：
    - 行存在：覆盖 template_content + is_custom=False 并 commit
    - 行不存在：不做任何写入，返回 None；由 router 决定用 defaults 直接构造响应

    返回更新后的 ORM 对象；行不存在返回 None。
    """
    row = await get_prompt_template_by_key(db, template_key)
    if row is None:
        return None
    row.template_content = default_content
    row.is_custom = False
    await db.commit()
    await db.refresh(row)
    return row
