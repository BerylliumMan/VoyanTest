# app/services/api_ai_analysis.py
# 040 接口测试 AI：失败归因（MVP）；后续契约/策略复用本模块
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.crud import api_ai as crud_api_ai
from app.config import get_settings
from app.gen.api.prompts import API_FAILURE_ANALYSIS_PROMPT
from app.services.api_ai import ApiAiUnavailable
from app.services.report import load_report
from core.api_runner.variables import mask_headers, mask_secrets_in_text

logger = logging.getLogger(__name__)

_ROOT_CAUSES = {
    "assertion_mismatch", "contract_change", "auth", "data",
    "environment", "server_error", "unknown",
}
_MAX_BODY_CHARS = 8000


def _truncate(value: Any, limit: int = _MAX_BODY_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + f"\n...[TRUNCATED {len(text) - limit} chars]"
    return text


def _safe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _mask_text(text: str, secret_values: list[str] | None = None) -> str:
    out = mask_secrets_in_text(text or "", secret_values or [])
    # 通用凭据模式（模型输入兜底；不依赖调用方是否提供 secret 值）
    out = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._\-]+",
        r"\1***",
        out,
    )
    out = re.sub(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b(\s*[=:]\s*)(\"?)[^\"\s,}]+",
        r"\1\2\3***",
        out,
    )
    return out


def shape_failure_evidence(run: db_models.TestRun, report: dict | None) -> dict:
    """从报告/运行记录提取可送模型的证据；只读、截断、脱敏。"""
    steps = (report or {}).get("steps") or []
    shaped_steps: list[dict] = []
    for idx, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict):
            continue
        req = step.get("request") or {}
        resp = step.get("response") or {}
        assertions = step.get("assertions") or []
        headers = req.get("headers") or {}
        if isinstance(headers, dict):
            headers = mask_headers(headers)
        shaped_steps.append({
            "index": idx + 1,
            "description": _truncate(step.get("description") or step.get("name") or "", 300),
            "success": bool(step.get("success")),
            "error": _truncate(step.get("error") or "", 800),
            "request": {
                "method": req.get("method"),
                "url": _truncate(req.get("url") or "", 800),
                "headers": headers,
                "body": _mask_text(_truncate(req.get("body") or "", 2000)),
            },
            "response": {
                "status": resp.get("status") or resp.get("status_code"),
                "headers": mask_headers(resp.get("headers") or {}) if isinstance(resp.get("headers"), dict) else {},
                "body": _mask_text(_truncate(resp.get("body") or "", _MAX_BODY_CHARS)),
            },
            "assertions": [
                {
                    "name": a.get("name") or a.get("type"),
                    "type": a.get("type"),
                    "condition": a.get("condition"),
                    "expected": _truncate(a.get("expected") or "", 300),
                    "actual": _truncate(a.get("actual") or "", 300),
                    "passed": bool(a.get("passed")),
                    "error": _truncate(a.get("error") or "", 300),
                }
                for a in (assertions if isinstance(assertions, list) else [])[:20]
                if isinstance(a, dict)
            ],
        })
    return {
        "run_id": run.id,
        "case_id": run.case_id,
        "status": run.status,
        "duration_s": run.duration,
        "execution_mode": run.execution_mode,
        "report_status": (report or {}).get("status"),
        "environment": (report or {}).get("environment"),
        "steps": shaped_steps,
        "missing_evidence": [] if shaped_steps else ["report_steps"],
    }


def _extract_json_object(text: str) -> dict | None:
    raw = str(text or "")
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def validate_failure_result(result: Any) -> dict:
    if not isinstance(result, dict):
        raise ApiAiUnavailable("AI 未返回对象结果")
    root = str(result.get("root_cause") or "unknown")
    if root not in _ROOT_CAUSES:
        root = "unknown"
    evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    evidence = [e for e in evidence if isinstance(e, dict)][:10]
    if not evidence and root != "unknown":
        root = "unknown"
    suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
    suggestions = [s for s in suggestions if isinstance(s, dict)][:8]
    missing = result.get("missing_evidence") if isinstance(result.get("missing_evidence"), list) else []
    return {
        "root_cause": root,
        "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
        "summary": _truncate(result.get("summary") or "", 600),
        "evidence": evidence,
        "suggestions": suggestions,
        "missing_evidence": [str(x)[:200] for x in missing[:10]],
    }


def _input_hash(evidence: dict) -> str:
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def analyze_failure(
    db: AsyncSession,
    *,
    run: db_models.TestRun,
    project_id: int,
    user_id: int | None,
    force: bool = False,
) -> db_models.ApiAiAnalysis:
    settings = get_settings()
    report = load_report(run.report_path)
    evidence = shape_failure_evidence(run, report)
    if not evidence["steps"]:
        raise ApiAiUnavailable("运行报告缺少步骤证据，无法分析")
    digest = _input_hash(evidence)
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="failure", run_id=run.id,
        )
        if cached and cached.status == "ready" and cached.input_hash == digest and not cached.stale_at:
            return cached, True
    payload = json.dumps(evidence, ensure_ascii=False)
    if len(payload) > settings.api_ai_max_input_chars:
        payload = payload[: settings.api_ai_max_input_chars] + "\n...[TRUNCATED]"
    prompt = API_FAILURE_ANALYSIS_PROMPT
    try:
        from app.gen.model_client import call_model
        from app.runtime_config import resolve_prompt_for_agent

        resolved = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="api_failure_analysis",
            variables={}, agent_id=None,
        )
        if resolved and len(resolved.strip()) > 80:
            prompt = resolved
        text = await asyncio.wait_for(
            call_model(
                [{"role": "system", "content": prompt}, {"role": "user", "content": payload}],
                agent_type="generation",
            ),
            timeout=settings.api_ai_timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise ApiAiUnavailable(f"AI 分析超时（>{settings.api_ai_timeout_s}s）") from exc
    except ApiAiUnavailable:
        raise
    except Exception as exc:
        logger.warning("失败归因 AI 调用失败: %s", exc, exc_info=True)
        raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc
    parsed = _extract_json_object(str(text or ""))
    if parsed is None:
        raise ApiAiUnavailable("AI 未返回合法 JSON")
    result = validate_failure_result(parsed)
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="failure", run_id=run.id,
        case_id=run.case_id, input_hash=digest, result=result,
        model=None, created_by=user_id, status="ready",
    )
    from app.auth import log_audit
    await log_audit(
        db, user_id, "ai.failure_analysis",
        details={"analysis_id": row.id, "run_id": run.id, "status": "ready"},
    )
    return row, False
