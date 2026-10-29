import pytest

from app.services import quality_tasks as qt


def _state(task_id="t1", status="running", done=1, total=3):
    return {
        "task_id": task_id, "status": status, "done": done, "total": total,
        "project_id": 1, "user_id": 1, "analysis_id": None,
        "result": None, "error": None, "cancelled": False,
    }


def test_task_progress_public():
    qt._TASKS["t1"] = _state()
    data = qt.get_task("t1")
    assert data["progress"] == 33
    assert data["status"] == "running"


def test_stop_running_task():
    qt._TASKS["t1"] = _state()
    assert qt.stop_task("t1") is True
    assert qt._TASKS["t1"]["status"] == "stopped"
    assert qt._TASKS["t1"]["cancelled"] is True
    assert qt.stop_task("t1") is False


def test_stop_finished_rejected():
    qt._TASKS["t2"] = _state(task_id="t2", status="completed")
    assert qt.stop_task("t2") is False


def test_missing_task():
    assert qt.get_task("nope") is None
