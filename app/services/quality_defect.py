# app/services/quality_defect.py
# 041 US4：缺陷报告 Markdown + 证据文件（不自动提交外部系统）。
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.crud import api_ai as crud_api_ai
from app.gen.api.prompts import QUALITY_DEFECT_PROMPT
from app.services.report import load_report
from core.evidence_bundle import extract_evidence_paths
from core.ui_failure_evidence import mask_text, truncate

logger = logging.getLogger(__name__)
REPORTS_ROOT = Path(os.path.abspath("reports"))


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:48]


def _extract_json(text: str) -> dict | None:
    raw = str(text or "")
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(raw[i:j + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def _environment_for_run(db: AsyncSession, run: db_models.TestRun, report: dict | None) -> str:
    if isinstance(report, dict):
        env = report.get("environment")
        if isinstance(env, dict):
            return str(env.get("name") or env.get("base_url") or "未提供")
        if isinstance(env, str) and env.strip():
            return env
    if run.case_id:
        ar = (
            await db.execute(
                select(db_models.AgentRun)
                .where(db_models.AgentRun.case_id == run.case_id)
                .order_by(db_models.AgentRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ar is not None and isinstance(ar.goal, dict):
            return str(ar.goal.get("environment_name") or ar.goal.get("environment") or "未提供")
    return "未提供"


def _steps_from_report(report: dict | None) -> list[dict]:
    out = []
    for s in (report or {}).get("steps") or []:
        if isinstance(s, dict):
            out.append({
                "name": s.get("name") or s.get("description") or "",
                "status": s.get("status") or ("passed" if s.get("success") else "failed"),
                "error": s.get("error") or "",
                "screenshot": s.get("screenshot_path"),
            })
    return out


def _case_steps(case: db_models.TestCase | None) -> list[dict]:
    if case is None:
        return []
    return [
        {"description": s.description or "", "expected": s.parsed_result or ""}
        for s in sorted(case.steps or [], key=lambda x: x.step_order)
    ]


async def _generate_ai_summary(
    db: AsyncSession, *, title: str, environment: str, steps: list[dict], evidence: list[str],
) -> tuple[str, str]:
    settings = get_settings()
    payload = {
        "title": title,
        "environment": environment,
        "steps": [s.get("description") or s.get("name") for s in steps][:30],
        "evidence": evidence[:20],
    }
    prompt = QUALITY_DEFECT_PROMPT
    try:
        from app.gen.model_client import call_model
        from app.runtime_config import resolve_prompt_for_agent

        resolved = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="quality_defect_report",
            variables={}, agent_id=None,
        )
        if resolved and len(resolved.strip()) > 80:
            prompt = resolved
        text = await asyncio.wait_for(
            call_model(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:20000]}],
                agent_type="generation",
            ),
            timeout=settings.quality_ai_timeout_s,
        )
        parsed = _extract_json(str(text or ""))
        if parsed:
            return str(parsed.get("title") or "")[:200], str(parsed.get("summary") or "")[:600]
    except Exception:
        logger.info("缺陷报告 AI 摘要不可用，使用模板", exc_info=True)
    return "", ""


def _render_markdown(*, title: str, fields: dict, notes: str) -> str:
    lines = [f"# {title}", ""]
    for key, label in [
        ("environment", "环境"),
        ("preconditions", "前置条件"),
        ("steps", "复现步骤"),
        ("expected", "期望结果"),
        ("actual", "实际结果"),
        ("evidence", "证据"),
    ]:
        val = fields.get(key)
        lines.append(f"## {label}")
        if isinstance(val, list):
            if not val:
                lines.append("未提供")
            else:
                lines.extend(f"{i}. {v}" for i, v in enumerate(val, 1))
        else:
            lines.append(str(val or "未提供"))
        lines.append("")
    if notes:
        lines.append("## 备注")
        lines.append(notes)
        lines.append("")
    return "\n".join(lines)


async def create_defect_report(
    db: AsyncSession,
    *,
    project_id: int,
    run_id: int | None = None,
    group_signature: str | None = None,
    days: int = 14,
    user_id: int | None = None,
    force: bool = False,
) -> tuple[db_models.ApiAiAnalysis, bool]:
    run = await db.get(db_models.TestRun, run_id) if run_id else None
    if group_signature and run is None:
        latest = (
            await db.execute(
                select(db_models.ApiAiAnalysis)
                .where(
                    db_models.ApiAiAnalysis.project_id == project_id,
                    db_models.ApiAiAnalysis.kind == "cluster",
                    db_models.ApiAiAnalysis.status == "ready",
                )
                .order_by(db_models.ApiAiAnalysis.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest:
            for g in (latest.result or {}).get("groups", []):
                if isinstance(g, dict) and g.get("signature") == group_signature:
                    run = await db.get(db_models.TestRun, g.get("representative_run_id"))
                    break
    if run is None:
        raise ValueError("运行不存在或问题组没有代表运行")
    if run.case_id:
        case = await db.get(db_models.TestCase, run.case_id)
    else:
        case = None
    if case is not None and case.project_id != project_id:
        raise ValueError("运行不属于当前项目")
    report = load_report(run.report_path)
    logs = (
        await db.execute(select(db_models.RunLog).where(db_models.RunLog.run_id == run.id))
    ).scalars().all()
    environment = await _environment_for_run(db, run, report)
    steps = _steps_from_report(report)
    if not steps:
        steps = _case_steps(case)
    expected = "\n".join(
        str(s.get("expected") or "") for s in _case_steps(case) if s.get("expected")
    ) or "未提供"
    failed = next((s for s in steps if str(s.get("status")).lower() in ("failed", "error")), None)
    ui_error = ""
    if not failed:
        for lg in logs:
            if str(lg.level or "").lower() in ("error", "critical", "failed"):
                ui_error = str(lg.message or "")
                break
    actual = (failed or {}).get("error") or ui_error or "未提供"
    evidence_paths = []
    for s in steps:
        if s.get("screenshot"):
            evidence_paths.append(str(s["screenshot"]))
    for lg in logs:
        if lg.screenshot_path:
            evidence_paths.append(str(lg.screenshot_path))
        evidence_paths.extend(extract_evidence_paths([lg.message or ""]))
    if run.report_path:
        evidence_paths.extend(extract_evidence_paths([run.report_path]))
    evidence_paths = list(dict.fromkeys(evidence_paths))
    title_prefix = f"[{case.name if case else '自定义请求'}]"
    phenomenon = mask_text(truncate((failed or {}).get("name") or actual, 80))
    title = f"{title_prefix} {phenomenon or '执行失败'}"[:250]
    preconditions = mask_text((case.description if case else "") or "未提供")
    step_texts = [mask_text(str(s.get("name") or s.get("description") or "")) for s in steps][:50]
    actual = mask_text(truncate(actual, 1000))
    expected = mask_text(truncate(expected, 1000))
    fields = {
        "environment": mask_text(environment),
        "preconditions": preconditions,
        "steps": step_texts,
        "expected": expected,
        "actual": actual,
        "evidence": [f"/{p}" if not p.startswith("/") else p for p in evidence_paths[:20]],
    }
    ai_title, ai_summary = await _generate_ai_summary(
        db, title=title, environment=environment, steps=steps, evidence=evidence_paths,
    )
    if ai_title:
        title = mask_text(ai_title)
    markdown = _render_markdown(title=title, fields=fields, notes=mask_text(ai_summary))
    result = {
        "title": title,
        "markdown": markdown,
        "fields": fields,
        "evidence_files": evidence_paths[:100],
        "masked": True,
    }
    digest = _hash({"run_id": run.id, "group": group_signature, "days": days})
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="defect", run_id=run.id,
        )
        if cached and cached.status == "ready" and cached.input_hash == digest and not cached.stale_at:
            return cached, True
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="defect", run_id=run.id,
        case_id=run.case_id, input_hash=digest, result=result,
        model=None, created_by=user_id, status="ready",
    )
    from app.auth import log_audit

    await log_audit(
        db, user_id, "quality.defect_report",
        details={"analysis_id": row.id, "run_id": run.id, "project_id": project_id},
    )
    return row, False
