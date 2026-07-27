"""Phase 1 / Phase 2 model calls.

Phase 1 extracts test items from document text, images, or ordered multimodal
parts; Phase 2 turns each batch of test items into test cases. Both phases go
through ``call_model`` from :mod:`app.gen.model_client`.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any

import openai
from markupsafe import escape

from app.gen.constants import MAX_RETRIES, RETRY_DELAY
from app.gen.csv_generator import CSV_HEADER
from app.gen.model_client import call_model
from app.gen.models import FunctionalPoint, TestCase
from app.gen.prompts import (
    FP_BATCH_SIZE,
    FP_EXTRACT_PROMPT,
    MIN_TCS_PER_ITEM,
    TC_GENERATE_PROMPT,
)
from app.gen.response_parser import _parse_fps_from_text, _parse_tcs_from_text
from app.runtime_config import render_prompt_variables

logger = logging.getLogger(__name__)


def content_parts_to_openai_user_content(
    content_parts: list[dict[str, Any]],
    *,
    intro: str = "请分析以下需求文档内容（文字与图片按文档顺序排列）：",
) -> list[dict[str, Any]]:
    """Build OpenAI multimodal user ``content`` array preserving order."""
    user_content: list[dict[str, Any]] = [{"type": "text", "text": intro}]
    for part in content_parts:
        ptype = part.get("type")
        if ptype == "text":
            text = (part.get("text") or "").strip()
            if text:
                user_content.append({"type": "text", "text": text})
        elif ptype == "image":
            ext = (part.get("ext") or "png").lstrip(".").lower()
            if ext == "jpg":
                ext = "jpeg"
            b64 = part.get("b64") or ""
            if not b64:
                continue
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{ext};base64,{b64}"},
            })
    return user_content


def _has_images(content_parts: list[dict[str, Any]] | None) -> bool:
    return bool(content_parts) and any(p.get("type") == "image" for p in content_parts)


def _vision_hint(exc: BaseException) -> str | None:
    err = str(exc).lower()
    if any(k in err for k in ("vision", "image", "multimodal", "unsupported", "invalid_request")):
        return f"当前模型可能不支持图片理解，请更换多模态模型后重试: {exc}"
    return None


async def extract_functional_points(
    text: str = None,
    image_data: tuple = None,
    content_parts: list[dict[str, Any]] | None = None,
    project_description: str = "",
    progress_callback=None,
    fp_prompt: str = None,
    agent_type: str = "generation",
    agent_id: int | None = None,
) -> list[FunctionalPoint]:
    """Extract test items (stored as FunctionalPoint) from document / image / parts.

    Phase 1 of two-phase pipeline. When ``content_parts`` includes images, a
    multimodal user message is sent with document order preserved.
    """
    fp_prompt = fp_prompt or FP_EXTRACT_PROMPT
    desc_prefix = ""
    if project_description:
        desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

    prompt = desc_prefix + fp_prompt
    if progress_callback:
        if _has_images(content_parts) or image_data:
            progress_callback(0, 0, "正在分析文档/图片提取测试项")
        else:
            progress_callback(0, 0, "正在分析文档提取测试项")

    async def _call(user_payload) -> str:
        return await call_model(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_payload},
            ],
            agent_type=agent_type,
            agent_id=agent_id,
        )

    def _user_payload():
        if content_parts is not None:
            return content_parts_to_openai_user_content(content_parts)
        if image_data:
            suffix, b64 = image_data
            return [
                {"type": "text", "text": "请分析此界面原型图中的所有测试项和UI元素："},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/{suffix};base64,{b64}"},
                },
            ]
        return text or ""

    try:
        content = await _call(_user_payload())
    except (openai.OpenAIError, asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        hint = _vision_hint(e) if (_has_images(content_parts) or image_data) else None
        if hint:
            logger.warning("Multimodal test-item extraction failed: %s", e)
            raise RuntimeError(hint) from e
        raise

    fps = _parse_fps_from_text(content)
    if progress_callback:
        progress_callback(0, 0, f"提取到 {len(fps)} 个测试项")
    if not fps:
        logger.warning("Test-item extraction empty, retrying... raw: %s", content[:300])
        await asyncio.sleep(2)
        content = await _call(_user_payload())
        fps = _parse_fps_from_text(content)
        if not fps:
            logger.warning("Test-item extraction still empty after retry. Raw: %s", content[:300])
        if progress_callback:
            progress_callback(0, 0, f"重试后提取到 {len(fps)} 个测试项")
    return fps


def _fp_descriptions(batch: list[FunctionalPoint]) -> str:
    return "\n".join(
        f"- 模块：{fp.module}\n  测试项：{fp.name} ({fp.category})\n  描述：{fp.description}"
        for fp in batch
    )


def _merge_tcs_by_title(primary: list[TestCase], extra: list[TestCase]) -> list[TestCase]:
    seen = {(tc.title or "").strip() for tc in primary}
    merged = list(primary)
    for tc in extra:
        title = (tc.title or "").strip()
        if title and title in seen:
            continue
        if title:
            seen.add(title)
        merged.append(tc)
    return merged


async def _generate_batch_once(
    *,
    batch: list[FunctionalPoint],
    tc_prompt: str,
    project_description: str,
    agent_type: str,
    agent_id: int | None,
    tc_counter: int,
    user_hint: str,
) -> list[TestCase]:
    fp_descriptions = _fp_descriptions(batch)
    csv_header = " | ".join(CSV_HEADER)
    prompt = render_prompt_variables(
        tc_prompt,
        fp_descriptions=fp_descriptions,
        fps=fp_descriptions,
        csv_header=csv_header,
    )
    desc_prefix = ""
    if project_description:
        desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

    tcs: list[TestCase] = []
    content = ""
    for attempt in range(MAX_RETRIES):
        try:
            content = await call_model(
                [
                    {"role": "system", "content": desc_prefix + prompt},
                    {"role": "user", "content": user_hint},
                ],
                agent_type=agent_type,
                agent_id=agent_id,
            )
            tcs = _parse_tcs_from_text(content, start_index=tc_counter)
            if tcs:
                return tcs
            logger.warning(
                "TC batch attempt %d: no TCs parsed, retrying... raw: %s",
                attempt + 1,
                content[:500],
            )
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        except (
            openai.OpenAIError,
            asyncio.TimeoutError,
            _json.JSONDecodeError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
        ) as e:
            logger.warning("TC batch attempt %d failed: %s", attempt + 1, e)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    return tcs


async def generate_test_cases_for_fps(
    fps: list[FunctionalPoint],
    project_description: str,
    progress_callback=None,
    phase1_offset=1,
    total_steps=1,
    tc_prompt: str = None,
    agent_type: str = "generation",
    agent_id: int | None = None,
) -> dict:
    """Generate test cases for test items in batches of ``FP_BATCH_SIZE``.

    If a batch yields fewer than ``len(batch) * MIN_TCS_PER_ITEM`` cases, one
    supplemental generation pass is attempted and results are merged by title.
    """
    tc_prompt = tc_prompt or TC_GENERATE_PROMPT
    all_tcs: list[TestCase] = []
    warnings: list[str] = []
    tc_counter = 0

    batches: list[list[FunctionalPoint]] = []
    for i in range(0, len(fps), FP_BATCH_SIZE):
        batches.append(fps[i : i + FP_BATCH_SIZE])

    for idx, batch in enumerate(batches):
        fp_names = ", ".join(fp.name for fp in batch[:3])
        if len(batch) > 3:
            fp_names += f" +{len(batch) - 3} more"

        if progress_callback:
            step = phase1_offset + idx
            msg = f"正在为 {batch[0].name} 生成用例 ({step + 1}/{total_steps})"
            progress_callback(step, total_steps, msg)

        min_needed = len(batch) * MIN_TCS_PER_ITEM
        tcs = await _generate_batch_once(
            batch=batch,
            tc_prompt=tc_prompt,
            project_description=project_description,
            agent_type=agent_type,
            agent_id=agent_id,
            tc_counter=tc_counter,
            user_hint=(
                f"请为以上 {len(batch)} 个测试项生成测试用例。"
                f"每个测试项至少 {MIN_TCS_PER_ITEM} 条（正常/异常/边界），"
                f"本批合计至少 {min_needed} 条。"
            ),
        )

        if tcs and len(tcs) < min_needed:
            logger.info(
                "Batch %d got %d TCs < required %d; supplemental generation",
                idx + 1,
                len(tcs),
                min_needed,
            )
            extra = await _generate_batch_once(
                batch=batch,
                tc_prompt=tc_prompt,
                project_description=project_description,
                agent_type=agent_type,
                agent_id=agent_id,
                tc_counter=tc_counter + len(tcs),
                user_hint=(
                    f"上一轮仅生成 {len(tcs)} 条，不足。"
                    f"请继续为以上 {len(batch)} 个测试项补齐用例，"
                    f"确保每个测试项至少 {MIN_TCS_PER_ITEM} 条（正常/异常/边界），"
                    f"不要重复已有标题。"
                ),
            )
            if extra:
                tcs = _merge_tcs_by_title(tcs, extra)

        if not tcs:
            warnings.append(
                f"Batch {idx + 1} ({fp_names}) returned no test cases after {MAX_RETRIES} retries"
            )
        else:
            # Re-number sequentially after merge
            for i, tc in enumerate(tcs):
                tc.test_case_id = f"TC-{tc_counter + i + 1:03d}"
            tc_counter += len(tcs)
            all_tcs.extend(tcs)
            if len(tcs) < min_needed:
                warnings.append(
                    f"Batch {idx + 1} ({fp_names}) 仅生成 {len(tcs)} 条用例，"
                    f"低于期望 {min_needed} 条（每测试项至少 {MIN_TCS_PER_ITEM} 条）"
                )
            logger.info("Batch %d generated %d test cases for: %s", idx + 1, len(tcs), fp_names)

        if idx < len(batches) - 1:
            await asyncio.sleep(2)

    return {"test_cases": all_tcs, "warnings": warnings}


__all__ = [
    "content_parts_to_openai_user_content",
    "extract_functional_points",
    "generate_test_cases_for_fps",
]
