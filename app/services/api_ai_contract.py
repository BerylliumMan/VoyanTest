# app/services/api_ai_contract.py
# 040 US5：契约检测（确定性 diff + 可选 AI 解释）
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.crud import api_ai as crud_api_ai
from app.gen.api.prompts import API_CONTRACT_PROMPT
from app.services.api_ai import ApiAiUnavailable
from app.services.report import load_report
from core.api_contract import diff_response

logger = logging.getLogger(__name__)


async def check_contract(
    db: AsyncSession,
    *,
    run: db_models.TestRun,
    project_id: int,
    definition_id: int | None,
    user_id: int | None,
    force: bool = False,
) -> db_models.ApiAiAnalysis:
    settings = get_settings()
    report = load_report(run.report_path) or {}
    steps = report.get("steps") or []
    step = next((s for s in steps if isinstance(s, dict) and s.get("response")), None)
    if step is None:
        raise ApiAiUnavailable("运行报告缺少响应证据")
    response = step.get("response") or {}
    body = response.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = body
    definition = None
    if definition_id:
        definition = await db.get(db_models.ApiDefinition, definition_id)
    if definition is None and run.case_id:
        case = await db.get(db_models.TestCase, run.case_id)
        if case is not None and case.api_definition_id:
            definition = await db.get(db_models.ApiDefinition, case.api_definition_id)
    if definition is None or definition.project_id != project_id:
        raise ApiAiUnavailable("未找到对应的接口定义，无法做契约检测")
    diff = diff_response(definition.response_schema or {}, response.get("status"), body)
    digest = hashlib.sha256(json.dumps(diff, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="contract", run_id=run.id,
        )
        if cached and cached.status == "ready" and cached.input_hash == digest and not cached.stale_at:
            return cached
    explanation = ""
    if diff.get("status") == "checked" and diff.get("findings"):
        try:
            from app.gen.model_client import call_model
            from app.runtime_config import resolve_prompt_for_agent

            prompt = await resolve_prompt_for_agent(
                db, agent_type="generation", prompt_key="api_contract_analysis",
                variables={}, agent_id=None,
            )
            if not prompt or len(prompt.strip()) < 80:
                prompt = API_CONTRACT_PROMPT
            raw = await asyncio.wait_for(
                call_model([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(diff, ensure_ascii=False)},
                ], agent_type="generation"),
                timeout=settings.api_ai_timeout_s,
            )
            data = json.loads(str(raw or "")[str(raw or "").find("{"): str(raw or "").rfind("}") + 1])
            explanation = str(data.get("summary") or "")[:1000]
        except Exception:
            logger.warning("契约 AI 解释失败（保留确定性 diff）", exc_info=True)
    result = {
        "definition_id": definition.id,
        "definition_name": definition.name,
        "status": diff.get("status"),
        "breaking": bool(diff.get("breaking")),
        "summary": diff.get("summary"),
        "findings": diff.get("findings") or [],
        "explanation": explanation,
    }
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="contract", run_id=run.id,
        case_id=run.case_id, input_hash=digest, result=result,
        created_by=user_id,
    )
    from app.auth import log_audit
    await log_audit(
        db, user_id, "ai.check_contract",
        details={"analysis_id": row.id, "run_id": run.id, "breaking": result["breaking"]},
    )
    return row
