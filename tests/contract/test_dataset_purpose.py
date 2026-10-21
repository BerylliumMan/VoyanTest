"""039 契约：数据集用途字段（离线 schema 校验）。"""
from __future__ import annotations

from app.models.schemas import ApiDatasetPayload, ApiDatasetResponse


def test_dataset_create_defaults_to_api():
    assert ApiDatasetPayload(project_id=1, name="x", columns=["a"], rows=[]).purpose == "api"


def test_dataset_update_accepts_ui():
    assert ApiDatasetResponse(id=1, project_id=1, name="x", purpose="ui").purpose == "ui"
