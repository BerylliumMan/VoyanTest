# app/services/quality_review.py
# 041 US2：用例评审（确定性规则 + 可选 LLM 增强；只产出建议，不直接写库）。
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
from app.gen.api.prompts import QUALITY_CASE_REVIEW_PROMPT
from app.services.api_ai import ApiAiUnavailable
from core.case_review_rules import evaluate_case, normalize_text

logger = logging.getLogger(__name__)

_MAX_ITEMS = 50
_MAX_RESULT_BYTES = 64 * 1024


class ReviewNotFound(Exception):
    """评审目标不存在或不属于项目（路由转 404）。"""


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def _steps_of(case: db_models.TestCase) -> list[dict]:
    return [
        {"description": s.description or "", "expected": s.parsed_result or ""}
        for s in sorted(case.steps or [], key=lambda s: s.step_order)
    ]


def _draft_payload(tc: db_models.GenTestCase) -> dict:
    return {
        "name": tc.title or "",
        "title": tc.title or "",
        "module": tc.module or "",
        "test_steps": tc.test_steps or "",
        "expected_result": tc.expected_result or "",
        "priority": tc.priority,
        "structured_steps": tc.structured_steps,
    }


def _case_payload(case: db_models.TestCase) -> dict:
    kind = case.case_kind or "functional"
    steps = _steps_of(case)
    if kind == "api" and isinstance(case.api_spec, dict):
        api_steps = case.api_spec.get("steps") or []
        descriptions = []
        assertions = []
        for s in api_steps:
            if not isinstance(s, dict):
                continue
            req = s.get("request") or {}
            descriptions.append(
                f"{req.get('method', 'GET')} {req.get('url', '')} {s.get('name') or ''}".strip()
            )
            assertions.extend(s.get("assertions") or [])
        steps = [{"description": d, "expected": ""} for d in descriptions]
        return {
            "name": case.name,
            "title": case.name,
            "module": str(case.module_id or ""),
            "steps": steps,
            "expected_result": "",
            "assertions": assertions,
            "priority": case.priority,
            "case_kind": kind,
        }
    return {
        "name": case.name,
        "title": case.name,
        "module": str(case.module_id or ""),
        "steps": steps,
        "expected_result": "\n".join(s.get("expected") or "" for s in steps),
        "priority": case.priority,
        "case_kind": kind,
    }


def _case_hash(case: db_models.TestCase) -> str:
    return _json_hash(_case_payload(case))


def _draft_hash(tc: db_models.GenTestCase) -> str:
    return _json_hash(_draft_payload(tc))


async def _load_targets(
    db: AsyncSession, *, project_id: int, target_type: str, target_ids: list[int],
) -> list[tuple[str, Any, dict, str]]:
    """返回 [(kind, row, payload, base_hash)]，并校验项目归属。"""
    out: list[tuple[str, Any, dict, str]] = []
    for target_id in target_ids:
        if target_type == "draft":
            row = await db.get(db_models.GenTestCase, target_id)
            if row is None:
                raise ReviewNotFound(f"草稿 #{target_id} 不存在")
            session = await db.get(db_models.GenSession, row.session_id)
            if session is None or session.project_id != project_id:
                raise ReviewNotFound(f"草稿 #{target_id} 不属于当前项目")
            payload = _draft_payload(row)
            out.append(("draft", row, payload, _draft_hash(row)))
        else:
            row = await db.get(db_models.TestCase, target_id)
            if row is None:
                raise ReviewNotFound(f"用例 #{target_id} 不存在")
            if row.project_id != project_id:
                raise ReviewNotFound(f"用例 #{target_id} 不属于当前项目")
            payload = _case_payload(row)
            out.append(("case", row, payload, _case_hash(row)))
    return out


async def _similar_cases(
    db: AsyncSession, project_id: int, limit: int = 200,
    exclude_case_ids: list[int] | None = None,
) -> list[dict]:
    stmt = select(db_models.TestCase).where(db_models.TestCase.project_id == project_id)
    if exclude_case_ids:
        stmt = stmt.where(db_models.TestCase.id.notin_(exclude_case_ids))
    rows = (
        await db.execute(stmt.order_by(db_models.TestCase.id.desc()).limit(limit))
    ).scalars().all()
    return [{"name": r.name, "title": r.name, "test_steps": "", "steps": _steps_of(r)} for r in rows]


