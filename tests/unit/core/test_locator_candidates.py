from core.locator_candidates import (
    LocatorCandidate,
    actionable_candidates,
    extract_candidates,
    serialize_candidates,
    snapshot_has_visible_overlay,
    validate_candidate_ref,
)


def test_validate_candidate_ref_rejects_disabled_or_obscured_targets():
    candidates = (
        LocatorCandidate("e1", "button", "关闭", "关闭", {}, ()),
        LocatorCandidate("e2", "button", "提交", "提交", {}, (), enabled=False),
        LocatorCandidate("e3", "button", "保存", "保存", {}, (), obscured=True),
    )

    assert validate_candidate_ref(
        "e2", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "disabled"
    assert validate_candidate_ref(
        "e3", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "obscured"


def test_serialize_candidates_bounds_model_context():
    candidates = tuple(
        LocatorCandidate(f"e{i}", "button", str(i), str(i), {}, ())
        for i in range(3)
    )

    serialized = serialize_candidates(candidates, max_candidates=2)

    assert "ref=e0" in serialized
    assert "ref=e1" in serialized
    assert "ref=e2" not in serialized


def test_actionable_candidates_filters_invisible_disabled_and_obscured():
    candidates = (
        LocatorCandidate("e1", "button", "可见", "可见", {}, ()),
        LocatorCandidate("e2", "button", "隐藏", "隐藏", {}, (), visible=False),
        LocatorCandidate("e3", "button", "禁用", "禁用", {}, (), enabled=False),
        LocatorCandidate("e4", "button", "遮挡", "遮挡", {}, (), obscured=True),
    )

    assert [item.ref for item in actionable_candidates(candidates)] == ["e1"]


def test_extract_ref_token_recovers_pasted_candidate_line():
    from core.locator_candidates import extract_ref_token

    pasted = "ref=f3e24 role=textbox name='请选择单位' frame=main"
    assert extract_ref_token(pasted) == "f3e24"
    assert extract_ref_token("f3e24") == "f3e24"
    assert extract_ref_token("#submit-btn") == "#submit-btn"
    assert extract_ref_token(None) is None


def test_snapshot_lines_mark_hidden_and_disabled_candidates():
    candidates = extract_candidates(
        '- button "隐藏" [ref=e1 hidden]\n- button "禁用" [ref=e2 disabled]'
    )

    assert candidates[0].visible is False
    assert candidates[1].enabled is False
    assert snapshot_has_visible_overlay('- dialog "提示" [ref=e3]') is True
