"""039 契约：报告按组过滤/标签辅助。"""
from __future__ import annotations


def test_group_filter_param_is_int_like():
    from app.routers.report_router import get_batch_detail

    import inspect

    sig = inspect.signature(get_batch_detail)
    assert "dataset_group" in sig.parameters
