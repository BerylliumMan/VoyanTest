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


def normalize_scenario_steps(items: Any) -> list[dict]:
    """场景步骤：[{case_id, enabled, order}]；非法行丢弃，同 case_id 取首个。"""
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    seen: set[int] = set()
    order = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            case_id = int(it.get("case_id"))
        except (TypeError, ValueError):
            continue
        if case_id <= 0 or case_id in seen:
            continue
        seen.add(case_id)
        order += 1
        out.append(
            {
                "case_id": case_id,
                "enabled": bool(it.get("enabled", True)),
                "order": int(it.get("order") or order),
            }
        )
    out.sort(key=lambda s: (s["order"], s["case_id"]))
    return out


async def validate_scenario_cases(db: AsyncSession, project_id: int, steps: list[dict]) -> None:
    """每一步引用的用例必须存在、同项目且 case_kind='api'，否则 400。"""
    for step in steps:
        case_id = step["case_id"]
        tc = await db.get(db_models.TestCase, case_id)
        if tc is None:
            raise ScenarioValidationError(f"步骤引用的用例不存在：case_id={case_id}")
        if tc.project_id != project_id:
            raise ScenarioValidationError(f"用例 {case_id} 不属于本项目")
        if getattr(tc, "case_kind", None) != "api":
            raise ScenarioValidationError(f"用例 {case_id} 不是接口用例（case_kind={getattr(tc, 'case_kind', None)!r}）")


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
    await validate_scenario_cases(db, project_id, steps)
    obj = db_models.ApiScenario(
        project_id=project_id,
        name=name,
        description=data.get("description"),
        environment_id=data.get("environment_id"),
        variables=normalize_scenario_variables(data.get("variables")),
        steps=steps,
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
    if "steps" in data:
        steps = normalize_scenario_steps(data.get("steps"))
        await validate_scenario_cases(db, obj.project_id, steps)
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
