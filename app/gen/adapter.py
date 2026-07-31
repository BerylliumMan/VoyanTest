"""Adapter: convert gentestcases analysis results to uitest-work DB models."""
import re
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app import db_models
from app.crud.testcase import get_next_project_case_number

logger = logging.getLogger(__name__)

# Priority mapping: Chinese → English
PRIORITY_MAP = {"高": "high", "中": "medium", "低": "low"}


def split_numbered_items(text: str) -> list[str]:
    """Split numbered step/expected text without breaking on values like ``2.0``.

    Prefers newline-delimited ``1. xxx`` items; falls back to inline items that
    require whitespace after the numbering marker (so ``版本2.0`` stays intact).

    Bare markers (``1.\\n2.\\n3.``) or collapsed ``1.2.3.4.`` are treated as empty
    so they are not shown as fake expected text.
    """
    if not text or not str(text).strip():
        return []
    text = str(text).strip()

    # Collapsed junk like "1.2.3.4.5." or "1. 2. 3. 4." — not real content
    if re.fullmatch(r"(?:\d+[\.、]\s*)+", text):
        return []

    def _clean(items: list[str]) -> list[str]:
        return [re.sub(r"\s+", " ", m).strip() for m in items if m.strip()]

    # Newline form: use explicit indices when present (``3. xxx`` → slot 3)
    if "\n" in text:
        indexed_map: dict[int, str] = {}
        sequential: list[str] = []
        saw_marker = False
        for ln in text.splitlines():
            m = re.match(r"^\s*(\d+)[\.、]\s*(.*)$", ln)
            if m:
                saw_marker = True
                idx = int(m.group(1))
                body = (m.group(2) or "").strip()
                indexed_map[idx] = body
                sequential.append(body)
            elif ln.strip():
                sequential.append(ln.strip())
        if saw_marker and indexed_map:
            # Bare "1.\n2.\n3." → all empty bodies
            if not any(v for v in indexed_map.values()):
                return []
            max_i = max(indexed_map)
            if max_i <= len(indexed_map) + 5 and max_i <= 200:
                return [indexed_map.get(i, "") for i in range(1, max_i + 1)]
            return sequential

        matches = re.findall(
            r"(?:^|\n)\s*\d+[\.、]\s*(.+?)(?=\n\s*\d+[\.、]\s*|\Z)",
            text,
            re.S,
        )
        if len(matches) >= 2:
            return _clean(matches)
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Inline: 1.foo 2.bar  (optional space after marker; next item must be spaced)
    matches = re.findall(
        r"(?:^|\s)\d+[\.、]\s*(.+?)(?=\s+\d+[\.、]\s*|\Z)",
        text,
        re.S,
    )
    if matches:
        cleaned = _clean(matches)
        # Single match that is itself only numbering junk
        if len(cleaned) == 1 and re.fullmatch(r"(?:\d+[\.、]\s*)+", cleaned[0]):
            return []
        return cleaned
    return [text]


def _split_numbered_steps(text: str) -> list[str]:
    return split_numbered_items(text)


def _split_expected_results(text: str) -> list[str]:
    return split_numbered_items(text)


def align_expected_to_steps(steps: list[str], results: list[str]) -> list[str]:
    """Align expected results to steps: merge extras; right-pad shorter with empty."""
    if not steps:
        return []
    if len(results) > len(steps):
        base = results[: len(steps) - 1] if len(steps) > 1 else []
        extras = results[len(steps) - 1 :]
        return base + ["；".join(extras)]
    if len(results) < len(steps):
        # Right-align: sparse expected usually applies to later steps; do not invent text.
        return [""] * (len(steps) - len(results)) + list(results)
    return list(results)


# Backward-compatible alias
_align_expected_to_steps = align_expected_to_steps


from app.gen.chunking import normalize_module_path, split_module_path


