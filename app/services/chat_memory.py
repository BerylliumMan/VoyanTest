"""044：记忆接口的读写编排（薄封装 core.chat.memory，便于 API 与测试复用）。"""

from __future__ import annotations

from core.chat import memory

BODY_PREVIEW_CHARS = 200


def list_for_project(project_id: int) -> list[dict]:
    return [
        {
            "name": e.name,
            "description": e.description,
            "type": e.type,
            "written_at": e.written_at,
            "body_preview": (e.body or "")[:BODY_PREVIEW_CHARS],
        }
        for e in memory.list_memories(project_id)
    ]


def delete_for_project(project_id: int, name: str) -> bool:
    return memory.delete_memory(project_id, name)
