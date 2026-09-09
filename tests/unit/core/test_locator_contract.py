from core.locator_candidates import (
    LocatorCandidate,
    extract_candidates,
    serialize_candidates,
    validate_candidate_ref,
)
from core.goal_agent_loop import is_locator_decision_error
from core.locator_candidates import is_snapshot_ref


def test_extract_candidates_preserves_ref_role_name_and_frame():
    snapshot = '- button "提交" [ref=e12]\n- textbox "问题描述" [ref=f1e3]'

    candidates = extract_candidates(snapshot, frame_path=("iframe#form",))

    assert [candidate.ref for candidate in candidates] == ["e12", "f1e3"]
    assert candidates[0].role == "button"
    assert candidates[0].name == "提交"
    assert candidates[0].frame_path == ("iframe#form",)


def test_validate_candidate_ref_rejects_invented_and_stale_decisions():
    candidates = extract_candidates('- button "提交" [ref=e12]')

    invented = validate_candidate_ref(
        "e999",
        snapshot_version="snap-1",
        decision_version="snap-1",
        candidates=candidates,
    )
    stale = validate_candidate_ref(
        "e12",
        snapshot_version="snap-2",
        decision_version="snap-1",
        candidates=candidates,
    )

    assert invented.failure_kind == "no_candidate"
    assert stale.failure_kind == "stale_ref"


def test_validate_candidate_ref_accepts_missing_version_with_known_ref():
    candidates = extract_candidates('- button "提交" [ref=e12]')

    result = validate_candidate_ref(
        "e12",
        snapshot_version="snap-1",
        decision_version=None,
        candidates=candidates,
    )

    assert result.valid is True


def test_validate_candidate_ref_returns_state_failure_kinds():
    candidates = (
        LocatorCandidate("e0", "button", "隐藏", "隐藏", {}, (), visible=False),
        LocatorCandidate("e1", "button", "禁用", "禁用", {}, (), enabled=False),
        LocatorCandidate("e2", "button", "遮挡", "遮挡", {}, (), obscured=True),
    )

    assert validate_candidate_ref(
        "e0", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "not_visible"
    assert validate_candidate_ref(
        "e1", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "disabled"
    assert validate_candidate_ref(
        "e2", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "obscured"


def test_selector_needs_candidate_check_only_for_snapshot_refs():
    from core.goal_agent_loop import selector_needs_candidate_check

    assert selector_needs_candidate_check("e12") is True
    assert selector_needs_candidate_check("f1e3") is True
    assert selector_needs_candidate_check("#submit") is False
    assert selector_needs_candidate_check("//button[1]") is False
    assert selector_needs_candidate_check(None) is False
    assert selector_needs_candidate_check("") is False


def test_locator_decision_error_is_retryable_only_for_candidate_rejection():
    assert is_locator_decision_error("invalid locator decision: stale_ref") is True
    assert is_locator_decision_error("LLM did not return JSON") is False


def test_locator_contract_keeps_legacy_css_selector_compatibility():
    assert is_snapshot_ref("e12") is True
    assert is_snapshot_ref("f1e3") is True
    assert is_snapshot_ref("#submit") is False


def test_runner_bridge_share_failure_taxonomy():
    from core.locator_verification import LocatorActionEvidence, verify_locator_action

    locator_kinds = {"stale_ref", "no_candidate", "not_visible", "disabled", "obscured"}
    verification_kinds = {
        "no_observable_change",
        "input_value_mismatch",
        "selected_value_mismatch",
        "verification_missing_expected_value",
    }

    candidates = extract_candidates('- button "提交" [ref=e12]')
    stale = validate_candidate_ref(
        "e12", snapshot_version="s2", decision_version="s1", candidates=candidates
    )
    assert stale.failure_kind in locator_kinds

    no_change = verify_locator_action(LocatorActionEvidence("click", "same", "same"))
    assert no_change.failure_kind in verification_kinds


def test_serialize_candidates_exposes_semantic_context():
    candidates = extract_candidates('- button "提交" [ref=e12]')

    assert "ref=e12" in serialize_candidates(candidates)
    assert "name='提交'" in serialize_candidates(candidates)
