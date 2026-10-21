"""039：数据组执行队列与参数化固化跳过。"""
from __future__ import annotations

import pytest

from core.runner._persistence import build_dataset_execution_queue
from core.script_synthesize import case_is_parameterized


def test_queue_expands_rows_in_order():
    queue = build_dataset_execution_queue(
        [11], None, "once", {11: [{"u": "a"}, {"u": "b"}]}
    )
    assert queue == [
        (11, False, 1, {"u": "a"}),
        (11, False, 2, {"u": "b"}),
    ]


def test_queue_unbound_case_single():
    queue = build_dataset_execution_queue([11, 12], None, "once", {11: [{"u": "a"}]})
    assert queue == [
        (11, False, 1, {"u": "a"}),
        (12, False, None, None),
    ]


def test_queue_init_before_each_repeats_for_each_group():
    queue = build_dataset_execution_queue(
        [11], [99], "before_each", {11: [{"u": "a"}, {"u": "b"}]}
    )
    # init 按「主用例」执行一次（不按数据组重复），随后展开该用例的每个数据组
    assert [item[0] for item in queue] == [99, 11, 11]
    assert [item[2] for item in queue] == [None, 1, 2]


def test_queue_init_once():
    queue = build_dataset_execution_queue(
        [11], [99], "once", {11: [{"u": "a"}, {"u": "b"}]}
    )
    assert [(c, g) for c, _, g, _ in queue] == [(99, None), (11, 1), (11, 2)]


def test_queue_preserves_original_group_numbers_on_filter():
    queue = build_dataset_execution_queue(
        [11], None, "once",
        {11: [{"u": "guest"}]},
        {11: [2]},
    )
    assert queue == [(11, False, 2, {"u": "guest"})]


@pytest.mark.parametrize(
    "steps, expected",
    [
        ([{"description": "用 {{username}} 登录"}], True),
        ([{"description": "用 {{ username }} 登录"}], True),
        ([{"description": "普通登录"}], False),
        ([{"description": "登录", "structured_step": {"value": "{{pw}}"}}], True),
        ([], False),
    ],
)
def test_case_is_parameterized(steps, expected):
    assert case_is_parameterized(steps) is expected
