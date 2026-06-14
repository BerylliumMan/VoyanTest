"""Extract text and FP summaries from a mixed batch of uploaded files.

Supports ``.docx``, ``.md``, ``.pdf`` (auto-detect dual-layer vs scan-only)
and ``.png`` / ``.jpg`` / ``.jpeg``. Documents contribute raw text; images
and scanned PDFs contribute per-page functional-point summaries produced by
:mod:`app.gen.feature_extractor`. The result is a single concatenated string
ready to feed into :func:`app.gen.orchestrator.two_phase_analyze`.
"""

import logging
import os
import time

from app.gen.constants import ALLOWED_EXTENSIONS, MAX_FILES, MAX_RETRIES, MAX_TOTAL_SIZE, RETRY_DELAY
from app.gen.docx_parser import extract_text
from app.gen.feature_extractor import extract_functional_points
from app.gen.image_parser import encode_image
from app.gen.md_parser import extract_text_from_md
from app.gen.pdf_parser import (
    extract_text_from_pdf,
    is_pdf_dual_layer,
    render_pdf_pages_to_images,
)

logger = logging.getLogger(__name__)


def extract_multi_file_content(files, filenames, progress_callback=None) -> tuple[str, list[str], list[str]]:
    """从多个文件中提取内容并拼接为一个大文本。

    Args:
        files: 文件对象列表（SpooledTemporaryFile 等，同步 read/write）
        filenames: 对应文件名列表
        progress_callback: 可选 callable(current, total, message)

    Returns:
        (combined_text, filenames, warnings)
        - combined_text: 拼接后的文本
        - filenames: 所有文件名列表
        - warnings: 警告信息列表
    """
    warnings: list[str] = []
    text_parts: list[str] = []
    image_fp_parts: list[str] = []

    # 校验文件数量
    if len(files) > MAX_FILES:
        raise ValueError(f"最多上传 {MAX_FILES} 个文件，当前选择了 {len(files)} 个")

    # 校验总大小
    total_size = 0
    for f in files:
        f.seek(0, 2)  # SEEK_END
        total_size += f.tell()
        f.seek(0)     # SEEK_SET
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
                text = extract_text(file)
                if not text.strip():
                    warnings.append(f"文件 {filename} 为空，已跳过")
                    continue
                text_parts.append(f"===== 文件{idx + 1}: {filename} =====\n{text}")

            elif ext == ".md":
                text = extract_text_from_md(file)
                if not text.strip():
                    warnings.append(f"文件 {filename} 为空，已跳过")
                    continue
                text_parts.append(f"===== 文件{idx + 1}: {filename} =====\n{text}")

            elif ext == ".pdf":
                # 双层 PDF 提取文本，扫描 PDF 提取图片功能点
                if is_pdf_dual_layer(file):
                    text = extract_text_from_pdf(file)
                    if not text.strip():
                        warnings.append(f"PDF 文件 {filename} 无有效文字，尝试图片模式")
                        file.seek(0)
                        page_images = render_pdf_pages_to_images(file)
                        if page_images:
                            for page_idx, (pext, pb64) in enumerate(page_images):
                                fps = extract_functional_points(
                                    image_data=(pext, pb64),
                                    progress_callback=progress_callback,
                                )
                                fp_text = "\n".join(
                                    f"- 【{fp.module}】{fp.name}({fp.category}): {fp.description}"
                                    for fp in fps
                                ) if fps else "（未提取到功能点）"
                                image_fp_parts.append(
                                    f"===== 图片功能点提取: {filename} 第{page_idx + 1}页 =====\n{fp_text}"
                                )
                        else:
                            warnings.append(f"PDF文件 {filename} 无有效页面，已跳过")
                        continue
                    text_parts.append(f"===== 文件{idx + 1}: {filename} =====\n{text}")
                else:
                    page_images = render_pdf_pages_to_images(file)
                    if not page_images:
                        warnings.append(f"PDF文件 {filename} 无有效页面，已跳过")
                        continue
                    for page_idx, (pext, pb64) in enumerate(page_images):
                        fps = extract_functional_points(
                            image_data=(pext, pb64),
                            progress_callback=progress_callback,
                        )
                        fp_text = "\n".join(
                            f"- 【{fp.module}】{fp.name}({fp.category}): {fp.description}"
                            for fp in fps
                        ) if fps else "（未提取到功能点）"
                        image_fp_parts.append(
                            f"===== 图片功能点提取: {filename} 第{page_idx + 1}页 =====\n{fp_text}"
                        )

            elif ext in (".png", ".jpg", ".jpeg"):
                suffix = ext.lstrip(".")
                b64 = encode_image(file)
                fps = []
                for attempt in range(MAX_RETRIES):
                    try:
                        fps = extract_functional_points(
                            image_data=(suffix, b64),
                            progress_callback=progress_callback,
                        )
                        break
                    except Exception as e:
                        if "timed out" in str(e).lower() and attempt < MAX_RETRIES - 1:
                            logger.warning("图片 %s 分析超时，第 %d 次重试...", filename, attempt + 1)
                            time.sleep(RETRY_DELAY * (attempt + 1))
                        else:
                            raise
                fp_text = "\n".join(
                    f"- 【{fp.module}】{fp.name}({fp.category}): {fp.description}"
                    for fp in fps
                ) if fps else "（未提取到功能点）"
                image_fp_parts.append(
                    f"===== 图片功能点提取: {filename} =====\n{fp_text}"
                )

        except Exception as e:
            logger.warning("文件 %s 提取失败: %s", filename, e)
            warnings.append(f"文件 {filename} 提取失败: {e}")

    # 拼接：文档文本在前，图片功能点描述在后
    combined = "\n\n".join(text_parts + image_fp_parts)

    if not combined.strip():
        raise ValueError("所有文件均未提取到有效内容")

    return combined, filenames, warnings


__all__ = ["extract_multi_file_content"]
