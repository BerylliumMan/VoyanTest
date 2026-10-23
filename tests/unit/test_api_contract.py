"""040 US5：契约 diff 纯函数。"""
from __future__ import annotations

from core.api_contract import diff_response


SCHEMA = {
    "statuses": {
        "200": {
            "schema": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"x": {"type": "string"}}},
                    },
                },
            }
        }
    }
}


def test_missing_required_is_breaking():
    result = diff_response(SCHEMA, 200, {"id": 1})
    assert result["status"] == "checked"
    assert result["breaking"] is True
    assert any(f["kind"] == "missing_required" and f["path"] == ".name" for f in result["findings"])


def test_type_change_is_breaking():
    result = diff_response(SCHEMA, 200, {"id": "1", "name": "x"})
    assert result["breaking"] is True
    assert any(f["kind"] == "type_changed" and f["path"] == "id" for f in result["findings"])


def test_added_field_is_compatible():
    result = diff_response(SCHEMA, 200, {"id": 1, "name": "x", "extra": True})
    assert result["breaking"] is False
    assert any(f["kind"] == "added_field" for f in result["findings"])


def test_unknown_status_is_breaking():
    result = diff_response(SCHEMA, 500, {})
    assert result["breaking"] is True
    assert any(f["kind"] == "status_changed" for f in result["findings"])


def test_missing_schema_is_missing_evidence():
    assert diff_response({}, 200, {})["status"] == "missing_evidence"
    assert diff_response({"statuses": {"200": {}}}, 200, {})["status"] == "missing_evidence"


def test_no_diff():
    result = diff_response(SCHEMA, 200, {"id": 1, "name": "x"})
    assert result["findings"] == []
    assert result["breaking"] is False
