"""API router for AI model configuration and prompt templates."""
from __future__ import annotations

import logging
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from typing import Optional
from pydantic import BaseModel, Field
import openai
from openai import AsyncOpenAI

from .. import crud
from ..auth import require_admin
from ..database import get_async_db
from ..security.encryption import encrypt_value, decrypt_value
from app.gen.analyzer import get_default_prompts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["配置"])


def _mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]


# --- Pydantic models ---

class AIConfigRequest(BaseModel):
    model: str = Field(..., min_length=1)
    api_key: Optional[str] = Field(None, min_length=1)
    api_base: str = Field(..., pattern=r"^https?://")
    temperature: float = Field(..., ge=0.0, le=2.0)

    model_config = {'extra': 'allow'}


class AIConfigResponse(BaseModel):
    model: str
    api_key_masked: str
    api_base: str
    temperature: float


# --- Routes ---

@router.get("/ai", response_model=AIConfigResponse)
async def get_ai_config(db: AsyncSession = Depends(get_async_db), user = Depends(require_admin)) -> AIConfigResponse:
    row = await crud.get_ai_config(db)
    if not row:
        return AIConfigResponse(
            model="",
            api_key_masked="",
            api_base="",
            temperature=0.0,
        )
    return AIConfigResponse(
        model=row.model,
        api_key_masked=_mask_key(row.api_key),
        api_base=row.api_base,
        temperature=row.temperature,
    )


@router.put("/ai", response_model=AIConfigResponse)
async def update_ai_config(
    body: AIConfigRequest,
    db: AsyncSession = Depends(get_async_db),
    user = Depends(require_admin),
) -> AIConfigResponse:
    # 在 router 层做加密：CRUD 层只接受已加密/可存储的值
    encrypted_key = encrypt_value(body.api_key) if body.api_key else None
    try:
        row = await crud.upsert_ai_config(db, body.model, encrypted_key, body.api_base, body.temperature)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.exception("AI 配置保存失败")
        raise HTTPException(status_code=500, detail=f"AI 配置保存失败: {exc}")

    return AIConfigResponse(
        model=row.model,
        api_key_masked=_mask_key(row.api_key),
        api_base=row.api_base,
        temperature=row.temperature,
    )


class AIConfigTestRequest(BaseModel):
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None


@router.post("/ai/test")
async def test_ai_config(
    body: AIConfigTestRequest,
    db: AsyncSession = Depends(get_async_db),
    user = Depends(require_admin),
) -> dict:
    """测试 AI 配置是否可用 — 发送一条简单请求验证连接。"""
    model = body.model
    api_key = body.api_key
    api_base = body.api_base

    if not model or not api_key or not api_base:
        row = await crud.get_ai_config(db)
        if row:
            if not model:
                model = row.model
            if not api_key:
                api_key = decrypt_value(row.api_key)
            if not api_base:
                api_base = row.api_base

    if not model:
        raise HTTPException(400, "缺少 model")
    if not api_key:
        raise HTTPException(400, "缺少 api_key")
    if not api_base:
        raise HTTPException(400, "缺少 api_base")

    try:
        client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Respond with only the word: OK"}],
                max_tokens=10,
            ),
            timeout=30,
        )
        reply = resp.choices[0].message.content.strip() if resp.choices else ""
        if "OK" in reply.upper():
            return {"success": True, "message": f"连接成功，模型回复: {reply}"}
        return {"success": True, "message": f"连接成功（回复: {reply}）"}
    except (openai.OpenAIError, asyncio.TimeoutError, OSError) as exc:
        # OpenAI SDK 抛出的所有错误（APIError / APIConnectionError / RateLimitError 等）
        # 加上网络层 OSError 与 asyncio.TimeoutError
        logger.warning("AI config test failed: %s", exc, exc_info=True)
        raise HTTPException(400, detail=f"连接失败: {exc}")


# --- Prompt Template Management ---

class PromptTemplateResponse(BaseModel):
    template_key: str
    label: str
    template_content: str
    is_custom: bool
    default_content: str
    updated_at: str | None = None


