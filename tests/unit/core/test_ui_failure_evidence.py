from core.ui_failure_evidence import build_ui_failure_evidence, split_segments, mask_text


class _Run:
    id = 1
    case_id = 2
    status = "failed"
    duration = 3.2
    execution_mode = "server"


def test_split_and_tags():
    segs = split_segments("点击失败 | [console] Error: boom | [network] GET /x 500")
    assert len(segs) == 3


def test_build_evidence_with_portal_tags():
    logs = [
        {"level": "ERROR", "message": "断言失败：没有找到按钮 | [console] Error: x", "screenshot_path": "reports/a.png"},
        {"level": "warning", "message": "[network] GET /api 500"},
    ]
    ev = build_ui_failure_evidence(run=_Run(), logs=logs, case_name="登录")
    assert ev["failed_step"]["text"].startswith("断言失败")
    assert ev["console"] and "Error" in ev["console"][0]
    assert ev["network"]
    assert ev["screenshots"] == ["reports/a.png"]
    assert ev["missing_evidence"] == []


def test_build_evidence_no_logs_marks_missing():
    ev = build_ui_failure_evidence(run=_Run(), logs=[])
    assert "steps" in ev["missing_evidence"]
    assert "screenshot" in ev["missing_evidence"]


def test_mask_text_hides_secrets():
    text = mask_text("password=abc123 Authorization: Bearer xyz token: zz")
    assert "abc123" not in text
    assert "xyz" not in text
    assert "zz" not in text
