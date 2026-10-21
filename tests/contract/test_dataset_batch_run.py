"""039 契约：批量执行覆盖字段。"""
from __future__ import annotations

from app.routers.testcase.execution._schemas import BatchRunRequest


def test_batch_accepts_dataset_override():
    body = BatchRunRequest(
        case_ids=[1], dataset_id=5, dataset_mode="loop", loop_count=3,
        dataset_fail_fast=True,
    )
    assert body.dataset_id == 5
    assert body.loop_count == 3
    assert body.dataset_fail_fast is True


def test_batch_defaults_do_not_override():
    body = BatchRunRequest(case_ids=[1])
    assert body.dataset_id is None
    assert body.dataset_mode is None
    assert body.loop_count is None
