from types import SimpleNamespace

import pytest

from app.services import quality_insights as qi


@pytest.mark.asyncio
async def test_generate_insights_deterministic_without_ai(monkeypatch):
    async def fake_runs(*args, **kwargs):
        return [
            {"run_id": 1, "case_id": 1, "started_at": "2026-10-01", "status": "failed"},
            {"run_id": 2, "case_id": 1, "started_at": "2026-10-02", "status": "failed"},
            {"run_id": 3, "case_id": 2, "started_at": "2026-10-03", "status": "failed"},
        ]

    async def fake_logs(*args, **kwargs):
        return {1: [], 2: [], 3: []}

    async def fake_history(*args, **kwargs):
        return [
            {"case_id": 1, "status": "passed"},
            {"case_id": 1, "status": "failed"},
            {"case_id": 1, "status": "passed"},
            {"case_id": 1, "status": "failed"},
            {"case_id": 1, "status": "passed"},
        ]

    async def no_cache(*args, **kwargs):
        return None

    async def save(*args, **kwargs):
        return SimpleNamespace(id=1, result=kwargs.get("result") or {"groups": []}, status="ready")

    async def no_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(qi.crud_run, "list_runs_for_quality", fake_runs)
    monkeypatch.setattr(qi, "_logs_by_run", fake_logs)
    monkeypatch.setattr(qi, "_case_history", fake_history)
    monkeypatch.setattr(qi.crud_api_ai, "get_analysis", no_cache)
    monkeypatch.setattr(qi.crud_api_ai, "upsert_analysis", save)
    monkeypatch.setattr("app.auth.log_audit", no_audit)

    row, cached = await qi.generate_insights(
        None, project_id=1, days=14, include_ai=False,
    )
    assert cached is False
    assert row.result["total_failures"] == 3
    assert row.result["ai_available"] is False
    assert len(row.result["flaky"]) == 1
    assert row.result["flaky"][0]["case_id"] == 1
