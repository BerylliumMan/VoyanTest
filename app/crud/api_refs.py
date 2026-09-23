"""接口定义 / 用例被场景引用的查询，以及定义变更同步到用例请求结构。"""
from __future__ import annotations

import json
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models


async def scenario_names(
    db: AsyncSession, project_id: int, predicate: Callable[[dict], bool]
) -> list[str]:
    result = await db.execute(
        select(db_models.ApiScenario).where(db_models.ApiScenario.project_id == project_id)
    )
    names: list[str] = []
    for scenario in result.scalars().all():
        for step in scenario.steps or []:
            if isinstance(step, dict) and predicate(step):
                names.append(scenario.name)
                break
    return names


def _step_type(step: dict) -> str:
    if step.get("type"):
        return str(step["type"])
    if step.get("case_id") is not None:
        return "case"
    return ""


async def scenarios_using_definition(
    db: AsyncSession, project_id: int, definition_id: int, case_ids: set[int]
) -> list[str]:
    def hit(step: dict) -> bool:
        kind = _step_type(step)
        if kind == "definition" and int(step.get("definition_id") or 0) == definition_id:
            return True
        if kind == "case" and int(step.get("case_id") or 0) in case_ids:
            return True
        return False

    return await scenario_names(db, project_id, hit)


async def scenarios_using_case(db: AsyncSession, project_id: int, case_id: int) -> list[str]:
    def hit(step: dict) -> bool:
        return _step_type(step) == "case" and int(step.get("case_id") or 0) == case_id

    return await scenario_names(db, project_id, hit)


def apply_definition_to_spec(spec: dict, definition, old_path: str | None = None) -> dict:
    """把定义上的 method、path、query、body 结构写入用例第一步。不改断言和提取。"""
    spec = dict(spec or {})
    steps = [dict(s) if isinstance(s, dict) else s for s in (spec.get("steps") or [])]
    if not steps or not isinstance(steps[0], dict):
        return spec
    step = dict(steps[0])
    request = dict(step.get("request") or {})
    request["method"] = definition.method
    new_path = definition.path or "/"
    url = str(request.get("url") or "")
    if old_path and old_path != new_path and old_path in url:
        url = url.replace(old_path, new_path, 1)
    elif not url or url.startswith("{{baseUrl}}"):
        url = "{{baseUrl}}" + new_path
    request["url"] = url

    schema = definition.request_schema or {}
    params = [
        p for p in (schema.get("params") or [])
        if isinstance(p, dict) and str(p.get("in") or "") == "query"
    ]
    existing = {
        str(item.get("key")): item
        for item in (request.get("query") or [])
        if isinstance(item, dict) and item.get("key")
    }
    query: list[dict] = []
    for param in params:
        name = str(param.get("name") or "").strip()
        if not name:
            continue
        prev = existing.get(name)
        if prev:
            query.append(prev)
        else:
            query.append({
                "key": name,
                "value": "" if param.get("example") is None else str(param.get("example")),
                "enable": True,
            })
    request["query"] = query

    body_schema = schema.get("body") if isinstance(schema.get("body"), dict) else {}
    content_type = str(body_schema.get("content_type") or "")
    example = body_schema.get("example")
    body = dict(request.get("body") or {})
    if "json" in content_type.lower():
        body["type"] = "json"
        if not str(body.get("content") or "").strip() and example is not None:
            body["content"] = example if isinstance(example, str) else json.dumps(example, ensure_ascii=False)
    elif content_type and not str(body.get("content") or "").strip() and example is not None:
        body["type"] = body.get("type") or "raw"
        body["content"] = example if isinstance(example, str) else json.dumps(example, ensure_ascii=False)
    request["body"] = body
    step["request"] = request
    steps[0] = step
    spec["steps"] = steps
    return spec
