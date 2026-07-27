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

from app.gen.cancel import GenAnalysisCancelled
from app.gen.chunking import (
    chunk_token_budget,
    estimate_parts_tokens,
    estimate_text_tokens,
    merge_functional_points,
    split_content_parts,
    split_text_into_chunks,
)
from app.gen.constants import MAX_RETRIES, RETRY_DELAY
from app.gen.csv_generator import CSV_HEADER
from app.gen.model_client import call_model, get_context_budget
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


async def _extract_fps_once(
    *,
    user_payload,
    prompt: str,
    agent_type: str,
    agent_id: int | None,
    multimodal: bool,
) -> list[FunctionalPoint]:
    async def _call(payload) -> str:
        return await call_model(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": payload},
            ],
            agent_type=agent_type,
            agent_id=agent_id,
        )

    try:
        content = await _call(user_payload)
    except (openai.OpenAIError, asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        hint = _vision_hint(e) if multimodal else None
        if hint:
            logger.warning("Multimodal test-item extraction failed: %s", e)
            raise RuntimeError(hint) from e
        raise

    fps = _parse_fps_from_text(content)
    if not fps:
        logger.warning("Test-item extraction empty, retrying... raw: %s", content[:300])
        await asyncio.sleep(2)
        content = await _call(user_payload)
        fps = _parse_fps_from_text(content)
        if not fps:
            logger.warning("Test-item extraction still empty after retry. Raw: %s", content[:300])
    return fps


async def extract_functional_points(
    text: str = None,
    image_data: tuple = None,
    content_parts: list[dict[str, Any]] | None = None,
    project_description: str = "",
    progress_callback=None,
    fp_prompt: str = None,
    agent_type: str = "generation",
    agent_id: int | None = None,
    cancel_checker=None,
) -> list[FunctionalPoint]:
    """Extract test items (stored as FunctionalPoint) from document / image / parts.

    Phase 1 of two-phase pipeline. Long documents are split so each model call
    stays within ~80% of the configured context window; results are merged.
    """
    fp_prompt = fp_prompt or FP_EXTRACT_PROMPT
    desc_prefix = ""
    if project_description:
        desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

    prompt = desc_prefix + fp_prompt
    multimodal = bool(_has_images(content_parts) or image_data)

    if cancel_checker and cancel_checker():
        raise GenAnalysisCancelled()

    # Single-image shortcut (no chunking needed)
    if image_data and content_parts is None and not text:
        if progress_callback:
            progress_callback(0, 0, "正在分析文档/图片提取测试项")
        suffix, b64 = image_data
        payload = [
            {"type": "text", "text": "请分析此界面原型图中的所有测试项和UI元素："},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/{suffix};base64,{b64}"},
            },
        ]
        fps = await _extract_fps_once(
            user_payload=payload,
            prompt=prompt,
            agent_type=agent_type,
            agent_id=agent_id,
            multimodal=True,
        )
        if progress_callback:
            progress_callback(0, 0, f"提取到 {len(fps)} 个测试项")
        return fps

    max_ctx = await get_context_budget()
    budget = chunk_token_budget(max_ctx)

    part_chunks: list[list[dict[str, Any]]] | None = None
    text_chunks: list[str] | None = None

    if content_parts is not None:
        total = estimate_parts_tokens(content_parts)
        if total <= budget:
            part_chunks = [content_parts]
        else:
            part_chunks = split_content_parts(content_parts, budget)
            logger.info(
                "Phase1 chunking content_parts: ~%d tokens > budget %d → %d chunks",
                total, budget, len(part_chunks),
            )
    else:
        body = text or ""
        total = estimate_text_tokens(body)
        if total <= budget:
            text_chunks = [body] if body.strip() else []
        else:
            text_chunks = split_text_into_chunks(body, budget)
            logger.info(
                "Phase1 chunking text: ~%d tokens > budget %d → %d chunks",
                total, budget, len(text_chunks),
            )

    n_chunks = len(part_chunks or text_chunks or [])
    if n_chunks == 0:
        return []

    if progress_callback:
        msg = "正在分析文档/图片提取测试项" if multimodal else "正在分析文档提取测试项"
        if n_chunks > 1:
            msg = f"{msg}（分段 1/{n_chunks}）"
        progress_callback(0, 0, msg)

    batch_results: list[list[FunctionalPoint]] = []
    for idx in range(n_chunks):
        if cancel_checker and cancel_checker():
            raise GenAnalysisCancelled()
        if n_chunks > 1 and progress_callback:
            progress_callback(
                idx, n_chunks,
                f"正在提取测试项（分段 {idx + 1}/{n_chunks}）",
            )
        if part_chunks is not None:
            payload = content_parts_to_openai_user_content(
                part_chunks[idx],
                intro=(
                    f"请分析以下需求文档内容（第 {idx + 1}/{n_chunks} 段，"
                    "文字与图片按文档顺序排列；只提取本段出现的测试项）："
                    if n_chunks > 1
                    else "请分析以下需求文档内容（文字与图片按文档顺序排列）："
                ),
            )
        else:
            chunk_text = (text_chunks or [])[idx]
            if n_chunks > 1:
                payload = (
                    f"以下为需求文档第 {idx + 1}/{n_chunks} 段，"
                    f"请只提取本段中的测试项：\n\n{chunk_text}"
                )
            else:
                payload = chunk_text

        fps = await _extract_fps_once(
            user_payload=payload,
            prompt=prompt,
            agent_type=agent_type,
            agent_id=agent_id,
            multimodal=multimodal and part_chunks is not None,
        )
        batch_results.append(fps)
        if idx < n_chunks - 1:
            await asyncio.sleep(RETRY_DELAY)

    merged = merge_functional_points(batch_results)
    if progress_callback:
        if n_chunks > 1:
            progress_callback(
                n_chunks, n_chunks,
                f"分段提取完成，合并后共 {len(merged)} 个测试项",
            )
        else:
            progress_callback(0, 0, f"提取到 {len(merged)} 个测试项")
    return merged


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
    cancel_checker=None,
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

    num_batches = max(1, len(batches)) if fps else 0
    if total_steps <= 1 and num_batches:
        # Pipeline / TCGenerator 未传 total_steps 时，与 image 两阶段路径一致
        total_steps = phase1_offset + num_batches

    for idx, batch in enumerate(batches):
        if cancel_checker and cancel_checker():
            raise GenAnalysisCancelled()
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
                f"本批测试项共 {len(batch)} 个，名称："
                + "、".join(fp.name for fp in batch)
                + f"。\n请为以上每个测试项各生成至少 {MIN_TCS_PER_ITEM} 条用例"
                f"（正常/异常/边界各至少 1 条），"
                f"JSON 数组合计至少 {min_needed} 条；"
                f"每条必须带 fp_name 与 scenario_type。"
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
                    f"上一轮仅生成 {len(tcs)} 条，不足 {min_needed} 条。"
                    f"请继续为以下测试项补齐："
                    + "、".join(fp.name for fp in batch)
                    + f"。每个测试项至少 {MIN_TCS_PER_ITEM} 条（正常/异常/边界），"
                    f"不要重复已有标题；每条必须带 fp_name 与 scenario_type。"
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
            if cancel_checker and cancel_checker():
                raise GenAnalysisCancelled()
            await asyncio.sleep(2)

    return {"test_cases": all_tcs, "warnings": warnings}


__all__ = [
    "content_parts_to_openai_user_content",
    "extract_functional_points",
    "generate_test_cases_for_fps",
]
