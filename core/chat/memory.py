"""044 US4：项目级文件记忆。

每条记忆一个 Markdown 文件，frontmatter 存名称/描述/类型/写入时间；
`MEMORY.md` 只是索引（由 load_index 动态生成，不落盘）。
检索用关键词打分（中文 2 字滑窗 + 英文词），不引入向量库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path("data/memory")
INDEX_MAX_LINES = 200
BODY_MAX_CHARS = 8192
RECALL_MIN_SCORE = 1


@dataclass
class MemoryEntry:
    name: str
    description: str
    type: str
    body: str
    written_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-一-鿿]", "-", str(name or "")).strip("-.")
    return cleaned or "memory"


def memory_root(project_id: int | None) -> Path:
    return _ROOT / f"project-{int(project_id) if project_id else 0}"


def _render(entry: MemoryEntry) -> str:
    written = entry.written_at or _now_iso()
    body = (entry.body or "")[:BODY_MAX_CHARS]
    return (
        "---\n"
        f"name: {_safe_name(entry.name)}\n"
        f"description: {str(entry.description or '').strip()}\n"
        f"type: {str(entry.type or 'project').strip()}\n"
        f"written_at: {written}\n"
        "---\n"
        f"{body}\n"
    )


def _parse(path: Path) -> MemoryEntry | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = parts[2].lstrip("\n")
    return MemoryEntry(
        name=meta.get("name") or path.stem,
        description=meta.get("description", ""),
        type=meta.get("type", "project"),
        body=body.strip(),
        written_at=meta.get("written_at", ""),
    )


def save_memory(project_id: int | None, entry: MemoryEntry) -> Path:
    root = memory_root(project_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_safe_name(entry.name)}.md"
    if not entry.written_at:
        entry.written_at = _now_iso()
    path.write_text(_render(entry), encoding="utf-8")
    return path


def delete_memory(project_id: int | None, name: str) -> bool:
    path = memory_root(project_id) / f"{_safe_name(name)}.md"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def list_memories(project_id: int | None) -> list[MemoryEntry]:
    root = memory_root(project_id)
    if not root.exists():
        return []
    entries = [e for p in sorted(root.glob("*.md")) if (e := _parse(p)) is not None]
    entries.sort(key=lambda e: e.written_at, reverse=True)
    return entries


def load_index(project_id: int | None) -> str:
    entries = list_memories(project_id)
    if not entries:
        return ""
    lines = [f"- [{e.name}] {e.description}（{e.type}）" for e in entries]
    return "\n".join(lines[:INDEX_MAX_LINES])


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    toks: set[str] = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    cjk = re.findall(r"[一-鿿]", lowered)
    for i in range(len(cjk) - 1):
        toks.add(cjk[i] + cjk[i + 1])
    return toks


def recall(project_id: int | None, query: str, *, limit: int = 5) -> list[MemoryEntry]:
    q = _tokens(query)
    if not q:
        return []
    scored: list[tuple[int, MemoryEntry]] = []
    for entry in list_memories(project_id):
        hay = _tokens(f"{entry.name} {entry.description} {entry.body}")
        score = len(q & hay)
        if score >= RECALL_MIN_SCORE:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[: max(1, limit)]]
