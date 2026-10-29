from app.services.quality_analysis import validate_ui_failure_result


def test_validate_ui_result_normalizes_unknown_and_limits():
    out = validate_ui_failure_result({
        "root_cause": "bad", "confidence": 2, "summary": "x" * 1000,
        "evidence": [{"source": "log", "quote": "q"}],
        "suggestions": [{"action": "nope", "detail": "d"}],
        "missing_evidence": ["screenshot"],
    })
    assert out["root_cause"] == "unknown"
    assert out["confidence"] == 1.0
    assert len(out["summary"]) <= 650
    assert out["suggestions"][0]["action"] == "inspect"


def test_validate_ui_result_unknown_without_evidence():
    out = validate_ui_failure_result({"root_cause": "test_case", "evidence": []})
    assert out["root_cause"] == "unknown"
