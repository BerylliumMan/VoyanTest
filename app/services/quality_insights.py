# app/services/quality_insights.py
# 041 US3：失败聚类 + Flaky（确定性统计优先，AI 只补充解释）。
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from app.crud import api_ai as crud_api_ai
from app.crud import run as crud_run
from app.gen.api.prompts import QUALITY_CLUSTER_PROMPT
from app.tz import now as tz_now
from core.failure_signature import build_signature, cluster_failures
from core.flaky_stats import flaky_stats

logger = logging.getLogger(__name__)


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:48]


def _extract_json(text: str) -> dict | None:
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def _logs_by_run(db: AsyncSession, run_ids: list[int]) -> dict[int, list]:
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(db_models.RunLog)
            .where(db_models.RunLog.run_id.in_(run_ids))
            .order_by(db_models.RunLog.id)
        )
    ).scalars().all()
    out: dict[int, list] = {}
    for row in rows:
        out.setdefault(row.run_id, []).append(row)
    return out


async def _case_history(db: AsyncSession, *, project_id: int, since) -> list[dict]:
    """窗口内全部终态运行（含通过），用于 Flaky 统计。"""
    from sqlalchemy import func

    stmt = (
        select(
            db_models.TestRun.case_id,
            db_models.TestRun.status,
            db_models.TestRun.start_time,
            db_models.TestRun.end_time,
        )
        .outerjoin(db_models.TestCase, db_models.TestRun.case_id == db_models.TestCase.id)
        .outerjoin(db_models.RunBatch, db_models.TestRun.batch_id == db_models.RunBatch.id)
        .where(
            db_models.TestRun.status.in_(("passed", "failed", "error", "cancelled", "skipped")),
            func.coalesce(db_models.TestCase.project_id, db_models.RunBatch.project_id) == project_id,
        )
    )
    if since is not None:
        stmt = stmt.where(
            func.coalesce(db_models.TestRun.start_time, db_models.TestRun.end_time,
                          db_models.RunBatch.created_at) >= since
        )
    stmt = stmt.order_by(db_models.TestRun.id.desc()).limit(5000)
    rows = (await db.execute(stmt)).all()
    return [
        {"case_id": r.case_id, "status": r.status,
         "started_at": (r.start_time or r.end_time).isoformat() if (r.start_time or r.end_time) else None}
        for r in rows
    ]


async def generate_insights(
    db: AsyncSession,
    *,
    project_id: int,
    days: int = 14,
    module_id: int | None = None,
    include_ai: bool = True,
    force: bool = False,
    user_id: int | None = None,
) -> tuple[db_models.ApiAiAnalysis, bool]:
    settings = get_settings()
    days = max(1, min(int(days or settings.quality_cluster_days), 90))
    since = tz_now() - timedelta(days=days)
    failures = await crud_run.list_runs_for_quality(
        db, project_id=project_id, since=since, module_id=module_id,
        limit=settings.quality_cluster_max_runs,
    )
    run_ids = [r["run_id"] for r in failures]
    logs = await _logs_by_run(db, run_ids)
    entries = []
    for r in failures:
        sig = build_signature(logs=logs.get(r["run_id"], []))
        entries.append({
            "run_id": r["run_id"],
            "case_id": r["case_id"],
            "signature": sig["signature"],
            "kind_hint": sig["kind_hint"],
            "summary": sig["raw_error"] or sig["failed_step"] or "无错误摘要",
            "started_at": r["started_at"],
        })
    clustered = cluster_failures(entries, min_runs=2)
    history = await _case_history(db, project_id=project_id, since=since)
    flaky = flaky_stats(history, min_runs=settings.quality_flaky_min_runs)
    result: dict[str, Any] = {
        "window_days": days,
        "generated_at": tz_now().isoformat(),
        "total_failures": clustered["total_failures"],
        "groups": clustered["groups"],
        "singletons": clustered["singletons"],
        "flaky": flaky,
        "ai_available": False,
    }
    digest = _hash({"days": days, "module": module_id, "runs": run_ids,
                    "flaky": [f["case_id"] for f in flaky]})
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="cluster", input_hash=digest,
        )
        if cached and cached.status == "ready" and not cached.stale_at:
            return cached, True

    if include_ai and clustered["groups"]:
        payload = {
            "groups": [
                {k: v for k, v in g.items() if k in ("signature", "kind_hint", "summary",
                                                     "run_count", "case_ids", "first_seen", "last_seen")}
                for g in clustered["groups"][:30]
            ],
            "flaky": flaky[:30],
        }
        prompt = QUALITY_CLUSTER_PROMPT
        try:
            from app.gen.model_client import call_model
            from app.runtime_config import resolve_prompt_for_agent

            resolved = await resolve_prompt_for_agent(
                db, agent_type="generation", prompt_key="quality_cluster_explain",
                variables={}, agent_id=None,
            )
            if resolved and len(resolved.strip()) > 80:
                prompt = resolved
            text = await asyncio.wait_for(
                call_model(
                    [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:settings.quality_ai_max_input_chars]}],
                    agent_type="generation",
                ),
                timeout=settings.quality_ai_timeout_s,
            )
            parsed = _extract_json(str(text or ""))
            if parsed:
                by_sig = {
                    str(g.get("signature")): g
                    for g in (parsed.get("groups") or []) if isinstance(g, dict)
                }
                for group in result["groups"]:
                    extra = by_sig.get(group.get("signature"))
                    if extra:
                        group["title"] = str(extra.get("title") or "")[:200]
                        group["explanation"] = str(extra.get("explanation") or "")[:600]
                        group["next_action"] = str(extra.get("next_action") or "")[:300]
                result["ai_available"] = True
                result["ai_summary"] = str(parsed.get("summary") or "")[:600]
        except asyncio.TimeoutError:
            logger.info("聚类 AI 解释超时，返回确定性统计")
        except Exception as exc:
            logger.warning("聚类 AI 解释失败: %s", exc, exc_info=True)

    # 控制 result 体积
    raw = json.dumps(result, ensure_ascii=False, default=str)
    if len(raw.encode("utf-8")) > 64 * 1024:
        for g in result["groups"]:
            g.pop("run_ids", None)
            g["summary"] = str(g.get("summary") or "")[:180]
            g["explanation"] = str(g.get("explanation") or "")[:300]
            g["next_action"] = str(g.get("next_action") or "")[:160]
        result["groups"] = result["groups"][:30]
        result["singletons"] = result["singletons"][:30]
        result["flaky"] = result["flaky"][:20]
    raw = json.dumps(result, ensure_ascii=False, default=str)
    if len(raw.encode("utf-8")) > 64 * 1024:
        result["groups"] = result["groups"][:12]
        result["singletons"] = result["singletons"][:10]
        result["flaky"] = result["flaky"][:10]
        result["truncated"] = True
    # 仍超限时继续裁剪（保证 upsert 不因 64KB 上限失败）
    while len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")) > 60 * 1024:
        if result["groups"]:
            result["groups"].pop()
        elif result["flaky"]:
            result["flaky"].pop()
        elif result["singletons"]:
            result["singletons"].pop()
        else:
            break
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="cluster", run_id=None, case_id=None,
        input_hash=digest, result=result, model=None, created_by=user_id, status="ready",
    )
    from app.auth import log_audit

    await log_audit(
        db, user_id, "quality.cluster",
        details={"analysis_id": row.id, "project_id": project_id,
                 "days": days, "ai_available": result["ai_available"]},
    )
    return row, False

