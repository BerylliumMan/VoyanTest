"""API router for AI model configuration and prompt templates."""
from __future__ import annotations

import logging
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from typing import Optional
from pydantic import BaseModel, Field, model_validator
import openai
from openai import AsyncOpenAI

from .. import crud
from ..auth import require_admin
from ..database import get_async_db
from ..security.encryption import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["配置"])


def _mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]


# --- Pydantic models ---

_DEFAULT_MAX_CONTEXT_TOKENS = 131072


class AIConfigRequest(BaseModel):
    model: str = Field(..., min_length=1)
    api_key: Optional[str] = Field(None, min_length=1)
    api_base: str = Field(..., pattern=r"^https?://")
    temperature: float = Field(..., ge=0.0, le=2.0)
    max_context_tokens: Optional[int] = Field(
        _DEFAULT_MAX_CONTEXT_TOKENS, ge=4096, le=1048576
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data):
        if not isinstance(data, dict):
            return data
        cleaned = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
            cleaned[k] = v
        # 未填 / 空字符串 / null → 使用默认上下文窗口，避免校验失败或入库 NULL
        mct = cleaned.get("max_context_tokens", _DEFAULT_MAX_CONTEXT_TOKENS)
        if mct is None or mct == "":
            cleaned["max_context_tokens"] = _DEFAULT_MAX_CONTEXT_TOKENS
        return cleaned

    model_config = {"extra": "allow"}


class AIConfigResponse(BaseModel):
    model: str
    api_key_masked: str
    api_base: str
    temperature: float
    max_context_tokens: int = _DEFAULT_MAX_CONTEXT_TOKENS


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
            max_context_tokens=_DEFAULT_MAX_CONTEXT_TOKENS,
        )
    return AIConfigResponse(
        model=row.model,
        api_key_masked=_mask_key(row.api_key),
        api_base=row.api_base,
        temperature=row.temperature,
        max_context_tokens=row.max_context_tokens or _DEFAULT_MAX_CONTEXT_TOKENS,
    )


@router.put("/ai", response_model=AIConfigResponse)
async def update_ai_config(
    body: AIConfigRequest,
    db: AsyncSession = Depends(get_async_db),
    user = Depends(require_admin),
) -> AIConfigResponse:
    # 在 router 层做加密：CRUD 层只接受已加密/可存储的值
    encrypted_key = encrypt_value(body.api_key) if body.api_key else None
    max_tokens = body.max_context_tokens or _DEFAULT_MAX_CONTEXT_TOKENS
    try:
        row = await crud.upsert_ai_config(
            db, body.model, encrypted_key, body.api_base,
            body.temperature, max_tokens,
        )
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.exception("AI 配置保存失败")
        raise HTTPException(status_code=500, detail=f"AI 配置保存失败: {exc}")

    # 清除 AI 配置缓存，确保下次调用加载新配置
    from app.gen.model_client import invalidate_ai_config_cache
    invalidate_ai_config_cache()

    return AIConfigResponse(
        model=row.model,
        api_key_masked=_mask_key(row.api_key),
        api_base=row.api_base,
        temperature=row.temperature,
        max_context_tokens=row.max_context_tokens or _DEFAULT_MAX_CONTEXT_TOKENS,
    )


class AIConfigTestRequest(BaseModel):
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def _trim_strings(cls, data):
        if isinstance(data, dict):
            return {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
        return data


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
        reply_raw = resp.choices[0].message.content
        reply = reply_raw.strip() if reply_raw else ""
        if "OK" in reply.upper():
            return {"success": True, "message": f"连接成功，模型回复: {reply}"}
        return {"success": True, "message": f"连接成功（回复: {reply}）"}
    except (openai.OpenAIError, asyncio.TimeoutError, OSError) as exc:
        # OpenAI SDK 抛出的所有错误（APIError / APIConnectionError / RateLimitError 等）
        # 加上网络层 OSError 与 asyncio.TimeoutError
        logger.warning("AI config test failed: %s", exc, exc_info=True)
        err_msg = str(exc)
        # 过滤掉 LLM API 返回的 HTML 页面，只保留可读信息
        if "<!DOCTYPE" in err_msg or "<html" in err_msg or err_msg.count(">") > 5:
            err_msg = "API 返回了非 JSON 响应（请检查 API Base URL 和 API Key 是否正确）"
        raise HTTPException(400, detail=f"连接失败: {err_msg}")


# --- Prompt Template Management (Versioned) ---

from typing import Any

class PromptTemplateResponse(BaseModel):
    id: int
    key: str
    name: str
    category: str
    content: str
    variables: list[str] = []
    version: int
    is_active: bool
    description: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PromptCreateRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z_]+$')
    name: str = Field(..., min_length=1, max_length=200)
    category: str = Field(..., pattern=r'^(generation|execution|verification)$')
    content: str = Field(..., min_length=10, max_length=10000)
    variables: list[str] = []
    description: str | None = None


