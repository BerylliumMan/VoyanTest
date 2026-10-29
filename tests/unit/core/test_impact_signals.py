from core.impact_signals import build_candidates, merge_signals, reason_text, signal


def test_merge_signals_dedupes_same_type_ref():
    merged = merge_signals([
        signal("definition_change", ref="definition:1", detail="v1"),
        signal("definition_change", ref="definition:1", detail="v2"),
        signal("recent_failure", ref="run:1"),
    ])
    assert len(merged) == 2
    assert merged[0]["detail"] == "v1"


def test_build_candidates_merges_signals_and_manual():
    rows = build_candidates(
        {3: [signal("recent_failure", ref="run:7", detail="失败")]},
        case_meta={3: {"case_kind": "api", "name": "登录"}},
        manual_case_ids=[3, 9],
        max_candidates=10,
    )
    assert [r["case_id"] for r in rows] == [3, 9]
    assert len(rows[0]["reasons"]) == 2
    assert rows[0]["case_kind"] == "api"


def test_build_candidates_truncates():
    rows = build_candidates({i: [signal("stale", ref=f"case:{i}")] for i in range(1, 6)}, max_candidates=3)
    assert len(rows) == 3


def test_reason_text_contains_label_and_detail():
    assert reason_text(signal("definition_change", detail="POST /x")) == "接口定义变更：POST /x"
