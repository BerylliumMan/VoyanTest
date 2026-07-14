"""Phase 1 / Phase 2 model calls.

Phase 1 extracts functional points from a document or image; Phase 2 turns
each batch of functional points into test cases. Both phases go through
``call_model`` from :mod:`app.gen.model_client`.
"""

import asyncio
import json as _json
import logging
import time

import openai
from markupsafe import escape

from app.gen.constants import MAX_RETRIES, RETRY_DELAY
from app.gen.csv_generator import CSV_HEADER
from app.gen.model_client import call_model
from app.gen.models import FunctionalPoint, TestCase
from app.gen.prompts import FP_BATCH_SIZE, FP_EXTRACT_PROMPT, TC_GENERATE_PROMPT
from app.gen.response_parser import _parse_fps_from_text, _parse_tcs_from_text

logger = logging.getLogger(__name__)


async def extract_functional_points(
    text: str = None,
    image_data: tuple = None,
    project_description: str = "",
    progress_callback=None,
    fp_prompt: str = None,
) -> list[FunctionalPoint]:
    """Extract functional points from document text or image.

    Phase 1 of two-phase pipeline.

    Args:
        text: Document text content (for .docx files).
        image_data: Tuple of (file_extension, base64_encoded_image) for image files.
        project_description: User-supplied project background.
        progress_callback: Optional callable(current, total, message) — forwarded by caller.
        fp_prompt: Custom FP extraction prompt (None = use default).

    Exactly one of text or image_data must be provided.
    """
    fp_prompt = fp_prompt or FP_EXTRACT_PROMPT
    desc_prefix = ""
    if project_description:
        desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

    if image_data:
        suffix, b64 = image_data
        prompt = desc_prefix + fp_prompt
        if progress_callback:
            progress_callback(0, 0, "正在分析图片提取功能点")
        content = await call_model([
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请分析此界面原型图中的所有功能点和UI元素："},
                    {"type": "image_url", "image_url": {"url": f"data:image/{suffix};base64,{b64}"}},
                ],
            },
        ])
    else:
        prompt = desc_prefix + fp_prompt
        if progress_callback:
            progress_callback(0, 0, "正在分析文档提取功能点")
        content = await call_model([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ])

    fps = _parse_fps_from_text(content)
    if progress_callback:
        progress_callback(0, 0, f"提取到 {len(fps)} 个功能点")
    return fps


async def generate_test_cases_for_fps(
    fps: list[FunctionalPoint],
    project_description: str,
    progress_callback=None,
    phase1_offset=1,
    total_steps=1,
    tc_prompt: str = None,
) -> dict:
    """Generate test cases for functional points in batches of 5-8.

    Phase 2 of two-phase pipeline. Groups FPs into batches, calls
    TC_GENERATE_PROMPT per batch with structured FP descriptions as context
    (no longer sends the full document), and merges all test cases.

    Args:
        phase1_offset: The starting step number for Phase 2 (default 1, meaning
            Phase 1 = step 0).
        total_steps: Total number of steps (Phase 1 + all batches).
        tc_prompt: Custom TC generation prompt (None = use default).

    Returns:
        dict with 'test_cases' (list[TestCase]) and 'warnings' (list[str]).
    """
    tc_prompt = tc_prompt or TC_GENERATE_PROMPT
    all_tcs: list[TestCase] = []
    warnings: list[str] = []
    tc_counter = 0  # running counter for global TC numbering

    # Split FPs into batches of FP_BATCH_SIZE
    batches = []
    for i in range(0, len(fps), FP_BATCH_SIZE):
        batches.append(fps[i : i + FP_BATCH_SIZE])

    for idx, batch in enumerate(batches):
        # Build display name for logging/warnings
        fp_names = ", ".join(fp.name for fp in batch[:3])
        if len(batch) > 3:
            fp_names += f" +{len(batch) - 3} more"

        if progress_callback:
            step = phase1_offset + idx
            msg = f"正在为 {batch[0].name} 生成用例 ({step + 1}/{total_steps})"
            progress_callback(step, total_steps, msg)

        tcs = []
        content = ""

        for attempt in range(MAX_RETRIES):
            try:
                fp_descriptions = "\n".join(
                    f"- 模块：{fp.module}\n  功能点：{fp.name} ({fp.category})\n  描述：{fp.description}"
                    for fp in batch
                )

                prompt = tc_prompt.format(
                    fp_descriptions=fp_descriptions,
                    csv_header=' | '.join(CSV_HEADER),
                )

                desc_prefix = ""
                if project_description:
                    desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

                content = await call_model([
                    {"role": "system", "content": desc_prefix + prompt},
                    {"role": "user", "content": f"请为以上功能点生成测试用例。"},
                ])

                tcs = _parse_tcs_from_text(content, start_index=tc_counter)
                if tcs:
                    break
                else:
                    logger.warning("Batch %d attempt %d: no TCs parsed, retrying...",
                                  idx + 1, attempt + 1)
                    logger.warning("Batch %d raw model output (first 500 chars): %s", idx + 1, content[:500])
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            except (openai.OpenAIError, asyncio.TimeoutError, _json.JSONDecodeError, ValueError, RuntimeError) as e:
                # 单次重试：覆盖 OpenAI 错误 / 异步超时 / JSON 解析 / Pydantic 校验 / MCP 运行时错误
                logger.warning("Batch %d attempt %d failed: %s", idx + 1, attempt + 1, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))

        if not tcs:
            logger.warning("Batch %d failed after %d attempts. fp_descriptions: %s",
                          idx + 1, MAX_RETRIES, fp_descriptions[:500] if fp_descriptions else "N/A")
            warnings.append(f"Batch {idx + 1} ({fp_names}) returned no test cases after {MAX_RETRIES} retries")
        else:
            tc_counter += len(tcs)
            all_tcs.extend(tcs)
            logger.info("Batch %d generated %d test cases for: %s", idx + 1, len(tcs), fp_names)

        if idx < len(batches) - 1:
            await asyncio.sleep(2)

    return {"test_cases": all_tcs, "warnings": warnings}


__all__ = [
    "extract_functional_points",
    "generate_test_cases_for_fps",
]
