from core.failure_signature import build_signature, cluster_failures, normalize_error


def test_normalize_error_collapses_noise():
    a = normalize_error("Ref e123 not found at http://x/1 after 123 ms")
    b = normalize_error("Ref e999 not found at http://x/2 after 456 ms")
    assert a == b


def test_kind_hint_environment():
    sig = build_signature(logs=[{"level": "ERROR", "message": "connect ECONNREFUSED 127.0.0.1:8092"}])
    assert sig["kind_hint"] == "environment"


def test_cluster_groups_and_singletons():
    entries = [
        {"run_id": 1, "case_id": 1, "signature": "a", "started_at": "2026-01-01"},
        {"run_id": 2, "case_id": 1, "signature": "a", "started_at": "2026-01-02"},
        {"run_id": 3, "case_id": 2, "signature": "b", "started_at": "2026-01-03"},
    ]
    out = cluster_failures(entries, min_runs=2)
    assert out["total_failures"] == 3
    assert len(out["groups"]) == 1
    assert len(out["singletons"]) == 1
    assert out["groups"][0]["run_count"] == 2
