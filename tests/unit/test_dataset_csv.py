"""US10 数据集 CSV 导出往返（round-trip）——033-api-test-maturity。

TDD：先于实现编写（预期红：to_csv_dataset 不存在）。
"""

from __future__ import annotations

from app.crud.api_dataset import parse_csv_dataset, to_csv_dataset


def test_round_trip_basic():
    columns = ["user", "pwd"]
    rows = [{"user": "u1", "pwd": "p1"}, {"user": "u2", "pwd": "p2"}]
    csv_text = to_csv_dataset(columns, rows)
    back = parse_csv_dataset(csv_text.encode("utf-8"))
    assert back["columns"] == columns
    assert back["rows"] == rows


def test_round_trip_special_characters():
    columns = ["name", "note"]
    rows = [
        {"name": "逗号,值", "note": '带"引号"'},
        {"name": "换行\n值", "note": "中文✓"},
    ]
    csv_text = to_csv_dataset(columns, rows)
    back = parse_csv_dataset(csv_text.encode("utf-8"))
    assert back["rows"] == rows


def test_round_trip_with_bom_export_variant():
    """导出端点会加 BOM（Excel 友好）；导入端 utf-8-sig 必须兼容。"""
    csv_text = to_csv_dataset(["a"], [{"a": "1"}])
    back = parse_csv_dataset(("\ufeff" + csv_text).encode("utf-8"))
    assert back["rows"] == [{"a": "1"}]


def test_empty_rows_round_trip():
    csv_text = to_csv_dataset(["a", "b"], [])
    back = parse_csv_dataset(csv_text.encode("utf-8"))
    assert back["columns"] == ["a", "b"] and back["rows"] == []


def test_missing_key_filled_empty():
    csv_text = to_csv_dataset(["a", "b"], [{"a": "1"}])
    back = parse_csv_dataset(csv_text.encode("utf-8"))
    assert back["rows"] == [{"a": "1", "b": ""}]


def test_column_order_preserved():
    csv_text = to_csv_dataset(["z", "a", "m"], [{"z": "1", "a": "2", "m": "3"}])
    assert csv_text.splitlines()[0].split(",") == ["z", "a", "m"]
