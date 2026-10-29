# app/services/quality_tasks.py
# 041 US2：批量评审后台任务（进程内，与生成/探索一致；重启即丢失）。
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_TASKS: dict[str, dict] = {}
_CONCURRENT: dict[tuple[int, int], str] = {}


def _public(state: dict) -> dict:
    return {
        "task_id": state["task_id"],
        "status": state["status"],
        "done": state["done"],
        "total": state["total"],
        "progress": int((state["done"] / state["total"]) * 100) if state["total"] else 0,
        "analysis_id": state.get("analysis_id"),
        "result": state.get("result"),
        "error": state.get("error"),
        "cancelled": bool(state.get("cancelled")),
    }


def get_task(task_id: str) -> dict | None:
    state = _TASKS.get(task_id)
    return _public(state) if state else None


def stop_task(task_id: str) -> bool:
    state = _TASKS.get(task_id)
    if not state or state["status"] not in ("running", "queued"):
        return False
    state["cancelled"] = True
    state["status"] = "stopped"
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    return True


async def _run(
    task_id: str,
    *,
    project_id: int,
    target_type: str,
    target_ids: list[int],
    use_llm: bool,
    force: bool,
    user_id: int | None,
) -> None:
    state = _TASKS[task_id]
    state["status"] = "running"
    try:
        from app.services.quality_review import review_cases

        def _progress(done: int, total: int) -> None:
            state["done"] = max(0, min(int(done), int(total)))
            state["total"] = max(1, int(total))

        async with AsyncSessionLocal() as db:
            row, _cached = await review_cases(
                db, project_id=project_id, target_type=target_type,
                target_ids=target_ids, use_llm=use_llm, force=force,
                user_id=user_id, on_progress=_progress,
            )
        state["analysis_id"] = row.id
        state["result"] = row.result
        state["done"] = state["total"]
        state["status"] = "completed"
    except asyncio.CancelledError:
        state["status"] = "stopped"
        state["cancelled"] = True
    except Exception as exc:
        logger.warning("批量评审任务失败: %s", exc, exc_info=True)
        state["status"] = "failed"
        state["error"] = str(exc)[:500]
    finally:
        key = (project_id, user_id or 0)
        if _CONCURRENT.get(key) == task_id:
            _CONCURRENT.pop(key, None)


def start_review_task(
    *,
    project_id: int,
    target_type: str,
    target_ids: list[int],
    use_llm: bool,
    force: bool,
    user_id: int | None,
) -> str:
    key = (project_id, user_id or 0)
    if key in _CONCURRENT:
        raise RuntimeError("该项目已有评审任务在运行")
    task_id = f"qrev_{uuid.uuid4().hex}"
    state: dict[str, Any] = {
        "task_id": task_id,
        "status": "queued",
        "done": 0,
        "total": len(target_ids),
        "project_id": project_id,
        "user_id": user_id,
        "analysis_id": None,
        "result": None,
        "error": None,
        "cancelled": False,
    }
    _TASKS[task_id] = state
    task = asyncio.create_task(_run(
        task_id, project_id=project_id, target_type=target_type,
        target_ids=target_ids, use_llm=use_llm, force=force, user_id=user_id,
    ))
    state["task"] = task
    _CONCURRENT[key] = task_id
    return task_id
