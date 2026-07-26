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
    """
    if not text or not str(text).strip():
        return []
    text = str(text).strip()

    def _clean(items: list[str]) -> list[str]:
        return [re.sub(r"\s+", " ", m).strip() for m in items if m.strip()]

    # 1. foo\n2. bar — need >=2 hits; a single hit usually means \Z swallowed inline numbers
    if "\n" in text:
        matches = re.findall(
            r"(?:^|\n)\s*\d+[\.、]\s+(.+?)(?=\n\s*\d+[\.、]\s+|\Z)",
            text,
            re.S,
        )
        if len(matches) >= 2:
            return _clean(matches)
    # 1. foo 2. bar  (space required after marker; avoids splitting "2.0")
    matches = re.findall(
        r"(?:^|\s)\d+[\.、]\s+(.+?)(?=\s+\d+[\.、]\s+|\Z)",
        text,
        re.S,
    )
    if matches:
        return _clean(matches)
    if "\n" in text:
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    return [text]


def _split_numbered_steps(text: str) -> list[str]:
    return split_numbered_items(text)


def _split_expected_results(text: str) -> list[str]:
    return split_numbered_items(text)


def align_expected_to_steps(steps: list[str], results: list[str]) -> list[str]:
    """Align expected results to steps: trim extras, right-align when shorter."""
    if not steps:
        return []
    if len(results) > len(steps):
        base = results[: len(steps) - 1] if len(steps) > 1 else []
        extras = results[len(steps) - 1 :]
        return base + ["；".join(extras)]
    if len(results) < len(steps):
        # 预期更少时右对齐：常见于模型只给最后几步写断言
        return [""] * (len(steps) - len(results)) + list(results)
    return list(results)


# Backward-compatible alias
_align_expected_to_steps = align_expected_to_steps


async def _find_or_create_module(db: AsyncSession, project_id: int, module_name: str) -> db_models.Module:
    """Find existing module by name in project, or create it."""
    result = await db.execute(
        select(db_models.Module).where(
            db_models.Module.project_id == project_id,
            db_models.Module.name == module_name,
        )
    )
    module = result.scalar_one_or_none()
    if not module:
        module = db_models.Module(project_id=project_id, name=module_name)
        db.add(module)
        await db.flush()
        logger.info("Created module: %s (id=%d)", module_name, module.id)
    return module


async def import_test_cases(
    db: AsyncSession,
    project_id: int,
    test_cases: list,  # list of gen.models.TestCase (gentestcases format)
    selected_ids: list[str] | None = None,  # list of test_case_id strings to import, None = all
) -> list[db_models.TestCase]:
    """Import gentestcases test cases into uitest-work DB.

    Args:
        db: SQLAlchemy session
        project_id: Target project ID
        test_cases: List of gentestcases TestCase dataclass instances
        selected_ids: Optional list of test_case_id strings to import. If None, import all.

    Returns:
        List of created uitest-work TestCase ORM objects.
    """
    created = []
    selected_set = set(selected_ids) if selected_ids else None

    for gen_tc in test_cases:
        # Skip if not selected
        if selected_set is not None and gen_tc.test_case_id not in selected_set:
            continue

        # Find or create module
        module_name = gen_tc.module.strip() if gen_tc.module else "通用"
        module = await _find_or_create_module(db, project_id, module_name)

        # Build description from preconditions
        description = ""
        if gen_tc.preconditions:
            description = f"前置条件：{gen_tc.preconditions}"

        # Map priority
        priority = PRIORITY_MAP.get(gen_tc.priority.strip(), "medium")

        # Create uitest-work TestCase
        project_case_number = await get_next_project_case_number(db, project_id)
        tc = db_models.TestCase(
            project_id=project_id,
            module_id=module.id,
            project_case_number=project_case_number,
            name=gen_tc.title.strip() if gen_tc.title else f"Test Case {gen_tc.test_case_id}",
            description=description,
            priority=priority,
        )
        db.add(tc)
        await db.flush()

        # Split test_steps and expected_results into TestStep records
        steps_text = _split_numbered_steps(gen_tc.test_steps)
        results_text = _align_expected_to_steps(
            steps_text, _split_expected_results(gen_tc.expected_result)
        )
        created_steps = []
        for idx, step_text in enumerate(steps_text, start=1):
            step = db_models.TestStep(
                case_id=tc.id,
                step_order=idx,
                description=step_text,
                parsed_result=results_text[idx - 1],
            )
            db.add(step)
            created_steps.append(step)

        created.append(tc)
        logger.info("Imported %s → TestCase id=%d (%s)", gen_tc.test_case_id, tc.id, tc.name)

    await db.commit()
    return created
