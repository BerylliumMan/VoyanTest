from core.goal_agent_loop import STAGNATION_LIMIT, detect_stagnation


def _entry(action, selector, value, success):
    return {"action": action, "selector": selector, "value": value, "success": success}


def test_all_failed_identical_turns_is_stagnation():
    journal = [_entry("click", "e10", None, False) for _ in range(STAGNATION_LIMIT)]
    assert detect_stagnation(journal) is True


def test_successful_repeat_is_not_stagnation():
    journal = [_entry("click", "e10", None, True) for _ in range(STAGNATION_LIMIT)]
    assert detect_stagnation(journal) is False


def test_mixed_failure_and_success_tail_is_not_stagnation():
    journal = [_entry("click", "e10", None, False) for _ in range(STAGNATION_LIMIT - 1)]
    journal.append(_entry("click", "e10", None, True))
    assert detect_stagnation(journal) is False


def test_varied_failed_actions_are_not_stagnation():
    journal = [
        _entry("click", f"e{10 + i}", None, False)
        for i in range(STAGNATION_LIMIT)
    ]
    assert detect_stagnation(journal) is False


def test_short_journal_is_not_stagnation():
    journal = [_entry("click", "e10", None, False) for _ in range(STAGNATION_LIMIT - 1)]
    assert detect_stagnation(journal) is False
