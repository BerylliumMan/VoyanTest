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
from app.gen.model_client import get_context_budget
from app.gen.prompts import FP_BATCH_SIZE

logger = logging.getLogger(__name__)


async def two_phase_analyze(
    text: str,
    progress_callback=None,
    project_description: str = "",
    prompts: dict = None,
    db = None,
    agent_id: int | None = None,
    skills: list | None = None,
    tc_prompt_key: str | None = None,
) -> dict:
    """Two-phase analysis using Pipeline (generation AgentDefinition)."""
    from app.gen.agents.pipeline import Pipeline
    config = {
        "project_description": project_description,
        "db": db,
        "agent_type": "generation",
        "agent_id": agent_id,
        "skills": skills or [],
        "tc_prompt_key": tc_prompt_key,
        "progress_callback": progress_callback,
    }
    if prompts:
        config["prompts"] = prompts
    pipeline = Pipeline(config)
    return await pipeline.run(text)


async def _analyze_image_two_phase(file, progress_callback, project_description) -> dict:
    """Two-phase analysis for image files: extract FPs from image then generate TCs."""
    warnings: list[str] = []
    suffix = os.path.splitext(file.filename)[1].lstrip(".")
    b64 = await asyncio.to_thread(encode_image, file)
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
    is_valid, error_msg = await asyncio.to_thread(validate_pdf, file)
    if not is_valid:
        return {"functional_points": [], "test_cases": [], "warnings": [error_msg], "error": True}

    # Detect dual-layer
    if await asyncio.to_thread(is_pdf_dual_layer, file):
        # Dual-layer: extract text and use text pipeline
        if progress_callback:
            progress_callback(0, 0, "正在从PDF提取文字")
        text = await asyncio.to_thread(extract_text_from_pdf, file)
        if not text.strip():
            return {"functional_points": [], "test_cases": [], "warnings": ["PDF文件中无有效文字内容"], "error": True}
        return await two_phase_analyze(text, progress_callback, project_description)
    else:
        # Scan-only: render pages to images, extract FPs per page
        if progress_callback:
            progress_callback(0, 0, "正在将PDF页面转为图片")
        page_images = await asyncio.to_thread(render_pdf_pages_to_images, file)
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

    # ── 质量校验 ──────────────────────────────────────────────────────────
    from app.gen.agents.validator import validate_test_cases
    v_result = validate_test_cases(all_tcs, all_fps)
    warnings.extend(v_result["warnings"])
    if not v_result["passed"]:
        warnings.append(f"质量校验: {v_result['valid_count']}/{len(all_tcs)} 个用例通过")
    all_tcs = v_result["valid_cases"]

    return {
            "functional_points": all_fps,
            "test_cases": all_tcs,
            "warnings": warnings,
        }


__all__ = [
    "two_phase_analyze",
    "_analyze_image_two_phase",
    "_analyze_pdf_two_phase",
]
