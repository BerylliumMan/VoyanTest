from app.services.quality_review import _trim_result
from app.services.quality_defect import _render_markdown


def test_trim_result_reduces_large_payload():
    items = [{"target_id": i, "summary": "x" * 4000, "issues": []} for i in range(30)]
    raw = {"items": items, "summary": "s"}
    out = _trim_result(raw)
    import json
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 64 * 1024


def test_render_markdown_has_required_sections():
    md = _render_markdown(
        title="缺陷",
        fields={
            "environment": "测试环境", "preconditions": "", "steps": ["打开页面"],
            "expected": "成功", "actual": "失败", "evidence": ["/reports/a.png"],
        },
        notes="备注",
    )
    for key in ("# 缺陷", "## 环境", "## 复现步骤", "## 期望结果", "## 实际结果", "## 证据", "## 备注"):
        assert key in md
