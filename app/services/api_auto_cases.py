"""047：变更即补用例。

对"受影响但没有任何用例"的接口，用规则生成器补出基础用例并直接入库，
由变更计划统一纳入本次回归。只新增、不改动人工用例；每个接口只补一次。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.api_test import ApiDefinition
from app.models.testcase import TestCase
from app.tz import now as tz_now

logger = logging.getLogger(__name__)

AUTO_NAME_PREFIX = "[自动] "


@dataclass
class AutoCaseStats:
    generated_definitions: int = 0
    generated_cases: int = 0
    skipped_definition_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "generated_definitions": self.generated_definitions,
            "generated_cases": self.generated_cases,
            "skipped": len(self.skipped_definition_ids),
            "errors": self.errors[:10],
        }


async def auto_generate_missing_cases(
    db: AsyncSession,
    *,
    project_id: int,
    definition_ids: list[int],
    trigger_id: int | None = None,
    now: datetime | None = None,
) -> AutoCaseStats:
    """为给定接口补基础用例；返回本次生成统计。"""
    settings = get_settings()
    stats = AutoCaseStats()
    if not settings.api_auto_case_enabled or not definition_ids:
        return stats

    now = now or tz_now()
    max_defs = max(0, int(settings.api_auto_case_max_definitions))
    max_each = max(1, int(settings.api_auto_case_max_each))
    daily_limit = max(0, int(settings.api_auto_case_daily_limit))
    budget = daily_limit - await _auto_cases_created_today(db, project_id, now)

    processed = 0
    seen: set[int] = set()
    for raw_id in definition_ids:
        try:
            definition_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if definition_id in seen:
            continue
        seen.add(definition_id)

        if processed >= max_defs or budget <= 0:
            stats.skipped_definition_ids.append(definition_id)
            continue

        definition = await db.get(ApiDefinition, definition_id)
        if (
            definition is None
            or int(definition.project_id) != int(project_id)
            or (definition.status or "active") != "active"
            or definition.auto_case_generated_at is not None
        ):
            stats.skipped_definition_ids.append(definition_id)
            continue

        drafts = _generate_drafts(definition, max_each, stats)
        if not drafts:
            stats.skipped_definition_ids.append(definition_id)
            continue

        created = 0
        for draft in drafts[:budget]:
            try:
                case = await _build_case(db, definition, draft, trigger_id=trigger_id, now=now)
                db.add(case)
                await db.flush()
                created += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其他用例
                logger.warning("自动用例写入失败 definition=%s: %s", definition_id, exc)
                stats.errors.append(f"definition {definition_id}: {exc}")

        if created:
            definition.auto_case_generated_at = now
            stats.generated_definitions += 1
            stats.generated_cases += created
            budget -= created
            processed += 1
        else:
            stats.skipped_definition_ids.append(definition_id)

    await db.flush()
    return stats


def _generate_drafts(
    definition: ApiDefinition, max_each: int, stats: AutoCaseStats
) -> list[dict]:
    """调用规则生成器；异常记录到 stats 并返回空列表。"""
    try:
        from app.gen.api.generator import generate_cases_for_definition

        payload = {
            "id": int(definition.id),
            "name": definition.name,
            "method": definition.method,
            "path": definition.path,
            "summary": definition.summary,
            "request_schema": definition.request_schema or {},
            "response_schema": definition.response_schema or {},
        }
        return list(
            generate_cases_for_definition(
                payload,
                {
                    "normal": True,
                    "missing_required": True,
                    "boundary": True,
                    "max_cases_per_operation": max_each,
                    "skip_multipart": True,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 - 生成失败不阻塞计划
        logger.warning("自动用例生成失败 definition=%s: %s", definition.id, exc)
        stats.errors.append(f"definition {definition.id}: {exc}")
        return []


async def _build_case(
    db: AsyncSession,
    definition: ApiDefinition,
    draft: dict,
    *,
    trigger_id: int | None,
    now: datetime,
) -> TestCase:
    from app.crud.testcase import get_next_project_case_number

    spec = dict(draft.get("api_spec") or {})
    meta = dict(spec.get("meta") or {})
    meta.update(
        {
            "auto_generated": True,
            "auto_source": "change_trigger",
            "auto_definition_id": int(definition.id),
            "auto_trigger_id": trigger_id,
            "generated_at": now.isoformat(),
        }
    )
    spec["meta"] = meta
    name = f"{AUTO_NAME_PREFIX}{draft.get('name') or f'{definition.method} {definition.path}'}"
    return TestCase(
        project_id=int(definition.project_id),
        module_id=definition.module_id,
        project_case_number=await get_next_project_case_number(
            db, int(definition.project_id)
        ),
        name=name[:255],
        description="；".join(draft.get("notes") or [])[:2000],
        priority=draft.get("priority") or "medium",
        status="active",
        case_kind="api",
        api_definition_id=int(definition.id),
        api_spec=spec,
    )


async def _auto_cases_created_today(
    db: AsyncSession, project_id: int, now: datetime
) -> int:
    """统计当日已自动生成的用例数（JSON 字段在 Python 侧过滤，兼容 SQLite/PG）。"""
    rows = (
        await db.execute(
            select(TestCase.api_spec, TestCase.created_at).where(
                TestCase.project_id == project_id,
                TestCase.case_kind == "api",
            )
        )
    ).all()
    today = now.date()
    count = 0
    for spec, created_at in rows:
        if not isinstance(spec, dict):
            continue
        meta = spec.get("meta") or {}
        if not isinstance(meta, dict) or not meta.get("auto_generated"):
            continue
        if created_at is not None and created_at.date() != today:
            continue
        count += 1
    return count
