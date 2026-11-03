"""046：代码变更 → 受影响接口 → 关联用例。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_test import ApiDefinition
from app.models.testcase import TestCase

logger = logging.getLogger(__name__)


@dataclass
class AffectedApi:
    definition_id: int
    method: str
    path: str
    case_ids: list[int] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "definition_id": self.definition_id,
            "method": self.method,
            "path": self.path,
            "case_ids": list(self.case_ids),
            "changed_files": list(self.changed_files),
        }


def normalize_repo_path(path: str | None) -> str:
    """统一为仓库相对路径（去 ./、前导 /、反斜杠）。"""
    if not path:
        return ""
    p = str(path).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def match_definition_ids(
    changed_files: list[str],
    definitions: list[tuple[int, str | None]],
) -> dict[int, list[str]]:
    """返回 {definition_id: [命中的变更文件]}。"""
    changed = {normalize_repo_path(f): f for f in changed_files if f}
    hits: dict[int, list[str]] = {}
    for definition_id, source_file in definitions:
        key = normalize_repo_path(source_file)
        if key and key in changed:
            hits.setdefault(int(definition_id), []).append(changed[key])
    return hits


def _case_definition_ids(case: TestCase) -> set[int]:
    """用例关联的接口定义：优先外键，兼容 api_spec 快照。"""
    ids: set[int] = set()
    direct = getattr(case, "api_definition_id", None)
    if isinstance(direct, int):
        ids.add(direct)
    spec = getattr(case, "api_spec", None)
    if isinstance(spec, dict):
        one = spec.get("definition_id")
        if isinstance(one, int):
            ids.add(one)
        many = spec.get("definition_ids")
        if isinstance(many, list):
            ids.update(x for x in many if isinstance(x, int))
    return ids


async def compute_impact(
    db: AsyncSession,
    *,
    project_id: int,
    changed_files: list[str],
    include_without_cases: bool = False,
) -> list[AffectedApi]:
    """计算受影响的接口及其关联用例；没有关联用例的接口不返回。"""
    if not changed_files:
        return []

    rows = (
        await db.execute(
            select(ApiDefinition).where(
                ApiDefinition.project_id == project_id,
                ApiDefinition.status == "active",
            )
        )
    ).scalars().all()
    hits = match_definition_ids(
        changed_files, [(r.id, getattr(r, "source_file", None)) for r in rows]
    )
    if not hits:
        return []

    by_id = {int(r.id): r for r in rows}
    affected: dict[int, AffectedApi] = {
        did: AffectedApi(
            definition_id=did,
            method=(by_id[did].method or "").upper(),
            path=by_id[did].path,
            changed_files=files,
        )
        for did, files in hits.items()
    }

    cases = (
        await db.execute(
            select(TestCase).where(
                TestCase.project_id == project_id,
                TestCase.case_kind == "api",
                TestCase.status == "active",
            )
        )
    ).scalars().all()
    for case in cases:
        linked = _case_definition_ids(case) & hits.keys()
        for did in linked:
            affected[did].case_ids.append(int(case.id))

    for item in affected.values():
        item.case_ids = sorted(set(item.case_ids))
    items = list(affected.values())
    if include_without_cases:
        return items
    return [a for a in items if a.case_ids]