def _rule_review(payload: dict, similar: list[dict]) -> dict:
    return evaluate_case(payload, similar_cases=similar)


def _merge_llm(review: dict, llm_item: dict | None) -> dict:
    if not isinstance(llm_item, dict):
        return review
    score = llm_item.get("score")
    if isinstance(score, (int, float)):
        review["score"] = max(0, min(100, int(score)))
    issues = list(review.get("issues") or [])
    seen = {(i.get("type"), i.get("detail")) for i in issues}
    for item in (llm_item.get("issues") or [])[:10]:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "other")
        detail = str(item.get("detail") or "")[:300]
        if (typ, detail) in seen:
            continue
        issues.append({
            "type": typ,
            "severity": str(item.get("severity") or "medium"),
            "detail": detail,
            "suggestion": str(item.get("suggestion") or "")[:500],
        })
        seen.add((typ, detail))
    review["issues"] = issues[:20]
    if llm_item.get("summary"):
        review["summary"] = str(llm_item["summary"])[:300]
    # 同一类型只保留最高严重度的一条，避免规则 + LLM 重复刷屏
    rank = {"high": 3, "medium": 2, "low": 1}
    best: dict[str, dict] = {}
    for issue in review["issues"]:
        key = str(issue.get("type") or "other")
        old = best.get(key)
        if old is None or rank.get(str(issue.get("severity")), 0) > rank.get(str(old.get("severity")), 0):
            best[key] = issue
    review["issues"] = list(best.values())
    return review


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


