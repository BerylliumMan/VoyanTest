"""Persist uploaded generation source files for later retry."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_GEN_UPLOAD_ROOT = Path(os.environ.get("VOYANTEST_GEN_UPLOAD_DIR", "data/gen_uploads"))
_SAFE_NAME = re.compile(r"[^\w.\-()\u4e00-\u9fff]+", re.UNICODE)


def session_upload_dir(session_id: str) -> Path:
    return _GEN_UPLOAD_ROOT / session_id


def _safe_filename(name: str, index: int) -> str:
    base = (name or f"file_{index}").replace("/", "_").replace("\\", "_").strip()
    base = _SAFE_NAME.sub("_", base) or f"file_{index}"
    return f"{index:02d}_{base}"


def save_session_files(
    session_id: str,
    filenames: list[str],
    file_bytes: list[bytes],
) -> Path:
    """Write uploaded bytes under ``data/gen_uploads/{session_id}/``."""
    if len(filenames) != len(file_bytes):
        raise ValueError("filenames and file_bytes length mismatch")
    dest = session_upload_dir(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    stored_names: list[str] = []
    for i, (name, data) in enumerate(zip(filenames, file_bytes)):
        stored = _safe_filename(name, i)
        (dest / stored).write_bytes(data)
        stored_names.append(stored)
    meta = {"filenames": filenames, "stored_names": stored_names}
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dest


def session_has_uploads(session_id: str) -> bool:
    """True when persisted uploads exist (enough for retry)."""
    return (session_upload_dir(session_id) / "meta.json").is_file()


def load_session_files(session_id: str) -> tuple[list[str], list[bytes]] | None:
    """Return ``(filenames, bytes_list)`` or None if uploads are missing."""
    dest = session_upload_dir(session_id)
    meta_path = dest / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        filenames = list(meta.get("filenames") or [])
        stored_names = list(meta.get("stored_names") or [])
        if not filenames or len(filenames) != len(stored_names):
            return None
        blobs: list[bytes] = []
        for stored in stored_names:
            path = dest / stored
            if not path.is_file():
                return None
            blobs.append(path.read_bytes())
        return filenames, blobs
    except Exception:
        logger.warning("load gen uploads failed session_id=%s", session_id, exc_info=True)
        return None


def delete_session_files(session_id: str) -> None:
    dest = session_upload_dir(session_id)
    if not dest.exists():
        return
    try:
        import shutil

        shutil.rmtree(dest, ignore_errors=True)
    except Exception:
        logger.debug("delete gen uploads failed session_id=%s", session_id, exc_info=True)
