"""039：数据集展开与计划（core/dataset_iteration.py）。"""
from __future__ import annotations

import pytest

from core.dataset_iteration import (
    DatasetPlanError,
    dataset_columns,
    normalize_rows,
    validate_dataset_plan,
)


def _dataset(*rows, columns=("username", "password")):
    return {"columns": list(columns), "rows": list(rows)}


def test_normalize_dict_rows_stringifies():
    rows = normalize_rows(_dataset({"username": "u1", "password": None}))
    assert rows == [{"username": "u1", "password": ""}]


def test_normalize_list_rows_uses_columns():
    rows = normalize_rows(_dataset(["u1", "p1"], ["u2", "p2"]))
    assert rows[0]["username"] == "u1"
    assert rows[1]["password"] == "p2"


def test_dataset_columns():
    assert dataset_columns(_dataset(columns=("a", "b"))) == ["a", "b"]
    assert dataset_columns(None) == []


def test_sequential_keeps_order_and_count():
    rows = validate_dataset_plan(_dataset({"username": "u1"}, {"username": "u2"}))
    assert [r["username"] for r in rows] == ["u1", "u2"]


def test_loop_multiplies_rows():
    rows = validate_dataset_plan(
        _dataset({"username": "u1"}, {"username": "u2"}),
        mode="loop",
        loop_count=3,
    )
    assert [r["username"] for r in rows] == ["u1", "u2", "u1", "u2", "u1", "u2"]


def test_random_same_set():
    rows = validate_dataset_plan(
        _dataset({"username": "u1"}, {"username": "u2"}, {"username": "u3"}),
        mode="random",
    )
    assert sorted(r["username"] for r in rows) == ["u1", "u2", "u3"]


def test_empty_dataset_rejected():
    with pytest.raises(DatasetPlanError, match="没有数据行"):
        validate_dataset_plan(_dataset())


def test_unknown_mode_rejected():
    with pytest.raises(DatasetPlanError, match="未知展开模式"):
        validate_dataset_plan(_dataset({"username": "u1"}), mode="bogus")


@pytest.mark.parametrize("count", [0, -1, 101, "x"])
def test_loop_count_bounds_rejected(count):
    with pytest.raises(DatasetPlanError, match="循环次数"):
        validate_dataset_plan(_dataset({"username": "u1"}), mode="loop", loop_count=count)


def test_group_limit_message_contains_actual_count():
    rows = [{"username": f"u{i}"} for i in range(60)]
    dataset = {"columns": ["username"], "rows": rows}
    with pytest.raises(DatasetPlanError) as exc:
        validate_dataset_plan(dataset, mode="loop", loop_count=2, max_groups=100)
    message = str(exc.value)
    assert "120 组" in message and "100" in message


def test_exact_limit_allowed():
    rows = [{"username": f"u{i}"} for i in range(100)]
    dataset = {"columns": ["username"], "rows": rows}
    assert len(validate_dataset_plan(dataset, max_groups=100)) == 100
