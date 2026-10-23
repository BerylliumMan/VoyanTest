# app/services/api_ai_generate.py
# 040 US3：自然语言 → 接口用例/场景草稿（预览，不直接落库）
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.gen.api.prompts import API_NL_GENERATE_PROMPT
from app.services.api_ai import ApiAiUnavailable
from core.api_spec import ApiSpecError, normalize_api_spec, validate_api_spec

logger = logging.getLogger(__name__)


def _tokens(text: str) -> set[str]:
    raw = (text or "").lower()
    parts = {t for t in re.split(r"[\s,，。；;、/]+", raw) if t}
    # 中文没有空格：补充 2-gram，便于“创建订单后查询订单列表”命中“创建订单/订单列表”
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        parts.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return parts


async def find_candidates(
    db: AsyncSession, project_id: int, text: str, *, limit: int = 50,
) -> list[db_models.ApiDefinition]:
    rows = (
        await db.execute(
            select(db_models.ApiDefinition)
            .where(db_models.ApiDefinition.project_id == project_id)
            .order_by(db_models.ApiDefinition.id)
            .limit(300)
        )
    ).scalars().all()
    words = _tokens(text)
    scored = []
    for row in rows:
        hay = f"{row.name or ''} {row.path or ''} {row.method or ''}".lower()
        score = sum(1 for w in words if w and w in hay)
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    return [row for _, row in scored[:limit]]


def _extract_json(text: str) -> dict | None:
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def generate_from_text(
    db: AsyncSession,
    *,
    project_id: int,
    text: str,
    target: str = "case",
    user_id: int | None = None,
) -> dict:
    settings = get_settings()
    candidates = await find_candidates(db, project_id, text)
    if not candidates:
        return {"target": target, "summary": "", "candidates": [], "missing": ["项目内没有可用接口定义"], "draft": None}
    payload = json.dumps({
        "target": target,
        "description": text,
        "candidates": [
            {
                "definition_id": c.id, "name": c.name, "method": c.method,
                "path": c.path,
                "request_schema": c.request_schema or {},
                "response_schema": c.response_schema or {},
            }
            for c in candidates
        ],
    }, ensure_ascii=False)
    if len(payload) > settings.api_ai_max_input_chars:
        payload = payload[: settings.api_ai_max_input_chars] + "\n...[TRUNCATED]"
    try:
        from app.gen.model_client import call_model
        from app.runtime_config import resolve_prompt_for_agent

        prompt = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="api_nl_generate",
            variables={}, agent_id=None,
        )
        if not prompt or len(prompt.strip()) < 80:
            prompt = API_NL_GENERATE_PROMPT
        raw = await asyncio.wait_for(
            call_model(
                [{"role": "system", "content": prompt}, {"role": "user", "content": payload}],
                agent_type="generation",
            ),
            timeout=settings.api_ai_timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise ApiAiUnavailable("AI 生成超时") from exc
    except ApiAiUnavailable:
        raise
    except Exception as exc:
        logger.warning("自然语言生成失败: %s", exc, exc_info=True)
        raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc
    parsed = _extract_json(str(raw or ""))
    if parsed is None:
        raise ApiAiUnavailable("AI 未返回合法 JSON")
    allowed_ids = {c.id for c in candidates}
    picked = []
    for item in (parsed.get("candidates") or []):
        if isinstance(item, dict) and item.get("definition_id") in allowed_ids:
            picked.append({"definition_id": item["definition_id"], "reason": str(item.get("reason") or "")[:200]})
    missing = [str(x)[:200] for x in (parsed.get("missing") or [])]
    draft = None
    if target == "case" and isinstance(parsed.get("case"), dict):
        spec = parsed["case"].get("api_spec")
        try:
            spec = normalize_api_spec(spec)
            problems = validate_api_spec(spec)
            if problems:
                missing.append("用例草案校验失败: " + "; ".join(problems[:5]))
            else:
                draft = {
                    "name": str(parsed["case"].get("name") or "AI 生成接口用例")[:255],
                    "priority": str(parsed["case"].get("priority") or "medium"),
                    "api_spec": spec,
                }
        except ApiSpecError as exc:
            missing.append(f"用例草案校验失败: {exc}")
    elif target == "scenario" and isinstance(parsed.get("scenario"), dict):
        scenario = parsed["scenario"]
        draft = {
            "name": str(scenario.get("name") or "AI 生成接口场景")[:255],
            "steps": scenario.get("steps") or [],
            "variables": scenario.get("variables") or [],
            "warnings": scenario.get("warnings") or [],
        }
    return {
        "target": target,
        "summary": str(parsed.get("summary") or "")[:1000],
        "candidates": picked,
        "missing": missing,
        "draft": draft,
    }
