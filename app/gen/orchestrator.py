"""Two-phase analysis orchestrators for text, image, and PDF inputs.

These functions decide whether the input should go through the text-based
two-phase pipeline (``two_phase_analyze``) or the image-based one
(``_analyze_image_two_phase`` / ``_analyze_pdf_two_phase``), and stitch the
FP extraction and TC generation steps together with progress reporting.
"""

import asyncio
import json as _json
import logging
import os

import openai

from app.gen.feature_extractor import (
    extract_functional_points,
    generate_test_cases_for_fps,
)
from app.gen.image_parser import encode_image
from app.gen.models import FunctionalPoint
from app.gen.pdf_parser import (
    extract_text_from_pdf,
    is_pdf_dual_layer,
    render_pdf_pages_to_images,
    validate_pdf,
)
from app.gen.prompts import FP_BATCH_SIZE

logger = logging.getLogger(__name__)

# Model token budget (kept in sync with model_client's hard cap).
MODEL_MAX_TOKENS = 8192


async def two_phase_analyze(
    text: str,
    progress_callback=None,
    project_description: str = "",
    prompts: dict = None,
) -> dict:
    """Orchestrate two-phase analysis: extract FPs then generate TCs per batch.

    Phase 1: Extract all functional points from the full document.
    Phase 2: Generate test cases for FPs in batches using FP descriptions
        as context (no longer sends the full document).

    Args:
        text: the extracted document text.
        progress_callback: optional callable(current, total, message).
        project_description: user-supplied project background context.

    Returns:
        dict with 'functional_points', 'test_cases', 'warnings'.
    """
    prompts = prompts or {}
    fp_prompt = prompts.get("fp_extract", {}).get("content") if isinstance(prompts.get("fp_extract"), dict) else prompts.get("fp_extract")
    tc_prompt = prompts.get("tc_generate", {}).get("content") if isinstance(prompts.get("tc_generate"), dict) else prompts.get("tc_generate")
    warnings: list[str] = []

    # Truncate text if it exceeds model's token budget
    total_tokens = int(len(text) * 1.5)
    max_tokens = 8192
    chunk_token_budget = int(max_tokens * 0.8)
    if total_tokens > chunk_token_budget:
        max_chars = int(chunk_token_budget / 1.5)
        logger.info("Doc too long (%d tokens > %d budget), truncating to %d chars",
                     total_tokens, chunk_token_budget, max_chars)
        text = text[:max_chars]

    if progress_callback:
        # Phase 1 = step 0. We don't know total yet (need FPs to know batch count).
        # Send with total=0 to indicate indeterminate progress for Phase 1.
        progress_callback(0, 0, "正在提取功能点清单")

    try:
        fps = await extract_functional_points(text=text, project_description=project_description, fp_prompt=fp_prompt)
        if not fps:
            warnings.append("No functional points extracted from document")
        logger.info("Phase 1: extracted %d functional points", len(fps))
    except (openai.OpenAIError, asyncio.TimeoutError, _json.JSONDecodeError, ValueError, RuntimeError) as e:
        # OpenAI SDK 错误 / 异步超时 / JSON 解析错误 / Pydantic 校验错误 / MCP 运行时错误
        logger.exception("Phase 1 (FP extraction) failed")
        return {"functional_points": [], "test_cases": [], "warnings": [f"FP extraction failed: {e}"], "error": True}

    # Phase 2: Generate test cases per FP batch
    if fps:
        num_batches = max(1, (len(fps) + FP_BATCH_SIZE - 1) // FP_BATCH_SIZE)
        total_steps = 1 + num_batches  # 1 for Phase 1 + N batches
        result = await generate_test_cases_for_fps(
            fps, project_description, progress_callback,
            phase1_offset=1, total_steps=total_steps, tc_prompt=tc_prompt,
        )
        warnings.extend(result.get("warnings", []))
        all_tcs = result["test_cases"]
    else:
        all_tcs = []

    return {
        "functional_points": fps,
        "test_cases": all_tcs,
        "warnings": warnings,
    }


async def _analyze_image_two_phase(file, progress_callback, project_description) -> dict:
    """Two-phase analysis for image files: extract FPs from image then generate TCs."""
    warnings: list[str] = []
    suffix = os.path.splitext(file.filename)[1].lstrip(".")
    b64 = encode_image(file)
    image_data = (suffix, b64)

    if progress_callback:
        progress_callback(0, 0, "正在从图片提取功能点")

    try:
        fps = await extract_functional_points(image_data=image_data, project_description=project_description)
        if not fps:
            warnings.append("No functional points extracted from image")
        logger.info("Phase 1 (image): extracted %d functional points", len(fps))
    except (openai.OpenAIError, asyncio.TimeoutError, _json.JSONDecodeError, ValueError, RuntimeError) as e:
        # OpenAI SDK 错误 / 异步超时 / JSON 解析错误 / Pydantic 校验错误 / MCP 运行时错误
        logger.exception("Phase 1 (image FP extraction) failed")
        return {"functional_points": [], "test_cases": [], "warnings": [f"Image FP extraction failed: {e}"], "error": True}

    # Phase 2: Generate test cases per FP batch
    if fps:
        num_batches = max(1, (len(fps) + FP_BATCH_SIZE - 1) // FP_BATCH_SIZE)
        total_steps = 1 + num_batches
        result = await generate_test_cases_for_fps(
            fps, project_description, progress_callback,
            phase1_offset=1, total_steps=total_steps,
        )
        warnings.extend(result.get("warnings", []))
        all_tcs = result["test_cases"]
    else:
        all_tcs = []

    return {
        "functional_points": fps,
        "test_cases": all_tcs,
        "warnings": warnings,
    }


async def _analyze_pdf_two_phase(file, progress_callback, project_description) -> dict:
    """Two-phase analysis for PDF files: auto-detect dual-layer vs scan-only."""
    warnings: list[str] = []

    # Validate PDF
    is_valid, error_msg = validate_pdf(file)
    if not is_valid:
        return {"functional_points": [], "test_cases": [], "warnings": [error_msg], "error": True}

    # Detect dual-layer
    if is_pdf_dual_layer(file):
        # Dual-layer: extract text and use text pipeline
        if progress_callback:
            progress_callback(0, 0, "正在从PDF提取文字")
        text = extract_text_from_pdf(file)
        if not text.strip():
            return {"functional_points": [], "test_cases": [], "warnings": ["PDF文件中无有效文字内容"], "error": True}
        return await two_phase_analyze(text, progress_callback, project_description)
    else:
        # Scan-only: render pages to images, extract FPs per page
        if progress_callback:
            progress_callback(0, 0, "正在将PDF页面转为图片")
        page_images = render_pdf_pages_to_images(file)
        if not page_images:
            return {"functional_points": [], "test_cases": [], "warnings": ["PDF文件中无有效页面"], "error": True}

        total_pages = len(page_images)
        all_fps: list[FunctionalPoint] = []

        for idx, (ext, b64) in enumerate(page_images):
            if progress_callback:
                progress_callback(idx, total_pages, f"正在分析第 {idx + 1}/{total_pages} 页")
            try:
                fps = await extract_functional_points(
                    image_data=(ext, b64),
                    project_description=project_description,
                    progress_callback=progress_callback,
                )
                if fps:
                    # Re-number FPs to be globally sequential
                    for fp in fps:
                        fp.id = len(all_fps) + 1
                        fp.session_id = ""
                    all_fps.extend(fps)
            except (openai.OpenAIError, asyncio.TimeoutError, _json.JSONDecodeError, ValueError, RuntimeError) as e:
                # 单页失败不影响整体 PDF 流程，仅作为 warning
                logger.warning("PDF page %d FP extraction failed: %s", idx + 1, e)
                warnings.append(f"第 {idx + 1} 页功能点提取失败: {e}")

        logger.info("PDF scan-only analysis: extracted %d functional points from %d pages", len(all_fps), total_pages)

        # Phase 2: Generate test cases from merged FPs
        if all_fps:
            num_batches = max(1, (len(all_fps) + FP_BATCH_SIZE - 1) // FP_BATCH_SIZE)
            total_steps = total_pages + num_batches
            result = await generate_test_cases_for_fps(
                all_fps, project_description, progress_callback,
                phase1_offset=total_pages, total_steps=total_steps,
            )
            warnings.extend(result.get("warnings", []))
            all_tcs = result["test_cases"]
        else:
            all_tcs = []

        return {
            "functional_points": all_fps,
            "test_cases": all_tcs,
            "warnings": warnings,
        }


__all__ = [
    "MODEL_MAX_TOKENS",
    "two_phase_analyze",
    "_analyze_image_two_phase",
    "_analyze_pdf_two_phase",
]
