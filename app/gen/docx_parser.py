"""DOCX parsers — plain text and ordered multimodal blocks (text + images)."""
from __future__ import annotations

import base64
import logging
from typing import Any, BinaryIO

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

logger = logging.getLogger(__name__)

# Limit embedded images per document for Phase-1 multimodal calls.
MAX_DOCX_IMAGES = 20

_NSMAP = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
}


def _ext_from_content_type(content_type: str, partname: str) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return "png"
    if "jpeg" in ct or "jpg" in ct:
        return "jpeg"
    if "gif" in ct:
        return "gif"
    if "webp" in ct:
        return "webp"
    name = (partname or "").lower()
    for ext in ("png", "jpeg", "jpg", "gif", "webp"):
        if name.endswith("." + ext):
            return "jpeg" if ext == "jpg" else ext
    return "png"


def _table_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _blip_rids(element) -> list[str]:
    """Collect image relationship ids from a paragraph/table OOXML element."""
    rids: list[str] = []
    # DrawingML blips
    for blip in element.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        rid = blip.get(qn("r:embed")) or blip.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        )
        if rid:
            rids.append(rid)
    # Legacy VML imagedata
    for imagedata in element.findall(".//{urn:schemas-microsoft-com:vml}imagedata"):
        rid = imagedata.get(qn("r:id")) or imagedata.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        if rid:
            rids.append(rid)
    return rids


def _flush_text(parts: list[dict[str, Any]], buf: list[str]) -> None:
    text = "\n".join(t for t in buf if t and t.strip()).strip()
    buf.clear()
    if text:
        parts.append({"type": "text", "text": text})


def extract_ordered_blocks(
    file: BinaryIO,
    *,
    max_images: int = MAX_DOCX_IMAGES,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract document body as ordered text/image blocks.

    Returns:
        (blocks, warnings)
        blocks: [{"type":"text","text":...} | {"type":"image","ext":"png","b64":...}, ...]
    """
    doc = Document(file)
    parts: list[dict[str, Any]] = []
    text_buf: list[str] = []
    warnings: list[str] = []
    image_count = 0
    skipped_images = 0

    def add_image(rid: str) -> None:
        nonlocal image_count, skipped_images
        if image_count >= max_images:
            skipped_images += 1
            return
        try:
            rel = doc.part.rels[rid]
        except KeyError:
            logger.debug("missing relationship %s", rid)
            return
        try:
            blob = rel.target_part.blob
            content_type = getattr(rel.target_part, "content_type", "") or ""
            partname = str(getattr(rel.target_part, "partname", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load image part %s: %s", rid, exc)
            warnings.append(f"无法读取嵌入图片 {rid}: {exc}")
            return
        if not blob:
            return
        _flush_text(parts, text_buf)
        ext = _ext_from_content_type(content_type, partname)
        parts.append({
            "type": "image",
            "ext": ext,
            "b64": base64.b64encode(blob).decode("ascii"),
        })
        image_count += 1

    for child in doc.element.body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            para = Paragraph(child, doc)
            txt = (para.text or "").strip()
            if txt:
                text_buf.append(txt)
            for rid in _blip_rids(child):
                add_image(rid)
        elif tag == qn("w:tbl"):
            table = Table(child, doc)
            txt = _table_text(table).strip()
            if txt:
                text_buf.append(txt)
            for rid in _blip_rids(child):
                add_image(rid)

    _flush_text(parts, text_buf)

    if skipped_images:
        warnings.append(
            f"文档嵌入图片超过上限 {max_images} 张，已跳过 {skipped_images} 张"
        )
    return parts, warnings


def extract_text(file: BinaryIO) -> str:
    """Backward-compatible plain-text extraction (paragraphs + tables, no images)."""
    blocks, _ = extract_ordered_blocks(file, max_images=0)
    return "\n".join(
        b["text"] for b in blocks if b.get("type") == "text" and b.get("text")
    )


def blocks_to_plain_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten ordered blocks to a text-only string (images become placeholders)."""
    lines: list[str] = []
    img_i = 0
    for b in blocks:
        if b.get("type") == "text":
            t = (b.get("text") or "").strip()
            if t:
                lines.append(t)
        elif b.get("type") == "image":
            img_i += 1
            lines.append(f"[图片{img_i}]")
    return "\n".join(lines)


__all__ = [
    "MAX_DOCX_IMAGES",
    "extract_ordered_blocks",
    "extract_text",
    "blocks_to_plain_text",
]
