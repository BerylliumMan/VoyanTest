# 042 US3：需求条目 / 关联 / 覆盖矩阵
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.auth import get_current_user, log_audit
from app.database import get_async_db
from app.routers.quality.common import ensure_capability
from app.services import quality_requirements as svc

router = APIRouter()


class RequirementCreate(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=300)
    module_id: Optional[int] = None
    description: Optional[str] = None


class RequirementUpdate(BaseModel):
    name: Optional[str] = None
    module_id: Optional[int] = None
    description: Optional[str] = None
    status: Optional[str] = None


class ImportRequest(BaseModel):
    project_id: int
    session_id: str
    module_id: Optional[int] = None


class LinkRequest(BaseModel):
    case_ids: list[int]
    source: str = "manual"


class SuggestRequest(BaseModel):
    project_id: int
    requirement_ids: list[int] = []
    candidate_case_ids: list[int] = []


@router.get("/requirements")
async def list_requirements(project_id: int, include_archived: bool = False,
                            user=Depends(get_current_user),
                            db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, project_id, "traceability")
    rows = await crud_quality().list_requirements(db, project_id, include_archived)
    return {"items": [{
        "id": r.id, "name": r.name, "module_id": r.module_id, "source": r.source,
        "description": r.description, "status": r.status,
    } for r in rows]}


@router.post("/requirements")
async def create_requirement(body: RequirementCreate, user=Depends(get_current_user),
                             db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, body.project_id, "traceability")
    existing = {r.name for r in await crud_quality().list_requirements(db, body.project_id, True)}
    if body.name in existing:
        raise HTTPException(status_code=400, detail="需求名称已存在")
    row = await crud_quality().create_requirement(
        db, project_id=body.project_id, name=body.name, module_id=body.module_id,
        description=body.description, source="manual", created_by=getattr(user, "id", None),
    )
    await log_audit(db, getattr(user, "id", None), "quality.requirement_create",
                    details={"requirement_id": row.id, "project_id": row.project_id})
    return {"id": row.id, "name": row.name}


@router.post("/requirements/import")
async def import_requirements(body: ImportRequest, user=Depends(get_current_user),
                              db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, body.project_id, "traceability")
    session = await db.get(db_models.GenSession, body.session_id)
    if session is None or session.project_id != body.project_id:
        raise HTTPException(status_code=404, detail="生成会话不存在或不属于该项目")
    result = await svc.import_from_session(
        db, project_id=body.project_id, session_id=body.session_id,
        module_id=body.module_id, user_id=getattr(user, "id", None),
    )
    await log_audit(db, getattr(user, "id", None), "quality.requirement_import",
                    details={"project_id": body.project_id, "created": result["created"],
                             "skipped": result["skipped"]})
    return result


@router.patch("/requirements/{requirement_id}")
async def update_requirement(requirement_id: int, body: RequirementUpdate,
                             user=Depends(get_current_user),
                             db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await crud_quality().get_requirement(db, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="需求不存在")
    await ensure_capability(db, user, row.project_id, "traceability")
    row = await crud_quality().update_requirement(
        db, row, name=body.name, module_id=body.module_id,
        description=body.description, status=body.status,
    )
    return {"id": row.id, "name": row.name, "status": row.status}


@router.delete("/requirements/{requirement_id}")
async def delete_requirement(requirement_id: int, user=Depends(get_current_user),
                             db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await crud_quality().get_requirement(db, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="需求不存在")
    await ensure_capability(db, user, row.project_id, "traceability")
    await crud_quality().delete_requirement(db, row)
    return {"deleted": requirement_id}


@router.post("/requirements/{requirement_id}/links")
async def link_cases(requirement_id: int, body: LinkRequest,
                     user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await crud_quality().get_requirement(db, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="需求不存在")
    await ensure_capability(db, user, row.project_id, "traceability")
    count = await svc.link_cases(
        db, requirement_id=requirement_id, case_ids=body.case_ids,
        source=body.source, user_id=getattr(user, "id", None),
    )
    await log_audit(db, getattr(user, "id", None), "quality.requirement_link",
                    details={"requirement_id": requirement_id, "count": count})
    return {"requirement_id": requirement_id, "linked": count}


@router.delete("/requirements/{requirement_id}/links/{case_id}")
async def unlink_case(requirement_id: int, case_id: int,
                      user=Depends(get_current_user),
                      db: AsyncSession = Depends(get_async_db)) -> dict:
    row = await crud_quality().get_requirement(db, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="需求不存在")
    await ensure_capability(db, user, row.project_id, "traceability")
    ok = await crud_quality().unlink_case(db, requirement_id, case_id)
    await log_audit(db, getattr(user, "id", None), "quality.requirement_unlink",
                    details={"requirement_id": requirement_id, "case_id": case_id})
    return {"unlinked": ok}


@router.get("/requirements/matrix")
async def get_matrix(project_id: int, case_kind: Optional[str] = None,
                     user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, project_id, "traceability")
    if case_kind not in (None, "all", "ui", "functional", "api"):
        raise HTTPException(status_code=400, detail="case_kind 非法")
    return await svc.matrix(db, project_id=project_id,
                            case_kind=None if case_kind in (None, "all") else case_kind)


@router.post("/requirements/ai-suggest")
async def ai_suggest(body: SuggestRequest, user=Depends(get_current_user),
                     db: AsyncSession = Depends(get_async_db)) -> dict:
    await ensure_capability(db, user, body.project_id, "traceability")
    from app.gen.model_client import call_model
    reqs = await crud_quality().list_requirements(db, body.project_id)
    if body.requirement_ids:
        reqs = [r for r in reqs if r.id in set(body.requirement_ids)]
    cases_q = await db.execute(
        __import__("sqlalchemy").select(db_models.TestCase).where(
            db_models.TestCase.project_id == body.project_id
        )
    )
    cases = list(cases_q.scalars().all())
    if body.candidate_case_ids:
        cases = [c for c in cases if c.id in set(body.candidate_case_ids)]
    if not reqs or not cases:
        return {"suggestions": [], "ai_available": True}
    payload = [{"id": r.id, "name": r.name} for r in reqs[:50]]
    case_payload = [{"id": c.id, "name": c.name, "kind": c.case_kind or "ui",
                     "module": c.module_id} for c in cases[:100]]
    prompt = (
        "为需求与测试用例推荐关联。只输出 JSON："
        '{"suggestions":[{"requirement_id":1,"case_id":2,"confidence":0.8,"reason":"..."}]}。'
        "不得编造 ID；不确定就不要输出。\n需求：" + json.dumps(payload, ensure_ascii=False)
        + "\n用例：" + json.dumps(case_payload, ensure_ascii=False)
    )
    try:
        raw = await call_model(
            [{"role": "user", "content": prompt}], agent_type="generation",
            max_tokens=1500,
        )
        text = raw if isinstance(raw, str) else str(raw)
        start = text.find("{")
        data = json.loads(text[start:text.rfind("}") + 1]) if start >= 0 else {}
        valid_req = {r.id for r in reqs}
        valid_case = {c.id for c in cases}
        suggestions = [
            s for s in (data.get("suggestions") or [])
            if int(s.get("requirement_id") or 0) in valid_req
            and int(s.get("case_id") or 0) in valid_case
        ]
        return {"suggestions": suggestions, "ai_available": True}
    except Exception as exc:  # noqa: BLE001
        return {"suggestions": [], "ai_available": False, "error": str(exc)[:300]}


def crud_quality():
    from app.crud import quality
    return quality
