# app/services/quality_analysis.py
# 041 US1：UI/客户端失败归因（run_logs + tool_calls + screenshot；无 report.json 也可用）。
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.crud import api_ai as crud_api_ai
from app.gen.api.prompts import QUALITY_UI_FAILURE_PROMPT
from app.services.api_ai import ApiAiUnavailable
from app.services.report import load_report
from core.ui_failure_evidence import build_ui_failure_evidence, truncate

logger = logging.getLogger(__name__)

_ROOT_CAUSES = {"product_defect", "test_case", "environment", "data", "flaky", "unknown"}
_ACTIONS = {"fix_case", "fix_env", "fix_data", "report_bug", "rerun", "inspect"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _extract_json_object(text: str) -> dict | None:
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def validate_ui_failure_result(result: Any) -> dict:
    if not isinstance(result, dict):
        raise ApiAiUnavailable("AI 未返回对象结果")
    root = str(result.get("root_cause") or "unknown")
    if root not in _ROOT_CAUSES:
        root = "unknown"
    evidence = [e for e in (result.get("evidence") or []) if isinstance(e, dict)][:10]
    if not evidence and root != "unknown":
        root = "unknown"
    suggestions = []
    for s in (result.get("suggestions") or [])[:8]:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or "inspect")
        if action not in _ACTIONS:
            action = "inspect"
        suggestions.append({
            "action": action,
            "detail": truncate(s.get("detail") or "", 500),
        })
    missing = [str(x)[:200] for x in (result.get("missing_evidence") or [])[:10]]
    return {
        "root_cause": root,
        "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
        "summary": truncate(result.get("summary") or "", 600),
        "evidence": evidence,
        "suggestions": suggestions,
        "missing_evidence": missing,
        "screenshot_included": bool(result.get("screenshot_included")),
    }


def _input_hash(evidence: dict) -> str:
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _agent_tool_calls(db: AsyncSession, run: db_models.TestRun) -> list:
    if not run.case_id:
        return []
    rows = (
        await db.execute(
            select(db_models.AgentRun)
            .where(db_models.AgentRun.case_id == run.case_id)
            .order_by(db_models.AgentRun.id.desc())
            .limit(30)
        )
    ).scalars().all()
    agent_run_ids = []
    for ar in rows:
        goal = ar.goal if isinstance(ar.goal, dict) else {}
        if goal.get("test_run_id") == run.id or (
            run.batch_id and goal.get("batch_id") == run.batch_id and goal.get("case_id") == run.case_id
        ):
            agent_run_ids.append(ar.id)
    if not agent_run_ids:
        return []
    return (
        await db.execute(
            select(db_models.AgentToolCall)
            .where(db_models.AgentToolCall.run_id.in_(agent_run_ids))
            .order_by(db_models.AgentToolCall.id)
        )
    ).scalars().all()


async def _last_passed(db: AsyncSession, run: db_models.TestRun) -> dict | None:
    if not run.case_id:
        return None
    row = (
        await db.execute(
            select(db_models.TestRun)
            .where(db_models.TestRun.case_id == run.case_id, db_models.TestRun.status == "passed")
            .order_by(db_models.TestRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "run_id": row.id,
        "status": row.status,
        "duration_s": row.duration,
        "execution_mode": row.execution_mode,
        "start_time": row.start_time.isoformat() if row.start_time else None,
    }


def _image_data_url(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p = p.resolve()
        if not p.is_file() or p.stat().st_size > _MAX_IMAGE_BYTES:
            return None
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except OSError:
        return None


async def analyze_ui_failure(
    db: AsyncSession,
    *,
    run: db_models.TestRun,
    project_id: int,
    user_id: int | None,
    force: bool = False,
) -> tuple[db_models.ApiAiAnalysis, bool]:
    settings = get_settings()
    logs = (
        await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run.id).order_by(db_models.RunLog.id)
        )
    ).scalars().all()
    tools = await _agent_tool_calls(db, run)
    report = load_report(run.report_path)
    environment = None
    if isinstance(report, dict):
        env = report.get("environment")
        if isinstance(env, dict):
            environment = env.get("name") or env.get("base_url")
    # 041：新 UI 运行把环境写入 AgentRun.goal；旧运行回退「未提供」
    if not environment and run.case_id:
        ar = (
            await db.execute(
                select(db_models.AgentRun)
                .where(db_models.AgentRun.case_id == run.case_id)
                .order_by(db_models.AgentRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ar is not None and isinstance(ar.goal, dict):
            environment = ar.goal.get("environment_name") or ar.goal.get("environment")
    case = await db.get(db_models.TestCase, run.case_id) if run.case_id else None
    evidence = build_ui_failure_evidence(
        run=run,
        logs=logs,
        tool_calls=tools,
        case_name=(case.name if case else None) or run.display_name,
        environment=environment,
        last_passed=await _last_passed(db, run),
    )
    if not evidence.get("steps") and not evidence.get("tool_calls"):
        raise ApiAiUnavailable("运行记录缺少步骤证据，无法分析")
    digest = _input_hash(evidence)
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="ui_failure", run_id=run.id,
        )
        if cached and cached.status == "ready" and cached.input_hash == digest and not cached.stale_at:
            return cached, True

    screenshot_path = None
    for path in reversed(evidence.get("screenshots") or []):
        if path and _image_data_url(path):
            screenshot_path = path
            break
    user_content: list[dict] = [{
        "type": "text",
        "text": json.dumps(evidence, ensure_ascii=False)[: settings.quality_ai_max_input_chars],
    }]
    if screenshot_path:
        url = _image_data_url(screenshot_path)
        if url:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
            evidence["screenshot_included"] = True
        else:
            evidence["screenshot_included"] = False
    else:
        evidence["screenshot_included"] = False

    prompt = QUALITY_UI_FAILURE_PROMPT
    try:
        from app.gen.model_client import call_model
        from app.runtime_config import resolve_prompt_for_agent

        resolved = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="quality_ui_failure",
            variables={}, agent_id=None,
        )
        if resolved and len(resolved.strip()) > 80:
            prompt = resolved
        text = await asyncio.wait_for(
            call_model(
                [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
                agent_type="generation",
            ),
            timeout=settings.quality_ai_timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise ApiAiUnavailable(f"AI 分析超时（>{settings.quality_ai_timeout_s}s）") from exc
    except ApiAiUnavailable:
        raise
    except Exception as exc:
        logger.warning("UI 失败归因 AI 调用失败: %s", exc, exc_info=True)
        raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc

    parsed = _extract_json_object(str(text or ""))
    if parsed is None:
        raise ApiAiUnavailable("AI 未返回合法 JSON")
    result = validate_ui_failure_result(parsed)
    result["screenshot_included"] = bool(evidence.get("screenshot_included"))
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="ui_failure", run_id=run.id,
        case_id=run.case_id, input_hash=digest, result=result,
        model=None, created_by=user_id, status="ready",
    )
    from app.auth import log_audit

    await log_audit(
        db, user_id, "quality.failure_analysis",
        details={"analysis_id": row.id, "run_id": run.id, "status": "ready"},
    )
    return row, False
