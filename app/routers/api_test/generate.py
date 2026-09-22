# app/routers/api_test/generate.py — 接口用例生成与导入（029-api-testing 契约 §1.3）
#
# 草案落库复用 gen_sessions（session_kind='api'）+ gen_test_cases：
#   structured_steps = {"kind": "api", "api_spec": {...}, "definition_id": N, ...}
#   test_steps       = 人读渲染（预览页直接展示）
#   title/priority   = 用例名与优先级
# 导入时按 api_spec 快照创建 test_cases(case_kind='api')。
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.crud import api_definition as crud_api_def
from app.crud import testcase as crud_testcase
from app.database import get_async_db
from app.models.schemas import ApiGenerateRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


def _step_summary(step: dict) -> str:
    request = step.get("request") or {}
    head = f"{request.get('method', 'GET')} {request.get('url', '')}"
    assertions = step.get("assertions") or []
    if not assertions:
        return head
    parts = []
    for a in assertions:
        if not isinstance(a, dict):
            continue
        expected = a.get("expected")
        parts.append(
            f"断言 {a.get('type')} {a.get('condition', 'equals')}"
            + (f" {expected}" if expected not in (None, "") else "")
        )
    return head + " | " + "；".join(parts) if parts else head


def _render_steps(spec: dict) -> str:
    lines = []
    for step in spec.get("steps", []):
        order = step.get("order", len(lines) + 1)
        name = step.get("name") or ""
        lines.append(f"{order}. {name} {_step_summary(step)}".strip())
    return "\n".join(lines)


def _is_multipart_definition(definition: dict) -> bool:
    body = (definition.get("request_schema") or {}).get("body") or {}
    return str(body.get("content_type") or "").lower().startswith("multipart/form-data")


def _needs_auth(definition: dict) -> bool:
    """definition 是否声明了鉴权要求（解析器写入的 request_schema.security）。"""
    security = (definition.get("request_schema") or {}).get("security")
    return bool(security)


def _login_candidate(row) -> dict | None:
    """把 api_definitions 行转成生成器需要的 login 定义（非 POST 或带鉴权的不算）。"""
    if str(row.method or "").upper() != "POST":
        return None
    if _needs_auth({"request_schema": row.request_schema or {}}):
        return None  # 登录接口自己不该要求鉴权
    body_text = json.dumps(row.request_schema or {}, ensure_ascii=False).lower()
    path = str(row.path or "").lower()
    looks_like_login = (
        "password" in body_text
        or path.endswith(("login", "signin", "token", "auth"))
        or "/login" in path
    )
    if not looks_like_login:
        return None
    return {
        "id": row.id,
        "name": row.name,
        "method": row.method,
        "path": row.path,
        "request_schema": row.request_schema or {},
    }


async def _find_login_definition(
    db: AsyncSession, project_id: int, definitions: list
) -> dict | None:
    """在项目里找登录接口定义（优先后端字段含 password 的 POST）。"""
    for row in await crud_api_def.list_api_definitions(db, project_id, limit=500):
        candidate = _login_candidate(row)
        if candidate and "password" in json.dumps(
            candidate["request_schema"], ensure_ascii=False
        ).lower():
            return candidate
    for row in definitions:  # 兜底：目标定义里就有登录接口
        candidate = _login_candidate(row)
        if candidate:
            return candidate
    return None


async def _persist_drafts(
    db: AsyncSession,
    session_id: str,
    definition: dict,
    cases: list[dict],
    seq_start: int,
) -> list[dict]:
    drafts: list[dict] = []
    for offset, case in enumerate(cases):
        index = seq_start + offset
        spec = case["api_spec"]
        assertions = (spec.get("steps") or [{}])[0].get("assertions") or []
        expected = "; ".join(
            f"{a.get('type')}={a.get('expected')}" for a in assertions if isinstance(a, dict)
        )
        row = db_models.GenTestCase(
            session_id=session_id,
            test_case_id=f"API-{index:03d}",
            module=str(definition.get("name") or definition.get("path") or ""),
            title=case["name"],
            preconditions="",
            test_steps=_render_steps(spec),
            expected_result=expected,
            priority=case.get("priority") or "medium",
            structured_steps={
                "kind": "api",
                "api_spec": spec,
                "definition_id": definition.get("id"),
                "notes": case.get("notes") or [],
                "source": case.get("source") or "rule",
            },
        )
        db.add(row)
        await db.flush()
        drafts.append(
            {
                "draft_id": row.id,
                "definition_id": definition.get("id"),
                "name": row.title,
                "priority": row.priority,
                "api_spec": spec,
                "notes": case.get("notes") or [],
            }
        )
    return drafts


