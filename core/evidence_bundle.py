# core/evidence_bundle.py
# 041 US4：证据文件收集与安全 zip 打包（只允许 reports/ 下普通文件）。
from __future__ import annotations

import re
import zipfile
from pathlib import Path

_EVIDENCE_RE = re.compile(
    r"reports/[^\s;)\]]+\.(?:png|jpe?g|webm|pdf|txt|csv|json|zip)",
    re.I,
)


def extract_evidence_paths(texts: list[str]) -> list[str]:
    out: list[str] = []
    for text in texts or []:
        for match in _EVIDENCE_RE.findall(str(text or "")):
            if match not in out:
                out.append(match)
    return out


def _safe_file(path: str, root: Path) -> Path | None:
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        resolved = p.resolve()
    except OSError:
        return None
    if resolved.is_symlink() or not resolved.is_file():
        return None
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def collect_evidence_files(paths: list[str], *, root: Path) -> list[Path]:
    out: list[Path] = []
    for path in paths or []:
        safe = _safe_file(path, root)
        if safe is not None and safe not in out:
            out.append(safe)
    return out


def build_evidence_zip(
    paths: list[str],
    *,
    dest: Path,
    root: Path,
    max_bytes: int = 100 * 1024 * 1024,
) -> tuple[Path, list[str]]:
    files = collect_evidence_files(paths, root=root)
    total = 0
    selected: list[Path] = []
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if total + size > max_bytes:
            break
        total += size
        selected.append(f)
    if not selected:
        raise ValueError("没有可打包的证据文件")
    dest.parent.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in selected:
            name = f.name
            if name in used:
                name = f"{f.stem}_{len(used)}{f.suffix}"
            used.add(name)
            zf.write(f, arcname=name)
    return dest, [f.name for f in selected]
