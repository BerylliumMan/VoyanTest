"""Adapter: convert gentestcases analysis results to uitest-work DB models."""
import re
import logging
from sqlalchemy.orm import Session
from app import db_models
from app.crud.testcase import get_next_project_case_number

logger = logging.getLogger(__name__)

# Priority mapping: Chinese → English
PRIORITY_MAP = {"高": "high", "中": "medium", "低": "low"}


def _split_numbered_steps(text: str) -> list[str]:
    """Split text like '1. step1 2. step2' or '1. step1\\n2. step2' into a list of steps."""
    if not text or not text.strip():
        return []
    # Match patterns like "1. xxx", "2. xxx" etc. - works with or without newlines
    parts = re.split(r'\d+\.\s*', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _split_expected_results(text: str) -> list[str]:
    """Split expected results text like '1. result1 2. result2' into a list."""
    if not text or not text.strip():
        return []
    parts = re.split(r'\d+\.\s*', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _find_or_create_module(db: Session, project_id: int, module_name: str) -> db_models.Module:
    """Find existing module by name in project, or create it."""
    module = db.query(db_models.Module).filter(
        db_models.Module.project_id == project_id,
        db_models.Module.name == module_name,
    ).first()
    if not module:
        module = db_models.Module(project_id=project_id, name=module_name)
        db.add(module)
        db.flush()
        logger.info("Created module: %s (id=%d)", module_name, module.id)
    return module


def import_test_cases(
    db: Session,
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
        module = _find_or_create_module(db, project_id, module_name)

        # Build description from preconditions
        description = ""
        if gen_tc.preconditions:
            description = f"前置条件：{gen_tc.preconditions}"

        # Map priority
        priority = PRIORITY_MAP.get(gen_tc.priority.strip(), "medium")

        # Create uitest-work TestCase
        tc = db_models.TestCase(
            project_id=project_id,
            module_id=module.id,
            project_case_number=get_next_project_case_number(db, project_id),
            name=gen_tc.title.strip() if gen_tc.title else f"Test Case {gen_tc.test_case_id}",
            description=description,
            priority=priority,
        )
        db.add(tc)
        db.flush()

        # Split test_steps and expected_results into TestStep records
        steps_text = _split_numbered_steps(gen_tc.test_steps)
        results_text = _split_expected_results(gen_tc.expected_result)
        if len(results_text) > len(steps_text):
            # 多余结果合并到最后一步
            base = results_text[:len(steps_text) - 1] if len(steps_text) > 1 else []
            extras = results_text[len(steps_text) - 1:]
            results_text = base + ['；'.join(extras)]
        elif len(results_text) < len(steps_text):
            # 不足时复用最后一条结果填充
            last = results_text[-1] if results_text else ''
            results_text = results_text + [last] * (len(steps_text) - len(results_text))
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

    db.commit()
    return created
