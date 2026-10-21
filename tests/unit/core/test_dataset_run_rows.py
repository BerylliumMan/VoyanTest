"""039：按组执行队列顺序/组号。"""
from __future__ import annotations

from core.runner._persistence import build_dataset_execution_queue


def test_rows_expand_in_order_with_groups():
    queue = build_dataset_execution_queue(
        [7], None, "once", {7: [{"u": "a"}, {"u": "b"}]}
    )
    assert [(g, r["u"]) for _, _, g, r in queue] == [(1, "a"), (2, "b")]


def test_unbound_case_single_entry():
    queue = build_dataset_execution_queue([7], None, "once", None)
    assert queue == [(7, False, None, None)]
