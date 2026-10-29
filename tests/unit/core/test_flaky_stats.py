from core.flaky_stats import flaky_stats


def test_flaky_needs_min_runs():
    runs = [{"case_id": 1, "status": "passed"}, {"case_id": 1, "status": "failed"}]
    assert flaky_stats(runs, min_runs=5) == []


def test_flaky_mixed_statuses():
    runs = [
        {"case_id": 1, "status": "passed"},
        {"case_id": 1, "status": "failed"},
        {"case_id": 1, "status": "passed"},
        {"case_id": 1, "status": "failed"},
        {"case_id": 1, "status": "passed"},
        {"case_id": 1, "status": "cancelled"},
    ]
    out = flaky_stats(runs, min_runs=5)
    assert len(out) == 1
    assert out[0]["runs"] == 5
    assert out[0]["pass_rate"] == 0.6


def test_all_passed_not_flaky():
    runs = [{"case_id": 1, "status": "passed"} for _ in range(6)]
    assert flaky_stats(runs, min_runs=5) == []
