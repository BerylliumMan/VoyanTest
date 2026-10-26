"""040 增强：生成进度字段与策略进度存储。"""
from app.models.schemas import ApiGenerateRequest


def test_generate_request_accepts_progress_id():
    req = ApiGenerateRequest(project_id=1, definition_ids=[2], progress_id="p-12345678")
    assert req.progress_id == "p-12345678"


def test_generate_request_progress_optional():
    assert ApiGenerateRequest(project_id=1).progress_id is None


def test_strategy_progress_store_clamps():
    from app.services.api_ai_strategy import _PROGRESS, _set_progress, get_progress

    _PROGRESS.clear()
    _set_progress("p1", -5, "a")
    assert get_progress("p1")["progress"] == 0
    _set_progress("p1", 250, "b")
    assert get_progress("p1")["progress"] == 100
    _set_progress(None, 50, "ignored")
    assert get_progress("p1")["message"] == "b"
