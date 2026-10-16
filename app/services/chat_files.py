"""对话附件：受控路径解析与文本提取（docx / pdf / xlsx / md）。

035+：附件内容提取供两条链路使用：
- 对话上下文注入（orchestrator 发送时提取，存 refs.text）；
- 文档驱动探索（browse_generate document_ref → 探索目标参考文档）。
图片附件不提取文本（返回说明性 error，不静默）。
"""
from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)

CHAT_UPLOAD_DIR = os.path.join("data", "chat_uploads")

# 单文件提取上限（超过只标注、不解析，避免拖垮请求）
MAX_EXTRACT_BYTES = 10 * 1024 * 1024


def resolve_chat_file(file_ref: str) -> Optional[str]:
    """把附件 file_ref 解析为受控路径（仅允许 data/chat_uploads 之下）。"""
    if not file_ref:
        return None
    normalized = os.path.normpath(file_ref)
    base = os.path.normpath(CHAT_UPLOAD_DIR)
    if not normalized.startswith(base + os.sep):
        return None
    if not os.path.isfile(normalized):
        return None
    return normalized


def extract_attachment_text(path: str, *, max_chars: int = 6000) -> tuple[str, str]:
    """提取附件文本；返回 (text, error)。

    error 非空表示未能提取（text 必为空）；两者同时为空表示文件为空。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return "", f"无法读取文件：{e}"
    if size > MAX_EXTRACT_BYTES:
        return "", f"文件过大（>{MAX_EXTRACT_BYTES // 1024 // 1024}MB），未提取内容"

    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if ext == ".docx":
            from app.gen.docx_parser import blocks_to_plain_text, extract_ordered_blocks

            blocks, _warn = extract_ordered_blocks(BytesIO(data))
            text = blocks_to_plain_text(blocks)
        elif ext == ".pdf":
            from app.gen.pdf_parser import extract_text_from_pdf, is_pdf_dual_layer

            bio = BytesIO(data)
            if not is_pdf_dual_layer(bio):
                return "", "扫描版 PDF（无文字层），未提取文本"
            bio.seek(0)
            text = extract_text_from_pdf(bio)
        elif ext == ".xlsx":
            from app.gen.xlsx_parser import extract_text_from_xlsx

            text = extract_text_from_xlsx(BytesIO(data))
        elif ext == ".md":
            text = data.decode("utf-8", errors="replace")
        elif ext in (".png", ".jpg", ".jpeg"):
            return "", "图片附件（未提取文本）"
        else:
            return "", f"不支持的文件类型：{ext}"
    except Exception as e:  # noqa: BLE001
        logger.warning("attachment extract failed %s: %s", path, e)
        return "", f"解析失败：{e}"

    text = (text or "").strip()
    if not text:
        return "", "文件为空或未提取到文本"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（内容过长，已截断）"
    return text, ""


__all__ = ["resolve_chat_file", "extract_attachment_text", "CHAT_UPLOAD_DIR"]
