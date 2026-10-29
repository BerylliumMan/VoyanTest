from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import db_models
from app.config import get_settings
from app.routers.quality import common as quality_common


def test_quality_defaults_on():
    s = get_settings()
    assert s.quality_ai_enabled is True
    assert s.quality_review_low_score == 60
    assert s.quality_cluster_days == 14
    assert s.quality_flaky_min_runs == 5
    assert s.quality_evidence_max_mb == 100
    assert s.quality_impact_enabled is True
    assert s.quality_flaky_enabled is True
    assert s.quality_traceability_enabled is True
    assert s.quality_impact_recent_days == 14
    assert s.quality_impact_stale_days == 30
    assert s.quality_flaky_recover_runs == 3


@pytest.mark.asyncio
async def test_capability_global_switches(db, monkeypatch):
    project = db_models.Project(name="042 switch")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    user = SimpleNamespace(role="admin", project_ids=[])

    for capability, flag in (
        ("impact", "quality_impact_enabled"),
        ("flaky", "quality_flaky_enabled"),
        ("traceability", "quality_traceability_enabled"),
    ):
        monkeypatch.setattr(
            quality_common, "get_settings",
            lambda flag=flag: SimpleNamespace(**{flag: False}),
        )
        with pytest.raises(HTTPException) as exc:
            await quality_common.ensure_capability(db, user, project.id, capability)
        assert exc.value.status_code == 403