@router.post("/generate")
async def generate_api_cases(
    payload: ApiGenerateRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """按接口定义生成参数化接口用例（规则 + 可选 LLM 增强）。"""
    from app.gen.api.generator import generate_cases_for_definition

    _ensure_project_access(user, payload.project_id)
    options = payload.options.model_dump() if payload.options else {}

    if payload.definition_ids:
        definitions = []
        for definition_id in payload.definition_ids[:200]:
            obj = await crud_api_def.get_api_definition(db, definition_id)
            if obj is not None and obj.project_id == payload.project_id:
                definitions.append(obj)
    else:
        definitions = await crud_api_def.list_api_definitions(
            db, payload.project_id, module_id=payload.module_id, limit=200
        )
    if not definitions:
        raise HTTPException(status_code=400, detail="没有可生成用例的接口定义")

    auth_definition: dict | None = None
    if any(_needs_auth({
        "request_schema": d.request_schema or {},
    }) for d in definitions):
        auth_definition = await _find_login_definition(db, payload.project_id, definitions)

    session_id = uuid.uuid4().hex
    # 会话必须先落库：gen_test_cases.session_id 是外键，草案在循环内写库
    session = db_models.GenSession(
        id=session_id,
        filename=f"接口用例生成（{len(definitions)} 个接口）",
        filenames=json.dumps(
            [f"{d.method} {d.path}" for d in definitions], ensure_ascii=False
        ),
        project_id=payload.project_id,
        user_id=getattr(user, "id", None),
        project_description="接口测试用例生成（规则 + AI 增强）",
        status="running",
        progress=0,
        progress_message="生成中",
        session_kind="api",
        functional_points_count=len(definitions),
        test_cases_count=0,
        imported_count=0,
    )
    db.add(session)
    await db.flush()

    all_drafts: list[dict] = []
    notes: list[str] = []
    seq = 1
    for obj in definitions:
        definition = {
            "id": obj.id,
            "name": obj.name,
            "method": obj.method,
            "path": obj.path,
            "summary": obj.summary,
            "request_schema": obj.request_schema or {},
            "response_schema": obj.response_schema or {},
        }
        base_cases = generate_cases_for_definition(
            definition, options, auth_definition=auth_definition
        )
        cases = list(base_cases)
        definition_notes: list[str] = []
        if options.get("use_llm", True):
            from app.gen.api.llm_cases import enhance_cases_with_llm

            extra, llm_notes = await enhance_cases_with_llm(
                db, definition, base_cases, options, agent_id=None
            )
            cases.extend(extra)
            definition_notes.extend(llm_notes)
        if not cases and _is_multipart_definition(definition):
            notes.append(
                f"跳过 {definition['method']} {definition['path']}：multipart/form-data "
                "文件上传暂不支持规则生成（需手工编写用例）"
            )
        drafts = await _persist_drafts(db, session_id, definition, cases, seq)
        seq += len(drafts)
        all_drafts.extend(drafts)
        notes.extend(definition_notes)

    total_drafts = len(all_drafts)
    session.test_cases_count = total_drafts
    session.status = "completed"
    session.progress = 100
    session.progress_message = "生成完成"
    await db.commit()

    return {
        "session_id": session_id,
        "total": total_drafts,
        "cases": all_drafts,
        "notes": notes,
    }


@router.post("/generate/{session_id}/import")
async def import_generated_cases(
    session_id: str,
    payload: dict | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """把草案导入为 test_cases（case_kind='api'）。"""
    from app.crud import api_definition as crud_api_defs

    session = await db.get(db_models.GenSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="生成会话不存在")
    _ensure_project_access(user, session.project_id)

    draft_ids = [
        int(i) for i in ((payload or {}).get("draft_ids") or []) if str(i).isdigit()
    ]
    stmt = select(db_models.GenTestCase).where(
        db_models.GenTestCase.session_id == session_id
    )
    if draft_ids:
        stmt = stmt.where(db_models.GenTestCase.id.in_(draft_ids))
    rows = list((await db.execute(stmt.order_by(db_models.GenTestCase.id))).scalars().all())

    created: list[int] = []
    skipped: list[dict] = []
    module_cache: dict[int, int | None] = {}
    for row in rows:
        structured = row.structured_steps if isinstance(row.structured_steps, dict) else {}
        spec = structured.get("api_spec")
        if not spec:
            skipped.append({"draft_id": row.id, "reason": "草案缺少 api_spec"})
            continue
        definition_id = structured.get("definition_id")
        module_id: int | None = None
        if isinstance(definition_id, int):
            if definition_id not in module_cache:
                definition = await crud_api_defs.get_api_definition(db, definition_id)
                module_cache[definition_id] = definition.module_id if definition else None
            module_id = module_cache[definition_id]
        case = db_models.TestCase(
            project_id=session.project_id,
            module_id=module_id,
            project_case_number=await crud_testcase.get_next_project_case_number(
                db, session.project_id
            ),
            name=row.title,
            description=row.expected_result or "",
            priority=row.priority or "medium",
            status="active",
            case_kind="api",
            api_spec=spec,
        )
        db.add(case)
        await db.flush()
        created.append(case.id)

    session.imported_count = int(session.imported_count or 0) + len(created)
    await db.commit()
    return {"created": created, "skipped": skipped}