async def _llm_review(
    db: AsyncSession,
    *,
    items: list[tuple[str, Any, dict, str]],
    similar: list[dict],
    user_id: int | None,
) -> dict[int, dict]:
    """分块调用模型（单次 12 条），避免大草稿集一次超时。"""
    settings = get_settings()
    prompt = QUALITY_CASE_REVIEW_PROMPT
    from app.gen.model_client import call_model
    from app.runtime_config import resolve_prompt_for_agent

    try:
        resolved = await resolve_prompt_for_agent(
            db, agent_type="generation", prompt_key="quality_case_review",
            variables={}, agent_id=None,
        )
        if resolved and len(resolved.strip()) > 80:
            prompt = resolved
    except Exception:
        logger.debug("resolve quality review prompt failed", exc_info=True)

    out: dict[int, dict] = {}
    chunk_size = 12
    for start in range(0, len(items), chunk_size):
        chunk = items[start:start + chunk_size]
        payload = {
            "cases": [
                {"target_id": row.id, "target_type": kind,
                 **{k: v for k, v in data.items() if k != "structured_steps"}}
                for kind, row, data, _ in chunk
            ],
            "similar_cases": [{"name": s["name"]} for s in similar[:60]],
        }
        raw = json.dumps(payload, ensure_ascii=False, default=str)[: settings.quality_ai_max_input_chars]
        try:
            text = await asyncio.wait_for(
                call_model(
                    [{"role": "system", "content": prompt}, {"role": "user", "content": raw}],
                    agent_type="generation",
                ),
                timeout=settings.quality_ai_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise ApiAiUnavailable(f"AI 评审超时（>{settings.quality_ai_timeout_s}s）") from exc
        except ApiAiUnavailable:
            raise
        except Exception as exc:
            logger.warning("用例评审 AI 调用失败: %s", exc, exc_info=True)
            raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc
        parsed = _extract_json(str(text or ""))
        if parsed is None or not isinstance(parsed.get("items"), list):
            raise ApiAiUnavailable("AI 未返回合法评审 JSON")
        for item in parsed["items"]:
            if not isinstance(item, dict):
                continue
            try:
                tid = int(item.get("target_id"))
            except (TypeError, ValueError):
                continue
            out[tid] = item
    return out


def _trim_result(result: dict) -> dict:
    """保证结果 ≤64KB：优先截断 issues/summary，不丢条目。"""
    def size(obj: Any) -> int:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    if size(result) <= _MAX_RESULT_BYTES:
        return result
    for item in result.get("items", []):
        item["issues"] = item.get("issues", [])[:3]
        for issue in item["issues"]:
            issue["detail"] = str(issue.get("detail") or "")[:120]
            issue["suggestion"] = str(issue.get("suggestion") or "")[:160]
    if size(result) > _MAX_RESULT_BYTES:
        result["items"] = result.get("items", [])[:25]
        result["truncated"] = True
    while size(result) > 60 * 1024 and result.get("items"):
        result["items"].pop()
        result["truncated"] = True
    if size(result) > _MAX_RESULT_BYTES:
        result["items"] = []
        result["summary"] = str(result.get("summary") or "")[:500]
        result["truncated"] = True
    return result


async def review_cases(
    db: AsyncSession,
    *,
    project_id: int,
    target_type: str,
    target_ids: list[int],
    use_llm: bool = True,
    force: bool = False,
    user_id: int | None = None,
    on_progress=None,
) -> tuple[db_models.ApiAiAnalysis, bool]:
    settings = get_settings()
    if not target_ids:
        raise ApiAiUnavailable("target_ids 不能为空")
    if len(target_ids) > settings.quality_review_batch_max:
        raise ApiAiUnavailable(f"单次最多 {settings.quality_review_batch_max} 条")
    targets = await _load_targets(
        db, project_id=project_id, target_type=target_type, target_ids=target_ids,
    )
    similar = await _similar_cases(
        db, project_id,
        exclude_case_ids=target_ids if target_type == "case" else None,
    )
    digest = _json_hash({
        "targets": [(kind, data, h) for kind, _, data, h in targets],
        "similar": [s["name"] for s in similar[:100]],
        "use_llm": use_llm,
    })
    if not force:
        cached = await crud_api_ai.get_analysis(
            db, project_id=project_id, kind="review", input_hash=digest,
        )
        if cached and cached.status == "ready" and not cached.stale_at:
            return cached, True

    rule_results = []
    total = len(targets)
    for idx, (kind, row, data, base_hash) in enumerate(targets, 1):
        review = _rule_review(data, similar)
        review.update({
            "target_type": kind,
            "target_id": row.id,
            "name": data.get("name") or data.get("title") or f"#{row.id}",
            "base_hash": base_hash,
        })
        rule_results.append(review)
        if on_progress:
            on_progress(idx, total)

    llm_map: dict[int, dict] = {}
    if use_llm:
        if on_progress:
            on_progress(max(1, total // 2), total)
        llm_map = await _llm_review(db, items=targets, similar=similar, user_id=user_id)
    for review in rule_results:
        if review["target_id"] in llm_map:
            _merge_llm(review, llm_map[review["target_id"]])

    result = {
        "summary": f"评审 {len(rule_results)} 条，{sum(1 for r in rule_results if r['score'] < settings.quality_review_low_score)} 条低于 {settings.quality_review_low_score} 分",
        "truncated": False,
        "low_score_threshold": settings.quality_review_low_score,
        "items": rule_results,
    }
    result = _trim_result(result)
    row = await crud_api_ai.upsert_analysis(
        db, project_id=project_id, kind="review", run_id=None,
        case_id=target_ids[0] if target_type == "case" and len(target_ids) == 1 else None,
        input_hash=digest, result=result, model=None, created_by=user_id, status="ready",
    )
    from app.auth import log_audit

    await log_audit(
        db, user_id, "quality.case_review",
        details={"analysis_id": row.id, "project_id": project_id, "count": len(target_ids),
                 "target_type": target_type, "status": "ready"},
    )
    return row, False


class ReviewStale(Exception):
    """评审后目标内容已变化（路由转 409）。"""


async def apply_review_suggestion(
    db: AsyncSession,
    *,
    analysis: db_models.ApiAiAnalysis,
    target_type: str,
    target_id: int,
    base_hash: str,
    fields: dict,
    user_id: int | None = None,
) -> dict:
    """确认后应用评审建议；未确认不会走到这里，且前后内容哈希双检。"""
    from app.models import schemas
    from app.crud import gen as crud_gen
    from app.crud import testcase as crud_testcase

    item = None
    for it in (analysis.result or {}).get("items", []):
        if isinstance(it, dict) and int(it.get("target_id", -1)) == int(target_id):
            item = it
            break
    if item is None:
        raise ReviewNotFound("评审结果中没有该目标")

    if target_type == "case":
        case = await db.get(db_models.TestCase, target_id)
        if case is None or case.project_id != analysis.project_id:
            raise ReviewNotFound("用例不存在")
        current = _case_hash(case)
        if base_hash and base_hash != current:
            raise ReviewStale("用例已在评审后修改，请重新评审")
        existing_steps = _steps_of(case)
        new_steps = fields.get("steps")
        if new_steps is None:
            new_steps = existing_steps
        steps = []
        for idx, s in enumerate(new_steps, 1):
            if isinstance(s, str):
                desc, expected = s, None
            else:
                desc = str(s.get("description") or s.get("text") or "")
                expected = s.get("expected") or s.get("parsed_result")
            if not desc.strip():
                continue
            steps.append(schemas.TestStepUpdate(
                step_order=idx, description=desc[:2000], parsed_result=expected,
            ))
        api_spec = fields.get("api_spec")
        if api_spec is not None:
            from core.api_spec import normalize_api_spec, validate_api_spec

            api_spec = normalize_api_spec(api_spec)
            problems = validate_api_spec(api_spec)
            if problems:
                raise ValueError("；".join(problems[:5]))
        payload = schemas.TestCaseUpdate(
            name=(fields.get("name") or case.name)[:255],
            description=case.description,
            module_id=case.module_id,
            steps=steps,
            priority=case.priority,
            case_kind=case.case_kind,
            api_spec=api_spec if api_spec is not None else case.api_spec,
        )
        await crud_testcase.update_test_case(
            db, target_id, payload,
            revision_user_id=user_id,
            revision_source="review",
            revision_summary="AI 评审建议应用",
        )
        rev = (
            await db.execute(
                select(db_models.CaseRevision)
                .where(db_models.CaseRevision.case_id == target_id)
                .order_by(db_models.CaseRevision.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        result = {"applied": True, "target_type": "case", "target_id": target_id,
                  "revision_id": rev.id if rev else None}
    else:
        tc = await db.get(db_models.GenTestCase, target_id)
        if tc is None:
            raise ReviewNotFound("草稿不存在")
        session = await db.get(db_models.GenSession, tc.session_id)
        if session is None or session.project_id != analysis.project_id:
            raise ReviewNotFound("草稿不属于当前项目")
        current = _draft_hash(tc)
        if base_hash and base_hash != current:
            raise ReviewStale("草稿已在评审后修改，请重新评审")
        update: dict = {}
        if fields.get("name"):
            update["title"] = str(fields["name"])[:500]
        if fields.get("test_steps") is not None:
            update["test_steps"] = str(fields["test_steps"])
        if fields.get("steps") is not None:
            lines = []
            for idx, s in enumerate(fields["steps"], 1):
                text = s if isinstance(s, str) else (s.get("description") or s.get("text") or "")
                if text:
                    lines.append(f"{idx}. {text}")
            update["test_steps"] = "\n".join(lines)
        if fields.get("api_spec") is not None:
            from core.api_spec import normalize_api_spec, validate_api_spec

            spec = normalize_api_spec(fields["api_spec"])
            problems = validate_api_spec(spec)
            if problems:
                raise ValueError("；".join(problems[:5]))
            old = tc.structured_steps if isinstance(tc.structured_steps, dict) else {}
            update["structured_steps"] = {**old, "kind": "api", "api_spec": spec}
        elif fields.get("test_steps") is not None or fields.get("steps") is not None:
            # 文本步骤已修改：清空旧结构化缓存，导入时按新文本重新解析，避免旧步骤覆盖
            update["structured_steps"] = None
        await crud_gen.update_gen_test_case_raw(db, target_id, update)
        result = {"applied": True, "target_type": "draft", "target_id": target_id}

    from app.auth import log_audit

    await log_audit(
        db, user_id, "quality.case_review_apply",
        details={"analysis_id": analysis.id, "target_type": target_type, "target_id": target_id},
    )
    return result