class PromptTemplateUpdate(BaseModel):
    template_content: str = Field(..., min_length=1)


@router.get("/prompts")
async def list_prompts(db: AsyncSession = Depends(get_async_db), user=Depends(require_admin)) -> list[PromptTemplateResponse]:
    """列出所有提示词模板（含默认内容）。"""
    rows = await crud.list_prompt_templates(db)
    defaults = get_default_prompts()
    result = []
    for row in rows:
        default = defaults.get(row.template_key, {}).get("content", "")
        result.append(PromptTemplateResponse(
            template_key=row.template_key,
            label=row.label,
            template_content=row.template_content,
            is_custom=row.is_custom,
            default_content=default,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        ))
    db_keys = {r.template_key for r in rows}
    for key, d in defaults.items():
        if key not in db_keys:
            result.append(PromptTemplateResponse(
                template_key=key,
                label=d["label"],
                template_content=d["content"],
                is_custom=False,
                default_content=d["content"],
            ))
    return result


@router.get("/prompts/{key}")
async def get_prompt(key: str, db: AsyncSession = Depends(get_async_db), user=Depends(require_admin)) -> PromptTemplateResponse:
    """获取单个提示词模板。"""
    defaults = get_default_prompts()
    if key not in defaults:
        raise HTTPException(404, f"未知的提示词模板: {key}")
    default = defaults[key]["content"]
    row = await crud.get_prompt_template_by_key(db, key)
    if row:
        return PromptTemplateResponse(
            template_key=row.template_key,
            label=row.label,
            template_content=row.template_content,
            is_custom=row.is_custom,
            default_content=default,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
    return PromptTemplateResponse(
        template_key=key,
        label=defaults[key]["label"],
        template_content=default,
        is_custom=False,
        default_content=default,
    )


@router.put("/prompts/{key}")
async def update_prompt(
    key: str,
    body: PromptTemplateUpdate,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(require_admin),
) -> PromptTemplateResponse:
    """保存（覆盖）提示词模板内容。"""
    defaults = get_default_prompts()
    if key not in defaults:
        raise HTTPException(404, f"未知的提示词模板: {key}")
    row = await crud.upsert_prompt_template(
        db, key, defaults[key]["label"], body.template_content, True
    )
    return PromptTemplateResponse(
        template_key=row.template_key,
        label=row.label,
        template_content=row.template_content,
        is_custom=row.is_custom,
        default_content=defaults[key]["content"],
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.post("/prompts/{key}/restore")
async def restore_prompt(
    key: str,
    db: AsyncSession = Depends(get_async_db),
    user=Depends(require_admin),
) -> PromptTemplateResponse:
    """恢复提示词模板为默认内容。"""
    defaults = get_default_prompts()
    if key not in defaults:
        raise HTTPException(404, f"未知的提示词模板: {key}")
    row = await crud.restore_prompt_template(db, key, defaults[key]["content"])
    if row:
        return PromptTemplateResponse(
            template_key=row.template_key,
            label=row.label,
            template_content=row.template_content,
            is_custom=row.is_custom,
            default_content=defaults[key]["content"],
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
    return PromptTemplateResponse(
        template_key=key,
        label=defaults[key]["label"],
        template_content=defaults[key]["content"],
        is_custom=False,
        default_content=defaults[key]["content"],
    )


class HealingConfig(BaseModel):
    enabled: bool = True
    max_retries: int = 3
    threshold: float = 0.8


_healing_config = HealingConfig()


@router.get("/healing", response_model=HealingConfig)
async def get_healing_config(admin=Depends(require_admin)) -> HealingConfig:
    """获取自愈选择器配置。"""
    return _healing_config


@router.put("/healing", response_model=HealingConfig)
async def update_healing_config(cfg: HealingConfig, admin=Depends(require_admin)) -> HealingConfig:
    """更新自愈选择器配置。"""
    global _healing_config
    _healing_config = cfg
    return _healing_config
