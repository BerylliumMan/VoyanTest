# app/services/api_ai_data.py
# 040 US4：AI 测试数据工厂（预览/保存为接口数据集）
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.crud import api_dataset as crud_dataset
from app.gen.api.prompts import API_DATA_FACTORY_PROMPT
from app.services.api_ai import ApiAiUnavailable
from core.api_runner.variables import is_secret_name

logger = logging.getLogger(__name__)


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


async def generate_data(
    db: AsyncSession,
    *,
    definition: db_models.ApiDefinition,
    categories: list[str] | None = None,
    row_count: int = 10,
    save: bool = False,
    name: str | None = None,
    project_id: int,
    user_id: int | None = None,
) -> dict:
    settings = get_settings()
    payload = json.dumps({
        "definition": {
            "name": definition.name, "method": definition.method, "path": definition.path,
            "request_schema": definition.request_schema or {},
            "response_schema": definition.response_schema or {},
        },
        "categories": categories or ["normal", "boundary", "type_error", "special", "long"],
        "rows": max(1, min(row_count, 200)),
    }, ensure_ascii=False)
    try:
        from app.gen.model_client import call_model
        from app.runtime_config import resolve_prompt_for_agent

        prompt = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="api_data_factory",
            variables={}, agent_id=None,
        )
        if not prompt or len(prompt.strip()) < 80:
            prompt = API_DATA_FACTORY_PROMPT
        raw = await asyncio.wait_for(
            call_model(
                [{"role": "system", "content": prompt}, {"role": "user", "content": payload}],
                agent_type="generation",
            ),
            timeout=settings.api_ai_timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise ApiAiUnavailable("AI 数据生成超时") from exc
    except ApiAiUnavailable:
        raise
    except Exception as exc:
        logger.warning("AI 数据生成失败: %s", exc, exc_info=True)
        raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc
    parsed = _extract_json(str(raw or ""))
    if parsed is None:
        raise ApiAiUnavailable("AI 未返回合法 JSON")
    columns = [str(c).strip() for c in (parsed.get("columns") or []) if str(c).strip()]
    rows = parsed.get("rows") or []
    if not columns or not isinstance(rows, list):
        raise ApiAiUnavailable("AI 未返回 columns/rows")
    try:
        columns = crud_dataset.validate_dataset_fields(columns, rows)
    except crud_dataset.DatasetValidationError as exc:
        raise ApiAiUnavailable(f"AI 数据不合法：{exc}") from exc
    sensitive = [c for c in columns if is_secret_name(c)]
    meta = {
        "source": "ai",
        "sensitive_columns": sensitive,
        "generated_at": None,
    }
    preview = {
        "columns": columns,
        "rows": rows[:200],
        "expected": parsed.get("expected") or [],
        "meta": meta,
        "sensitive_columns": sensitive,
    }
    if save:
        obj = await crud_dataset.create_dataset(db, {
            "project_id": project_id,
            "name": name or f"AI 数据 · {definition.name}",
            "columns": columns,
            "rows": rows,
            "source": "ai",
            "purpose": "api",
            "meta": meta,
        })
        preview["dataset_id"] = obj.id
    return preview
