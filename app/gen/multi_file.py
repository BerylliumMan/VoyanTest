"""Extract text and ordered multimodal parts from a mixed batch of uploaded files.

Supports ``.docx`` (ordered text + embedded images), ``.md``, ``.pdf``
(auto-detect dual-layer vs scan-only) and ``.png`` / ``.jpg`` / ``.jpeg``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.gen.constants import ALLOWED_EXTENSIONS, MAX_FILES, MAX_TOTAL_SIZE
from app.gen.docx_parser import blocks_to_plain_text, extract_ordered_blocks
from app.gen.image_parser import encode_image
from app.gen.md_parser import extract_text_from_md
from app.gen.pdf_parser import (
    extract_text_from_pdf,
    is_pdf_dual_layer,
    render_pdf_pages_to_images,
)

logger = logging.getLogger(__name__)


def _file_header(idx: int, filename: str) -> dict[str, Any]:
    return {"type": "text", "text": f"===== 文件{idx + 1}: {filename} ====="}


async def extract_multi_file_content(
    files,
    filenames,
    progress_callback=None,
) -> tuple[str, list[str], list[str], list[dict[str, Any]]]:
    """从多个文件中提取内容。

    Returns:
        (combined_text, filenames, warnings, content_parts)
        - combined_text: 拼接后的纯文本（含图片占位），兼容旧调用方
        - content_parts: 有序多模态块，供 Phase1 vision 使用
    """
    warnings: list[str] = []
    content_parts: list[dict[str, Any]] = []

    if len(files) > MAX_FILES:
        raise ValueError(f"最多上传 {MAX_FILES} 个文件，当前选择了 {len(files)} 个")

    total_size = 0
    for f in files:
        f.seek(0, 2)
        total_size += f.tell()
        f.seek(0)
    if total_size > MAX_TOTAL_SIZE:
        raise ValueError(f"文件总大小超过 50MB 限制（当前 {total_size / 1024 / 1024:.1f}MB）")

    total_files = len(files)

    for idx, file in enumerate(files):
        filename = filenames[idx] if idx < len(filenames) else f"file_{idx + 1}"
        ext = os.path.splitext(filename)[1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型: {ext}，仅支持 .docx、.md、.png、.jpg、.jpeg、.pdf")

        if progress_callback:
            progress_callback(idx, total_files, f"正在提取: {filename} ({idx + 1}/{total_files})")

        try:
            file.seek(0)
            if ext == ".docx":
                blocks, doc_warnings = await asyncio.to_thread(extract_ordered_blocks, file)
                warnings.extend(doc_warnings)
                if not blocks:
                    warnings.append(f"文件 {filename} 为空，已跳过")
                    continue
                content_parts.append(_file_header(idx, filename))
                content_parts.extend(blocks)

            elif ext == ".md":
                text = await asyncio.to_thread(extract_text_from_md, file)
                if not text.strip():
                    warnings.append(f"文件 {filename} 为空，已跳过")
                    continue
                content_parts.append(_file_header(idx, filename))
                content_parts.append({"type": "text", "text": text})

            elif ext == ".pdf":
                if await asyncio.to_thread(is_pdf_dual_layer, file):
                    text = await asyncio.to_thread(extract_text_from_pdf, file)
                    if text.strip():
                        content_parts.append(_file_header(idx, filename))
                        content_parts.append({"type": "text", "text": text})
                    else:
                        warnings.append(f"PDF 文件 {filename} 无有效文字，尝试图片模式")
                        file.seek(0)
                        page_images = await asyncio.to_thread(render_pdf_pages_to_images, file)
                        if not page_images:
                            warnings.append(f"PDF文件 {filename} 无有效页面，已跳过")
                            continue
                        content_parts.append(_file_header(idx, filename))
                        for page_idx, (pext, pb64) in enumerate(page_images):
                            content_parts.append({
                                "type": "text",
                                "text": f"===== {filename} 第{page_idx + 1}页 =====",
                            })
                            content_parts.append({"type": "image", "ext": pext, "b64": pb64})
                else:
                    page_images = await asyncio.to_thread(render_pdf_pages_to_images, file)
                    if not page_images:
                        warnings.append(f"PDF文件 {filename} 无有效页面，已跳过")
                        continue
                    content_parts.append(_file_header(idx, filename))
                    for page_idx, (pext, pb64) in enumerate(page_images):
                        content_parts.append({
                            "type": "text",
                            "text": f"===== {filename} 第{page_idx + 1}页 =====",
                        })
                        content_parts.append({"type": "image", "ext": pext, "b64": pb64})

            elif ext in (".png", ".jpg", ".jpeg"):
                suffix = ext.lstrip(".")
                b64 = await asyncio.to_thread(encode_image, file)
                content_parts.append(_file_header(idx, filename))
                content_parts.append({"type": "image", "ext": suffix, "b64": b64})

        except Exception as e:  # noqa: BLE001
            logger.warning("文件 %s 提取失败: %s", filename, e)
            warnings.append(f"文件 {filename} 提取失败: {e}")

        if idx < total_files - 1:
            await asyncio.sleep(0.5)

    combined = blocks_to_plain_text(content_parts)
    if not combined.strip() and not any(p.get("type") == "image" for p in content_parts):
        raise ValueError("所有文件均未提取到有效内容")

    return combined, filenames, warnings, content_parts


__all__ = ["extract_multi_file_content"]
