# app/services/api_ai_strategy.py
# 040 US7：接口测试策略与覆盖顾问（只读汇总 + AI 排序）
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.crud import api_ai as crud_api_ai
from app.gen.api.prompts import API_STRATEGY_PROMPT
from app.services.api_ai import ApiAiUnavailable

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


async def build_strategy_input(db: AsyncSession, project_id: int) -> dict:
    defs = (
        await db.execute(
            select(db_models.ApiDefinition).where(db_models.ApiDefinition.project_id == project_id)
        )
    ).scalars().all()
    cases = (
        await db.execute(
            select(db_models.TestCase).where(
                db_models.TestCase.project_id == project_id,
                db_models.TestCase.case_kind == "api",
            )
        )
    ).scalars().all()
    referenced: set[int] = set()
    weak: list[dict] = []
    for case in cases:
        spec = case.api_spec if isinstance(case.api_spec, dict) else {}
        step_defs = {
            int(s.get("definition_id")) for s in (spec.get("steps") or [])
            if isinstance(s, dict) and str(s.get("definition_id") or "").isdigit()
        }
        if case.api_definition_id:
            step_defs.add(int(case.api_definition_id))
        referenced.update(step_defs)
        assertions = [
            a for s in (spec.get("steps") or []) if isinstance(s, dict)
            for a in (s.get("assertions") or []) if isinstance(a, dict)
        ]
        if len(assertions) <= 1 or all(a.get("type") == "status_code" for a in assertions):
            weak.append({"case_id": case.id, "name": case.name, "assertions": len(assertions)})
    uncovered = [
        {"definition_id": d.id, "name": d.name, "method": d.method, "path": d.path}
        for d in defs if d.id not in referenced
    ]
    failed = (
        await db.execute(
            select(db_models.TestRun).where(
                db_models.TestRun.case_id.in_([c.id for c in cases] or [-1]),
                db_models.TestRun.status == "failed",
            ).order_by(db_models.TestRun.id.desc()).limit(50)
        )
    ).scalars().all()
    failure_case_ids = sorted({r.case_id for r in failed if r.case_id})
    return {
        "project_id": project_id,
        "definitions": [
            {"definition_id": d.id, "name": d.name, "method": d.method, "path": d.path}
            for d in defs
        ],
        "cases": [{"case_id": c.id, "name": c.name} for c in cases],
        "uncovered_definitions": uncovered,
        "weak_assertion_cases": weak[:50],
        "recent_failed_case_ids": failure_case_ids,
    }


async def generate_strategy(
    db: AsyncSession,
    *,
    project_id: int,
    user_id: int | None = None,
    force: bool = False,
) -> db_models.ApiAiAnalysis:
    settings = get_settings()
    data = await build_strategy_input(db, project_id)
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="strategy", input_hash=digest,
        )
        if cached and cached.status == "ready" and not cached.stale_at:
            return cached
    if not data["definitions"]:
        raise ApiAiUnavailable("项目内没有接口定义，无法生成策略")
    try:
        from app.gen.model_client import call_model
        from app.runtime_config import resolve_prompt_for_agent

        prompt = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="api_strategy",
            variables={}, agent_id=None,
        )
        if not prompt or len(prompt.strip()) < 80:
            prompt = API_STRATEGY_PROMPT
        raw = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ], agent_type="generation"),
            timeout=settings.api_ai_timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise ApiAiUnavailable("AI 策略生成超时") from exc
    except ApiAiUnavailable:
        raise
    except Exception as exc:
        logger.warning("AI 策略生成失败: %s", exc, exc_info=True)
        raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc
    parsed = _extract_json(str(raw or ""))
    if parsed is None:
        raise ApiAiUnavailable("AI 未返回合法 JSON")
    allowed_defs = {d["definition_id"] for d in data["definitions"]}
    allowed_cases = {c["case_id"] for c in data["cases"]}
    items = []
    for item in (parsed.get("items") or []):
        if not isinstance(item, dict):
            continue
        defs = [x for x in (item.get("definition_ids") or []) if x in allowed_defs]
        cases = [x for x in (item.get("case_ids") or []) if x in allowed_cases]
        if not defs and not cases:
            continue
        items.append({
            "priority": str(item.get("priority") or "medium"),
            "reason": str(item.get("reason") or "")[:300],
            "definition_ids": defs,
            "case_ids": cases,
            "action": str(item.get("action") or "generate_case"),
        })
    result = {
        "summary": str(parsed.get("summary") or "")[:1000],
        "items": items[:20],
        "uncovered_count": len(data["uncovered_definitions"]),
        "weak_assertion_count": len(data["weak_assertion_cases"]),
    }
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="strategy", input_hash=digest,
        result=result, created_by=user_id,
    )
    from app.auth import log_audit
    await log_audit(
        db, user_id, "ai.strategy",
        details={"analysis_id": row.id, "project_id": project_id, "items": len(items)},
    )
    return row
