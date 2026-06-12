"""API router for AI model configuration and prompt templates."""
import logging
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from ..auth import require_admin
from ..database import get_db
from .. import db_models
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
async def get_ai_config(db: Session = Depends(get_db), user = Depends(require_admin)):
    row = db.query(db_models.AIConfig).filter(db_models.AIConfig.id == 1).first()
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
    db: Session = Depends(get_db),
    user = Depends(require_admin),
):
    row = db.query(db_models.AIConfig).filter(db_models.AIConfig.id == 1).first()
    if not row:
        row = db_models.AIConfig(id=1)
        db.add(row)

    row.model = body.model
    if body.api_key:
        row.api_key = encrypt_value(body.api_key)
    row.api_base = body.api_base
    row.temperature = body.temperature

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
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
    db: Session = Depends(get_db),
    user = Depends(require_admin),
):
    """测试 AI 配置是否可用 — 发送一条简单请求验证连接。"""
    model = body.model
    api_key = body.api_key
    api_base = body.api_base

    if not model or not api_key or not api_base:
        row = db.query(db_models.AIConfig).filter(db_models.AIConfig.id == 1).first()
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
    except Exception as exc:
        logger.warning(f"AI config test failed: {exc}")
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
async def list_prompts(db: Session = Depends(get_db), user=Depends(require_admin)):
    """列出所有提示词模板（含默认内容）。"""
    rows = db.query(db_models.PromptTemplate).all()
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
async def get_prompt(key: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    """获取单个提示词模板。"""
    defaults = get_default_prompts()
    if key not in defaults:
        raise HTTPException(404, f"未知的提示词模板: {key}")
    default = defaults[key]["content"]
    row = db.query(db_models.PromptTemplate).filter(
        db_models.PromptTemplate.template_key == key
    ).first()
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
    db: Session = Depends(get_db),
    user=Depends(require_admin),
):
    """保存（覆盖）提示词模板内容。"""
    defaults = get_default_prompts()
    if key not in defaults:
        raise HTTPException(404, f"未知的提示词模板: {key}")
    row = db.query(db_models.PromptTemplate).filter(
        db_models.PromptTemplate.template_key == key
    ).first()
    if not row:
        row = db_models.PromptTemplate(
            template_key=key,
            label=defaults[key]["label"],
            template_content=body.template_content,
            is_custom=True,
        )
        db.add(row)
    else:
        row.template_content = body.template_content
        row.is_custom = True
    db.commit()
    db.refresh(row)
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
    db: Session = Depends(get_db),
    user=Depends(require_admin),
):
    """恢复提示词模板为默认内容。"""
    defaults = get_default_prompts()
    if key not in defaults:
        raise HTTPException(404, f"未知的提示词模板: {key}")
    row = db.query(db_models.PromptTemplate).filter(
        db_models.PromptTemplate.template_key == key
    ).first()
    if row:
        row.template_content = defaults[key]["content"]
        row.is_custom = False
        db.commit()
        db.refresh(row)
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
