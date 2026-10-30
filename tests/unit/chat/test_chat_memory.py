"""044 US4：项目文件记忆（先红）。"""

from core.chat.memory import (
    MemoryEntry,
    delete_memory,
    load_index,
    memory_root,
    recall,
    save_memory,
)


def test_save_and_index_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)
    save_memory(7, MemoryEntry(name="run-habit", description="用户偏好先跑冒烟", type="feedback", body="先跑 smoke"))
    index = load_index(7)
    assert "run-habit" in index and "用户偏好先跑冒烟" in index
    assert (memory_root(7) / "run-habit.md").exists()


def test_recall_scores_by_keyword(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)
    save_memory(1, MemoryEntry(name="a", description="环境不可达排查", type="project", body="检查 agent 在线"))
    save_memory(1, MemoryEntry(name="b", description="用例命名规范", type="project", body="中文标题"))
    hits = recall(1, "环境不可达怎么排查", limit=2)
    assert hits and hits[0].name == "a"


def test_delete_memory_removes_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)
    save_memory(2, MemoryEntry(name="x", description="d", type="user", body="b"))
    assert delete_memory(2, "x") is True
    assert delete_memory(2, "x") is False
