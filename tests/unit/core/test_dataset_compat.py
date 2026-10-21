"""039：参数化降级/兼容回归。"""
from __future__ import annotations

import pytest

from core.dataset_iteration import DatasetPlanError, validate_dataset_plan
from core.runner._persistence import build_dataset_execution_queue
from core.script_synthesize import case_is_parameterized


def test_non_parameterized_queue_unchanged():
    queue = build_dataset_execution_queue([1, 2], None, "once", None)
    assert queue == [(1, False, None, None), (2, False, None, None)]


def test_parameterized_without_binding_is_detected():
    steps = [{"description": "输入 {{username}}"}]
    assert case_is_parameterized(steps) is True


def test_plain_steps_are_not_parameterized():
    assert case_is_parameterized([{"description": "点击登录"}]) is False


def test_empty_dataset_rejected():
    with pytest.raises(DatasetPlanError):
        validate_dataset_plan({"columns": ["u"], "rows": []})


def test_max_groups_rejected():
    rows = [{"u": str(i)} for i in range(3)]
    with pytest.raises(DatasetPlanError):
        validate_dataset_plan({"columns": ["u"], "rows": rows}, mode="loop", loop_count=2, max_groups=5)


def test_dataset_rows_original_group_numbers():
    queue = build_dataset_execution_queue(
        [1], None, "once", {1: [{"u": "b"}]}, {1: [2]}
    )
    assert queue[0][2] == 2