class PromptActivateRequest(BaseModel):
    version: int = Field(..., ge=1)


class PromptPreviewResponse(BaseModel):
    rendered: str
    variables: dict[str, str]
    version: int
    token_estimate: int


@router.get("/prompts")
async def list_prompts(
    db: AsyncSession = Depends(get_async_db),
    admin = Depends(require_admin),
) -> list[PromptTemplateResponse]:
    """列出所有提示词模板的最新版本。"""
    rows = await crud.list_prompt_templates(db)
    return [_pt_to_response(r) for r in rows]


@router.get("/prompts/{key}")
async def get_prompt(
    key: str,
    db: AsyncSession = Depends(get_async_db),
    admin = Depends(require_admin),
) -> PromptTemplateResponse:
    """获取指定 key 的活跃版本。"""
    row = await crud.get_prompt_template_by_key(db, key)
    if not row:
        raise HTTPException(404, f"提示词模板不存在: {key}")
    return _pt_to_response(row)


@router.get("/prompts/{key}/versions")
async def get_prompt_versions(
    key: str,
    db: AsyncSession = Depends(get_async_db),
    admin = Depends(require_admin),
) -> list[PromptTemplateResponse]:
    """获取指定 key 的所有版本历史。"""
    rows = await crud.get_prompt_versions(db, key)
    if not rows:
        raise HTTPException(404, f"提示词模板不存在: {key}")
    return [_pt_to_response(r) for r in rows]


@router.post("/prompts", status_code=201)
async def create_prompt(
    body: PromptCreateRequest,
    db: AsyncSession = Depends(get_async_db),
    admin = Depends(require_admin),
) -> PromptTemplateResponse:
    """创建提示词模板的新版本（版本号自动递增）。"""
    try:
        row = await crud.create_prompt_template(
            db,
            key=body.key,
            name=body.name,
            category=body.category,
            content=body.content,
            variables=body.variables,
            description=body.description,
        )
    except Exception as exc:
        logger.exception("创建提示词模板失败")
        raise HTTPException(400, detail=str(exc))
    return _pt_to_response(row)


@router.put("/prompts/{key}/activate")
async def activate_prompt(
    key: str,
    body: PromptActivateRequest,
    db: AsyncSession = Depends(get_async_db),
    admin = Depends(require_admin),
) -> dict:
    """切换指定 key 的活跃版本。"""
    row = await crud.activate_prompt_version(db, key, body.version)
    if not row:
        raise HTTPException(404, f"版本 {body.version} 不存在")
    return {"message": f"{key} 已切换到 version {body.version}"}


@router.get("/prompts/{key}/preview")
async def preview_prompt(
    key: str,
    version: int | None = None,
    sample: str = "",
    db: AsyncSession = Depends(get_async_db),
    admin = Depends(require_admin),
) -> PromptPreviewResponse:
    """预览提示词模板（渲染变量）。"""
    row = await crud.get_prompt_template_by_key(db, key, version=version or None)
    if not row:
        raise HTTPException(404, f"提示词模板不存在: {key}")
    rendered = row.content.replace("{text}", sample) if sample else row.content
    return PromptPreviewResponse(
        rendered=rendered,
        variables={"text": sample} if sample else {},
        version=row.version,
        token_estimate=len(rendered) * 2,
    )


def _pt_to_response(pt: Any) -> PromptTemplateResponse:
    from datetime import timezone
    return PromptTemplateResponse(
        id=pt.id,
        key=pt.key,
        name=pt.name,
        category=pt.category,
        content=pt.content,
        variables=pt.variables or [],
        version=pt.version,
        is_active=pt.is_active,
        description=pt.description,
        created_at=pt.created_at.astimezone(timezone.utc).isoformat() if pt.created_at else None,
        updated_at=pt.updated_at.astimezone(timezone.utc).isoformat() if pt.updated_at else None,
    )


from app.runtime_config import HealingConfig, healing_config as _healing_config


@router.get("/healing", response_model=HealingConfig)
async def get_healing_config(admin=Depends(require_admin)) -> HealingConfig:
    """获取自愈选择器配置。"""
    return _healing_config


@router.put("/healing", response_model=HealingConfig)
async def update_healing_config(cfg: HealingConfig, admin=Depends(require_admin)) -> HealingConfig:
    """更新自愈选择器配置。"""
    from app.runtime_config import healing_config as _rt
    _rt.enabled = cfg.enabled
    _rt.max_retries = cfg.max_retries
    _rt.threshold = cfg.threshold
    return _rt
