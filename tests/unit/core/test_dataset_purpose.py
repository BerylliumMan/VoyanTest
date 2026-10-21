"""039：数据集用途隔离/预检（cross-purpose 拒绝）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.dataset_execution import plan_batch_datasets


def _case(**kw):
    base = dict(
        id=1, project_id=24, case_kind="ui", dataset_id=5,
        dataset_mode="sequential", loop_count=1, fail_fast=False,
        parameters=[{"name": "username"}],
        steps=[{"step_order": 1, "description": "输入 {{username}}"}],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _dataset(**kw):
    base = dict(id=5, project_id=24, purpose="ui", columns=["username"], rows=[{"username": "admin"}])
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_cross_purpose_rejected(monkeypatch):
    async def fake_get_dataset(db, dataset_id):
        return _dataset(purpose="api")

    import app.crud.api_dataset as ds_mod

    monkeypatch.setattr(ds_mod, "get_dataset", fake_get_dataset)
    plan = await plan_batch_datasets(None, [_case()])
    assert not plan.ok
    assert "不是 UI 数据集" in plan.errors[0]


@pytest.mark.asyncio
async def test_wrong_project_rejected(monkeypatch):
    async def fake_get_dataset(db, dataset_id):
        return _dataset(project_id=99)

    import app.crud.api_dataset as ds_mod

    monkeypatch.setattr(ds_mod, "get_dataset", fake_get_dataset)
    plan = await plan_batch_datasets(None, [_case()])
    assert not plan.ok
    assert "不属于" in plan.errors[0]


@pytest.mark.asyncio
async def test_missing_column_rejected(monkeypatch):
    async def fake_get_dataset(db, dataset_id):
        return _dataset(columns=["other"], rows=[{"other": "x"}])

    import app.crud.api_dataset as ds_mod

    monkeypatch.setattr(ds_mod, "get_dataset", fake_get_dataset)
    plan = await plan_batch_datasets(None, [_case()])
    assert not plan.ok
    assert "缺少列 username" in plan.errors[0]


@pytest.mark.asyncio
async def test_ui_dataset_expands(monkeypatch):
    async def fake_get_dataset(db, dataset_id):
        return _dataset(rows=[{"username": "admin"}, {"username": "guest"}])

    import app.crud.api_dataset as ds_mod

    monkeypatch.setattr(ds_mod, "get_dataset", fake_get_dataset)
    plan = await plan_batch_datasets(None, [_case()])
    assert plan.ok
    assert plan.total_groups == 2
    assert plan.rows_for(1)[1]["username"] == "guest"
