"""040 US1：接口失败归因证据塑形/校验/缓存。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.api_ai_analysis import (
    _input_hash,
    shape_failure_evidence,
    validate_failure_result,
)


def _run(**kw):
    base = dict(id=944, case_id=736, status="failed", duration=1.2, execution_mode="server", report_path=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_shape_evidence_masks_headers_and_truncates():
    report = {
        "status": "failed",
        "environment": {"id": 44, "name": "local"},
        "steps": [{
            "description": "登录",
            "success": False,
            "error": "401",
            "request": {
                "method": "POST",
                "url": "http://x/login",
                "headers": {"Authorization": "Bearer secret", "Content-Type": "application/json"},
                "body": "x" * 5000,
            },
            "response": {
                "status": 401,
                "headers": {"Set-Cookie": "sid=abc"},
                "body": "y" * 9000,
            },
            "assertions": [{
                "type": "status_code", "condition": "equals", "expected": "200",
                "actual": "401", "passed": False,
            }],
        }],
    }
    evidence = shape_failure_evidence(_run(), report)
    assert evidence["steps"][0]["response"]["status"] == 401
    assert evidence["steps"][0]["request"]["headers"]["Authorization"] != "Bearer secret"
    assert "TRUNCATED" in evidence["steps"][0]["request"]["body"]
    assert "TRUNCATED" in evidence["steps"][0]["response"]["body"]
    assert evidence["steps"][0]["assertions"][0]["expected"] == "200"


def test_shape_evidence_missing_report():
    evidence = shape_failure_evidence(_run(), None)
    assert evidence["steps"] == []
    assert evidence["missing_evidence"] == ["report_steps"]


def test_validate_rejects_unknown_root_and_requires_evidence():
    result = validate_failure_result({"root_cause": "made_up", "evidence": [], "summary": "x"})
    assert result["root_cause"] == "unknown"
    assert result["evidence"] == []


def test_validate_clamps_confidence_and_limits():
    result = validate_failure_result({
        "root_cause": "auth", "confidence": 9,
        "evidence": [{"type": "response", "ref": "s1", "detail": "401"}] * 20,
        "suggestions": [{"action": "update_request"}] * 20,
    })
    assert result["confidence"] == 1.0
    assert len(result["evidence"]) == 10
    assert len(result["suggestions"]) == 8


def test_input_hash_stable():
    a = _input_hash({"steps": [{"x": 1}]})
    b = _input_hash({"steps": [{"x": 1}]})
    assert a == b
    assert len(a) == 64
