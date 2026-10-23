"""039：参数化预检（无绑定/占位符拒绝）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.dataset_execution import plan_batch_datasets


def _case(**kw):
    base = dict(
        id=1, project_id=24, case_kind="ui", dataset_id=None,
        dataset_mode="sequential", loop_count=1, fail_fast=False,
        parameters=None, steps=[{"step_order": 1, "description": "点击登录"}],
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_unbound_without_placeholder_is_single_run():
    plan = await plan_batch_datasets(None, [_case()])
    assert plan.ok
    assert plan.rows_for(1) is None


@pytest.mark.asyncio
async def test_unbound_with_placeholder_rejected():
    plan = await plan_batch_datasets(
        None, [_case(steps=[{"step_order": 1, "description": "输入 {{username}}"}])]
    )
    assert not plan.ok
    assert "当前环境变量未提供" in plan.errors[0]


@pytest.mark.asyncio
async def test_unbound_with_env_values_ok():
    plan = await plan_batch_datasets(
        None,
        [_case(steps=[{"step_order": 1, "description": "输入 {{username}}"}])],
        env_values={"username": "admin"},
    )
    assert plan.ok
    assert plan.rows_for(1) is None


@pytest.mark.asyncio
async def test_api_case_skipped():
    plan = await plan_batch_datasets(None, [_case(case_kind="api")])
    assert plan.ok
    assert plan.total_groups == 0
