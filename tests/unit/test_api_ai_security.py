"""040 T068：AI 输入/结果脱敏。"""
from types import SimpleNamespace

from app.services.api_ai_analysis import shape_failure_evidence


def test_failure_evidence_masks_authorization_and_cookie_headers():
    run = SimpleNamespace(id=1, case_id=2, status="failed", duration=1.0,
                          execution_mode="server", report_path=None)
    report = {
        "steps": [{
            "success": False,
            "request": {"method": "GET", "url": "/x",
                        "headers": {"Authorization": "Bearer top-secret", "Cookie": "sid=abc"},
                        "body": "token=top-secret"},
            "response": {"status": 401, "headers": {"Set-Cookie": "sid=abc"}, "body": "{}"},
            "assertions": [],
        }],
    }
    evidence = shape_failure_evidence(run, report)
    headers = evidence["steps"][0]["request"]["headers"]
    assert "top-secret" not in headers["Authorization"]
    assert "abc" not in headers["Cookie"]
    assert "top-secret" not in str(evidence)
    assert "sid=abc" not in str(evidence)
