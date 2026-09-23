# app/crud/api_scenario.py — 接口测试场景 CRUD（029-api-testing 场景页）
#
# 场景 = 有序的接口用例集合 + 执行环境 + 场景级变量覆盖。
# 保存时校验每一步引用的用例（存在 + 同项目 + case_kind='api'），
# 执行时按 enabled 顺序逐条经 run_api_case_server 跑在场景环境上。
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)

DEFAULT_SCENARIO_NAME = "未命名场景"


class ScenarioValidationError(ValueError):
    """场景字段不可恢复问题（路由层转 400）。"""


def normalize_scenario_variables(items: Any) -> list[dict]:
    """场景级变量覆盖：[{key,value,secret,enable}]，无 key 的行丢弃。"""
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        out.append(
            {
                "key": key,
                "value": it.get("value"),
                "secret": bool(it.get("secret", False)),
                "enable": bool(it.get("enable", True)),
            }
        )
    return out


_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _step_id(raw: Any, index: int) -> str:
    text = str(raw or "").strip()
    return text or f"s{index}"


def _copy_request_step(it: dict, *, step_type: str, index: int) -> dict:
    request = it.get("request") if isinstance(it.get("request"), dict) else {}
    method = str(request.get("method") or "").strip().upper()
    url = str(request.get("url") or "").strip()
    if method not in _HTTP_METHODS:
        raise ScenarioValidationError(f"第 {index} 步请求方法无效")
    if not url:
        raise ScenarioValidationError(f"第 {index} 步请求 URL 不能为空")
    return {
        "id": _step_id(it.get("id"), index),
        "type": step_type,
        "enabled": bool(it.get("enabled", True)),
        "order": index,
        "name": str(it.get("name") or "").strip() or ("自定义请求" if step_type == "request" else "接口请求"),
        "request": request,
        "assertions": it.get("assertions") if isinstance(it.get("assertions"), list) else [],
        "extractors": it.get("extractors") if isinstance(it.get("extractors"), list) else [],
        "pre": it.get("pre") if isinstance(it.get("pre"), list) else [],
        "post": it.get("post") if isinstance(it.get("post"), list) else [],
    }


def _normalize_step(it: dict, order: int) -> dict:
    raw_type = str(it.get("type") or "").strip()
    if not raw_type and it.get("case_id") is not None:
        raw_type = "case"
    if raw_type == "case":
        try:
            case_id = int(it.get("case_id"))
        except (TypeError, ValueError):
            raise ScenarioValidationError(f"第 {order} 步缺少有效的 case_id") from None
        if case_id <= 0:
            raise ScenarioValidationError(f"第 {order} 步 case_id 无效")
        return {
            "id": _step_id(it.get("id"), order),
            "type": "case",
            "case_id": case_id,
            "enabled": bool(it.get("enabled", True)),
            "order": int(it.get("order") or order),
        }
    if raw_type == "definition":
        try:
            definition_id = int(it.get("definition_id"))
        except (TypeError, ValueError):
            raise ScenarioValidationError(f"第 {order} 步缺少有效的 definition_id") from None
        if definition_id <= 0:
            raise ScenarioValidationError(f"第 {order} 步 definition_id 无效")
        step = _copy_request_step(it, step_type="definition", index=order)
        step["definition_id"] = definition_id
        step["order"] = int(it.get("order") or order)
        return step
    if raw_type == "request":
        step = _copy_request_step(it, step_type="request", index=order)
        step["order"] = int(it.get("order") or order)
        return step
    if raw_type == "wait":
        try:
            ms = int(it.get("ms") or 0)
        except (TypeError, ValueError):
            raise ScenarioValidationError(f"第 {order} 步等待时间无效") from None
        if ms < 0:
            raise ScenarioValidationError(f"第 {order} 步等待时间不能为负")
        return {
            "id": _step_id(it.get("id"), order),
            "type": "wait",
            "name": str(it.get("name") or "").strip() or "等待",
            "enabled": bool(it.get("enabled", True)),
            "order": int(it.get("order") or order),
            "ms": min(ms, 60000),
            "children": [],
        }
    if raw_type in ("loop", "foreach", "if"):
        children = normalize_scenario_steps(it.get("children") or [])
        base = {
            "id": _step_id(it.get("id"), order),
            "type": raw_type,
            "name": str(it.get("name") or "").strip() or {"loop": "循环", "foreach": "遍历", "if": "条件"}[raw_type],
            "enabled": bool(it.get("enabled", True)),
            "order": int(it.get("order") or order),
            "children": children,
        }
        if raw_type == "loop":
            try:
                count = int(it.get("count") or 1)
            except (TypeError, ValueError):
                raise ScenarioValidationError(f"第 {order} 步循环次数无效") from None
            if count < 1 or count > 100:
                raise ScenarioValidationError(f"第 {order} 步循环次数须在 1 到 100 之间")
            base["count"] = count
        elif raw_type == "foreach":
            try:
                dataset_id = int(it.get("dataset_id"))
            except (TypeError, ValueError):
                raise ScenarioValidationError(f"第 {order} 步缺少数据集") from None
            base["dataset_id"] = dataset_id
        else:
            variable = str(it.get("variable") or "").strip()
            operator = str(it.get("operator") or "equals")
            if not variable:
                raise ScenarioValidationError(f"第 {order} 步条件缺少变量名")
            if operator not in ("equals", "contains"):
                raise ScenarioValidationError(f"第 {order} 步条件只支持 equals 或 contains")
            base["variable"] = variable
            base["operator"] = operator
            base["expected"] = "" if it.get("expected") is None else str(it.get("expected"))
        return base
    raise ScenarioValidationError(f"第 {order} 步类型无效")


