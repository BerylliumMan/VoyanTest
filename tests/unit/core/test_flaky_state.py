from core.flaky_state import can_transition, next_after_confirm, next_consecutive_passes


def test_transition_matrix():
    assert can_transition("candidate", "confirming")
    assert can_transition("confirming", "quarantined")
    assert can_transition("quarantined", "recovered")
    assert not can_transition("recovered", "confirming")
    assert not can_transition("unknown", "quarantined")


def test_next_after_confirm():
    assert next_after_confirm(results=["passed", "passed"], confirm_runs=3) == "candidate"
    assert next_after_confirm(results=["passed", "failed"], confirm_runs=3) == "quarantined"
    assert next_after_confirm(results=[], confirm_runs=3) == "candidate"


def test_consecutive_passes_reset_on_failure():
    assert next_consecutive_passes(2, "passed") == 3
    assert next_consecutive_passes(2, "failed") == 0
    assert next_consecutive_passes(2, "cancelled") == 0