async def _find_or_create_module(
    db: AsyncSession,
    project_id: int,
    module_name: str,
    parent_id: int | None = None,
) -> db_models.Module:
    """Find existing module by name + parent in project, or create it."""
    name = (module_name or "").strip() or "通用"
    q = select(db_models.Module).where(
        db_models.Module.project_id == project_id,
        db_models.Module.name == name,
    )
    if parent_id is None:
        q = q.where(db_models.Module.parent_id.is_(None))
    else:
        q = q.where(db_models.Module.parent_id == parent_id)
    result = await db.execute(q)
    module = result.scalar_one_or_none()
    if not module:
        module = db_models.Module(
            project_id=project_id,
            name=name,
            parent_id=parent_id,
        )
        db.add(module)
        await db.flush()
        logger.info(
            "Created module: %s (id=%d, parent_id=%s)",
            name, module.id, parent_id,
        )
    return module


async def resolve_module_for_import(
    db: AsyncSession,
    project_id: int,
    module_path: str,
    parent_module_id: int | None = None,
) -> db_models.Module:
    """Resolve ``一级`` / ``一级——二级`` into the leaf Module (create parents as needed).

    When ``parent_module_id`` is set, the primary segment is created under that module.
    """
    primary, secondary = split_module_path(module_path)
    l1 = await _find_or_create_module(
        db, project_id, primary, parent_id=parent_module_id,
    )
    if not secondary:
        return l1
    return await _find_or_create_module(db, project_id, secondary, parent_id=l1.id)


async def import_test_cases(
    db: AsyncSession,
    project_id: int,
    test_cases: list,  # list of gen.models.TestCase (gentestcases format)
    selected_ids: list[str] | None = None,  # list of test_case_id strings to import, None = all
    parent_module_id: int | None = None,
    case_kind: str = "ui",
) -> tuple[list[db_models.TestCase], int]:
    """Import gentestcases test cases into uitest-work DB.

    Returns:
        (created_orm_list, skipped_count) — skips when same project+module+title exists.
    """
    created = []
    skipped = 0
    selected_set = set(selected_ids) if selected_ids else None
    if case_kind not in ("functional", "ui"):
        case_kind = "ui"

    for gen_tc in test_cases:
        if selected_set is not None and gen_tc.test_case_id not in selected_set:
            continue

        module_name = normalize_module_path(
            gen_tc.module.strip() if gen_tc.module else "通用"
        )
        module = await resolve_module_for_import(
            db, project_id, module_name, parent_module_id=parent_module_id,
        )

        title = gen_tc.title.strip() if gen_tc.title else f"Test Case {gen_tc.test_case_id}"
        existing = await db.execute(
            select(db_models.TestCase).where(
                db_models.TestCase.project_id == project_id,
                db_models.TestCase.module_id == module.id,
                db_models.TestCase.name == title,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            logger.info("Skip duplicate import: %s / %s", module_name, title)
            continue

        description = ""
        if gen_tc.preconditions:
            description = f"前置条件：{gen_tc.preconditions}"

        priority = PRIORITY_MAP.get((gen_tc.priority or "").strip(), "medium")

        project_case_number = await get_next_project_case_number(db, project_id)
        tc = db_models.TestCase(
            project_id=project_id,
            module_id=module.id,
            project_case_number=project_case_number,
            name=title,
            description=description,
            priority=priority,
            case_kind=case_kind,
        )
        db.add(tc)
        await db.flush()

        steps_text = _split_numbered_steps(gen_tc.test_steps)
        results_text = _align_expected_to_steps(
            steps_text, _split_expected_results(gen_tc.expected_result)
        )
        for idx, step_text in enumerate(steps_text, start=1):
            step = db_models.TestStep(
                case_id=tc.id,
                step_order=idx,
                description=step_text,
                parsed_result=results_text[idx - 1],
            )
            db.add(step)

        created.append(tc)
        logger.info("Imported %s → TestCase id=%d (%s)", gen_tc.test_case_id, tc.id, tc.name)

    await db.commit()
    return created, skipped