def normalize_scenario_steps(items: Any) -> list[dict]:
    """场景步骤。旧数据只有 case_id 时视为 type=case。

    容器步骤 wait / loop / foreach / if 的子步骤放在 children。
    wait 最长 60 秒，loop 次数 1–100。
    """
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    order = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        order += 1
        out.append(_normalize_step(it, order))
    out.sort(key=lambda s: (s.get("order") or 0, s.get("id") or ""))
    for i, step in enumerate(out, start=1):
        step["order"] = i
    return out


async def validate_scenario_steps(db: AsyncSession, project_id: int, steps: list[dict]) -> None:
    """case / definition / 数据集引用必须属于同一项目。子步骤同样校验。"""
    for step in steps:
        kind = step.get("type")
        if kind == "case":
            case_id = step["case_id"]
            tc = await db.get(db_models.TestCase, case_id)
            if tc is None:
                raise ScenarioValidationError(f"步骤引用的用例不存在：case_id={case_id}")
            if tc.project_id != project_id:
                raise ScenarioValidationError(f"用例 {case_id} 不属于本项目")
            if getattr(tc, "case_kind", None) != "api":
                raise ScenarioValidationError(
                    f"用例 {case_id} 不是接口用例（case_kind={getattr(tc, 'case_kind', None)!r}）"
                )
        elif kind == "definition":
            definition_id = step["definition_id"]
            definition = await db.get(db_models.ApiDefinition, definition_id)
            if definition is None:
                raise ScenarioValidationError(f"步骤引用的接口不存在：definition_id={definition_id}")
            if definition.project_id != project_id:
                raise ScenarioValidationError(f"接口 {definition_id} 不属于本项目")
        elif kind == "foreach":
            dataset = await db.get(db_models.ApiDataset, step["dataset_id"])
            if dataset is None or dataset.project_id != project_id:
                raise ScenarioValidationError(f"数据集 {step['dataset_id']} 不存在或不属于本项目")
        children = step.get("children") or []
        if children:
            await validate_scenario_steps(db, project_id, children)


async def list_scenarios(
    db: AsyncSession, project_id: int, *, limit: int = 200, offset: int = 0
) -> list[db_models.ApiScenario]:
    """列出项目内场景（新的在前）。"""
    stmt = (
        select(db_models.ApiScenario)
        .where(db_models.ApiScenario.project_id == project_id)
        .order_by(db_models.ApiScenario.id.desc())
        .limit(max(1, min(limit, 1000)))
        .offset(max(0, offset))
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_scenario(db: AsyncSession, scenario_id: int) -> Optional[db_models.ApiScenario]:
    result = await db.execute(
        select(db_models.ApiScenario).where(db_models.ApiScenario.id == scenario_id)
    )
    return result.scalar_one_or_none()


async def create_scenario(db: AsyncSession, data: dict) -> db_models.ApiScenario:
    """创建场景；steps 归一化后逐条校验用例引用。"""
    name = str(data.get("name") or "").strip() or DEFAULT_SCENARIO_NAME
    steps = normalize_scenario_steps(data.get("steps"))
    project_id = int(data["project_id"])
    await validate_scenario_steps(db, project_id, steps)
    obj = db_models.ApiScenario(
        project_id=project_id,
        name=name,
        description=data.get("description"),
        environment_id=data.get("environment_id"),
        variables=normalize_scenario_variables(data.get("variables")),
        steps=steps,
        share_cookie=bool(data.get("share_cookie", False)),
        continue_on_failure=bool(data.get("continue_on_failure", False)),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update_scenario(
    db: AsyncSession, scenario_id: int, data: dict
) -> Optional[db_models.ApiScenario]:
    """局部更新场景；提供 steps 时重新归一化并校验用例引用。"""
    obj = await get_scenario(db, scenario_id)
    if obj is None:
        return None
    if "name" in data:
        obj.name = str(data.get("name") or "").strip() or obj.name
    if "description" in data:
        obj.description = data.get("description")
    if "environment_id" in data:
        obj.environment_id = data.get("environment_id")
    if "variables" in data:
        obj.variables = normalize_scenario_variables(data.get("variables"))
    if "share_cookie" in data:
        obj.share_cookie = bool(data.get("share_cookie"))
    if "continue_on_failure" in data:
        obj.continue_on_failure = bool(data.get("continue_on_failure"))
    if "steps" in data:
        steps = normalize_scenario_steps(data.get("steps"))
        await validate_scenario_steps(db, obj.project_id, steps)
        obj.steps = steps
    await db.commit()
    await db.refresh(obj)
    return obj


async def delete_scenario(db: AsyncSession, scenario_id: int) -> bool:
    obj = await get_scenario(db, scenario_id)
    if obj is None:
        return False
    await db.delete(obj)
    await db.commit()
    return True
