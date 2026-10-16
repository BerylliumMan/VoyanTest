"""上传路径的 ASCII 安全化（MCP 兼容层）。

背景（2026-10-16 实测）：@playwright/mcp 的 ``browser_file_upload`` / ``browser_drop``
在 paths 含非 ASCII 字符（如中文文件名）时**静默失败**——不报错、chooser 已打开但
files 未写入、change 事件不触发。ASCII 路径同一流程正常。容器与客户端两端的 MCP
均有此行为（0.0.77/0.0.80/0.0.83 实测一致）。

对策：调用前把非 ASCII 路径复制为 ASCII 临时路径投喂，调用后清理。原始文件
仍保留在原位（只读复制，不改动用户文件）。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid

logger = logging.getLogger(__name__)

_PREFIX = "vt_upload_"


def _pick_ascii_base() -> str:
    """选一个 ASCII 可写的临时目录（Windows 用户名可能含中文，逐级回退）。"""
    candidates: list[str] = []
    env_tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if env_tmp:
        candidates.append(env_tmp)
    candidates.append(tempfile.gettempdir())
    sysdrive = os.environ.get("SystemDrive")
    if sysdrive:
        candidates.append(os.path.join(sysdrive + os.sep, "ProgramData", "voyantest", "upload_tmp"))
    for c in candidates:
        if not c or not c.isascii():
            continue
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, f"{_PREFIX}probe_{uuid.uuid4().hex[:8]}")
            with open(probe, "wb"):
                pass
            os.remove(probe)
            return c
        except OSError:
            continue
    return tempfile.gettempdir()


def prepare_upload_args(args: dict) -> tuple[dict, list[str]]:
    """把 args['paths'] 中的非 ASCII 路径替换为 ASCII 临时副本。

    返回 (新 args, 临时文件列表)。临时文件应在调用结束后经
    ``cleanup_upload_temps`` 清理。
    """
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        return args, []
    new_paths: list[str] = []
    temps: list[str] = []
    for p in paths:
        if isinstance(p, str) and p and not p.isascii() and os.path.isfile(p):
            tmp = _copy_to_ascii(p)
            if tmp:
                new_paths.append(tmp)
                temps.append(tmp)
                continue
            logger.warning("upload path ascii-copy failed, fallback to original: %s", p)
        new_paths.append(p)
    if not temps:
        return args, []
    logger.info("upload paths ascii-safe: %d/%d replaced", len(temps), len(paths))
    return {**args, "paths": new_paths}, temps


def _copy_to_ascii(path: str) -> str | None:
    ext = os.path.splitext(path)[1]
    if not ext.isascii():
        ext = ""
    try:
        base = _pick_ascii_base()
        tmp = os.path.join(base, f"{_PREFIX}{uuid.uuid4().hex}{ext}")
        shutil.copyfile(path, tmp)
        return tmp
    except OSError as e:
        logger.warning("copy upload file failed: %s (%s)", path, e)
        return None


def cleanup_upload_temps(temps: list[str]) -> None:
    for t in temps or []:
        try:
            os.remove(t)
        except OSError:
            logger.debug("cleanup upload temp failed: %s", t)


__all__ = ["prepare_upload_args", "cleanup_upload_temps"]
