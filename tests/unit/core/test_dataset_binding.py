"""039：用例数据集绑定（crud.update_case_dataset + schemas）。"""
from __future__ import annotations

import pytest

from app import db_models
from app.crud.testcase import get_test_case, update_case_dataset
from app.models.schemas import TestCaseCreate, TestCaseDatasetUpdate, TestCaseUpdate


async def _make_case(db, **kwargs):
    case = db_models.TestCase(project_id=24, name="绑定用例", case_kind="ui", **kwargs)
    db.add(case)
    await db.flush()
    db.add(db_models.TestStep(case_id=case.id, step_order=1, description="用 {{username}} 登录"))
    await db.commit()
    await db.refresh(case)
    return case


@pytest.mark.asyncio
async def test_update_binding_sets_fields_and_clears_script(db):
    case = await _make_case(db)
    case.compiled_script = "old script"
    case.compiled_script_hash = "abc"
    await db.commit()

    updated, previous = await update_case_dataset(
        db, case.id, dataset_id=12, dataset_mode="loop", loop_count=3, fail_fast=True
    )
    assert updated is not None
    assert previous is None
    assert updated.dataset_id == 12
    assert updated.dataset_mode == "loop"
    assert updated.loop_count == 3
    assert updated.fail_fast is True
    assert updated.compiled_script is None
    assert updated.compiled_script_hash is None


@pytest.mark.asyncio
async def test_update_binding_unbind_keeps_other_fields(db):
    case = await _make_case(db, dataset_id=12, dataset_mode="random", loop_count=2)
    await db.commit()

    updated, previous = await update_case_dataset(db, case.id, dataset_id=None)
    assert previous == 12
    assert updated.dataset_id is None
    assert updated.dataset_mode == "random"
    assert updated.loop_count == 2


@pytest.mark.asyncio
async def test_update_binding_only_parameters(db):
    case = await _make_case(db)
    updated, _ = await update_case_dataset(
        db, case.id, parameters=[{"name": "username", "description": "账号"}]
    )
    assert updated.parameters == [{"name": "username", "description": "账号"}]
    refreshed = await get_test_case(db, case.id)
    assert refreshed.parameters[0]["name"] == "username"


def test_case_create_accepts_dataset_fields():
    payload = TestCaseCreate(
        project_id=24,
        name="参数化",
        case_kind="ui",
        parameters=[{"name": "username"}],
        dataset_id=7,
        dataset_mode="loop",
        loop_count=2,
        fail_fast=True,
        steps=[],
    )
    assert payload.dataset_id == 7
    assert payload.loop_count == 2


def test_case_update_fields_are_optional():
    payload = TestCaseUpdate(name="x")
    assert payload.dataset_id is None
    assert payload.model_fields_set == {"name"}


def test_dataset_update_partial_fields():
    payload = TestCaseDatasetUpdate(dataset_id=None)
    assert payload.dataset_id is None
    assert payload.model_fields_set == {"dataset_id"}
