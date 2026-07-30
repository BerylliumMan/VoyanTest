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
    CHARS_PER_TOKEN_INV,
    build_phase1_chunks_from_parts,
    build_phase1_chunks_from_text,
    chunk_token_budget,
    estimate_parts_tokens,
    estimate_text_tokens,
    looks_like_filename_module,
    merge_functional_points,
    normalize_module_path,
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
from app.gen.response_parser import (
    _parse_fps_from_text,
    _parse_tcs_from_text,
    looks_truncated_fp_output,
)
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


def compact_parts_keep_images(
    content_parts: list[dict[str, Any]],
    budget: int,
) -> list[dict[str, Any]]:
    """Shrink multimodal parts to fit ``budget`` while preferring to keep images.

    Text parts are truncated / dropped first; images are kept in document order
    until the remaining budget is exhausted.
    """
    if estimate_parts_tokens(content_parts) <= budget:
        return list(content_parts)

    images = [p for p in content_parts if p.get("type") == "image" and p.get("b64")]
    texts = [p for p in content_parts if p.get("type") == "text" and (p.get("text") or "").strip()]

    # Reserve capacity for as many images as possible, then fill with text.
    kept_images: list[dict[str, Any]] = []
    used = 0
    for img in images:
        cost = estimate_parts_tokens([img])
        if used + cost > budget and kept_images:
            break
        if used + cost > budget:
            break
        kept_images.append(img)
        used += cost

    text_budget = max(0, budget - used)
    kept_texts: list[dict[str, Any]] = []
    for t in texts:
        raw = (t.get("text") or "").strip()
        if not raw:
            continue
        cost = estimate_text_tokens(raw)
        if cost <= text_budget:
            kept_texts.append({"type": "text", "text": raw})
            text_budget -= cost
            continue
        # Truncate to remaining budget (approx chars)
        max_chars = max(80, int(text_budget / CHARS_PER_TOKEN_INV) if text_budget else 0)
        if max_chars < 80:
            break
        kept_texts.append({"type": "text", "text": raw[:max_chars] + "…"})
        text_budget = 0
        break

    # Re-interleave roughly: all truncated texts first as context, then images
    # (order less critical once compacted; intro still carries the TC hint).
    if not kept_images and not kept_texts:
        return list(content_parts)[:1]
    logger.info(
        "Phase2 flow compact_parts: %d→%d parts (images %d→%d) for budget %d",
        len(content_parts),
        len(kept_texts) + len(kept_images),
        len(images),
        len(kept_images),
        budget,
    )
    return kept_texts + kept_images


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
    continue_from: list[FunctionalPoint] | None = None,
) -> tuple[list[FunctionalPoint], str, bool]:
    """Returns (fps, raw_content, truncated_flag)."""

    async def _call(payload) -> str:
        return await call_model(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": payload},
            ],
            agent_type=agent_type,
            agent_id=agent_id,
        )

    # When continuing, prepend instruction as text prefix for string payloads,
    # or as an extra text part for multimodal lists.
    cont_hint = ""
    if continue_from:
        names = "、".join(
            f"{fp.module}/{fp.name}" for fp in continue_from[:80] if fp.name
        )
        cont_hint = (
            "【续写】上一轮输出可能被截断。请继续提取尚未覆盖的测试项，"
            "不要重复以下已有项：\n"
            f"{names}\n"
            "只输出增量 JSON：{\"functional_points\":[...]}，desc 控制在一句话内。\n\n"
        )

    def _with_hint(payload):
        if not cont_hint:
            return payload
        if isinstance(payload, str):
            return cont_hint + payload
        if isinstance(payload, list):
            return [{"type": "text", "text": cont_hint}] + list(payload)
        return payload

    try:
        content = await _call(_with_hint(user_payload))
    except (openai.OpenAIError, asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        hint = _vision_hint(e) if multimodal else None
        if hint:
            logger.warning("Multimodal test-item extraction failed: %s", e)
            raise RuntimeError(hint) from e
        raise

    truncated = looks_truncated_fp_output(content)
    fps = _parse_fps_from_text(content)
    if not fps:
        logger.warning("Test-item extraction empty, retrying... raw: %s", content[:300])
        await asyncio.sleep(2)
        content = await _call(_with_hint(user_payload))
        truncated = looks_truncated_fp_output(content)
        fps = _parse_fps_from_text(content)
        if not fps:
            logger.warning("Test-item extraction still empty after retry. Raw: %s", content[:300])
    elif truncated:
        logger.warning(
            "Test-item extraction truncated but salvaged %d items; will continue if needed",
            len(fps),
        )
    return fps, content, truncated


async def _extract_fps_with_continuation(
    *,
    user_payload,
    prompt: str,
    agent_type: str,
    agent_id: int | None,
    multimodal: bool,
    max_continues: int = 2,
) -> list[FunctionalPoint]:
    """Extract FPs; if output truncates, continue up to ``max_continues`` times."""
    all_fps: list[FunctionalPoint] = []
    continue_from: list[FunctionalPoint] | None = None
    for round_i in range(max_continues + 1):
        fps, _raw, truncated = await _extract_fps_once(
            user_payload=user_payload,
            prompt=prompt,
            agent_type=agent_type,
            agent_id=agent_id,
            multimodal=multimodal,
            continue_from=continue_from,
        )
        if fps:
            all_fps = merge_functional_points([all_fps, fps]) if all_fps else list(fps)
        if not truncated:
            break
        if round_i >= max_continues:
            logger.warning(
                "Phase1 still truncated after %d continuations; keeping %d items",
                max_continues, len(all_fps),
            )
            break
        logger.info(
            "Phase1 continuation %d/%d after truncation (have %d items)",
            round_i + 1, max_continues, len(all_fps),
        )
        continue_from = all_fps
        await asyncio.sleep(RETRY_DELAY)
    return all_fps


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
        fps = await _extract_fps_with_continuation(
            user_payload=payload,
            prompt=prompt,
            agent_type=agent_type,
            agent_id=agent_id,
            multimodal=True,
        )
        for fp in fps:
            fp.module = normalize_module_path(fp.module)
        if progress_callback:
            progress_callback(0, 0, f"提取到 {len(fps)} 个测试项")
        return fps

    max_ctx = await get_context_budget(agent_type=agent_type, agent_id=agent_id)
    budget = chunk_token_budget(max_ctx)

    if content_parts is not None:
        total = estimate_parts_tokens(content_parts)
        phase1_chunks = build_phase1_chunks_from_parts(content_parts, budget)
        if len(phase1_chunks) > 1:
            logger.info(
                "Phase1 chapter-chunking content_parts: ~%d tokens, budget %d → %d chunks",
                total, budget, len(phase1_chunks),
            )
    else:
        body = text or ""
        total = estimate_text_tokens(body)
        phase1_chunks = build_phase1_chunks_from_text(body, budget)
        if len(phase1_chunks) > 1:
            logger.info(
                "Phase1 chapter-chunking text: ~%d tokens, budget %d → %d chunks",
                total, budget, len(phase1_chunks),
            )

    n_chunks = len(phase1_chunks)
    if n_chunks == 0:
        return []

    if progress_callback:
        msg = "正在分析文档/图片提取测试项" if multimodal else "正在分析文档提取测试项"
        if n_chunks > 1:
            c0 = phase1_chunks[0]
            msg = f"{msg}（{c0.module} 1/{n_chunks}）"
        progress_callback(0, 0, msg)

    batch_results: list[list[FunctionalPoint]] = []
    for idx, chunk in enumerate(phase1_chunks):
        if cancel_checker and cancel_checker():
            raise GenAnalysisCancelled()
        if n_chunks > 1 and progress_callback:
            progress_callback(
                idx, n_chunks,
                f"正在提取测试项（{chunk.module} {chunk.segment}/{chunk.segment_total}，总段 {idx + 1}/{n_chunks}）",
            )
        if chunk.multimodal or chunk.parts:
            payload = content_parts_to_openai_user_content(
                chunk.parts,
                intro=chunk.intro,
            )
            use_mm = True
        else:
            if n_chunks > 1:
                payload = f"{chunk.intro}\n\n{chunk.text}"
            else:
                payload = chunk.text
            use_mm = False

        fps = await _extract_fps_with_continuation(
            user_payload=payload,
            prompt=prompt,
            agent_type=agent_type,
            agent_id=agent_id,
            multimodal=use_mm and multimodal,
        )
        # Prefer chapter module when model left it generic (never file-name modules)
        for fp in fps:
            if (
                chunk.module
                and chunk.module not in ("通用", "文档开头")
                and not looks_like_filename_module(chunk.module)
            ):
                if not (fp.module or "").strip() or fp.module in ("通用", "文档开头"):
                    fp.module = chunk.module
            fp.module = normalize_module_path(fp.module, chapter_hint=chunk.module)
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
    content_parts: list[dict[str, Any]] | None = None,
    flow_mode: bool = False,
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

    # Flow mode: attach handbook text+screenshots so steps can read boxed UI labels.
    user_payload: Any = user_hint
    multimodal = False
    if flow_mode and _has_images(content_parts):
        max_ctx = await get_context_budget(agent_type=agent_type, agent_id=agent_id)
        budget = chunk_token_budget(max_ctx)
        # Leave headroom for system prompt + hint already counted loosely in budget.
        parts = compact_parts_keep_images(list(content_parts or []), budget)
        intro = (
            f"{user_hint}\n\n"
            "以下为操作手册原文与截图（按文档顺序）。"
            "请结合红框/色框/高亮区域与相邻文字生成本批 UI 步骤。"
        )
        user_payload = content_parts_to_openai_user_content(parts, intro=intro)
        multimodal = True

    tcs: list[TestCase] = []
    content = ""
    for attempt in range(MAX_RETRIES):
        try:
            content = await call_model(
                [
                    {"role": "system", "content": desc_prefix + prompt},
                    {"role": "user", "content": user_payload},
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
            hint = _vision_hint(e) if multimodal else None
            if hint:
                logger.warning("Flow TC multimodal failed, falling back to text-only: %s", e)
                user_payload = user_hint
                multimodal = False
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
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
    min_tcs_per_item: int = MIN_TCS_PER_ITEM,
    flow_mode: bool = False,
    content_parts: list[dict[str, Any]] | None = None,
) -> dict:
    """Generate test cases for test items in batches of ``FP_BATCH_SIZE``.

    If a batch yields fewer than ``len(batch) * min_tcs_per_item`` cases, one
    supplemental generation pass is attempted and results are merged by title.
    """
    tc_prompt = tc_prompt or TC_GENERATE_PROMPT
    per_item = max(1, int(min_tcs_per_item or MIN_TCS_PER_ITEM))
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

        min_needed = len(batch) * per_item
        if flow_mode:
            user_hint = (
                f"本批操作流程共 {len(batch)} 个，名称："
                + "、".join(fp.name for fp in batch)
                + f"。\n请为以上每个流程各生成 {per_item} 条文档主路径 UI 用例，"
                f"JSON 数组合计至少 {min_needed} 条；"
                f"scenario_type 填「文档流程」；禁止扩异常/边界。"
                f"expected 只能摘录文档/截图中已写明的断言；"
                f"文档未写明则该步 expected 用空字符串，禁止自编预期，禁止写「文档未写明预期」。"
            )
        else:
            user_hint = (
                f"本批测试项共 {len(batch)} 个，名称："
                + "、".join(fp.name for fp in batch)
                + f"。\n请为以上每个测试项生成高价值用例（合计至少 {min_needed} 条，"
                f"每项通常 {per_item}～4 条）：按测试项类型选型"
                f"（主路径/校验失败/空结果/组合查询等），"
                f"**禁止**机械凑「正常/异常/边界」三类；"
                f"文档未写明的场景不要编造。"
                f"每条必须带 fp_name 与具体 scenario_type。"
                f"组合查询项须输出两两组合（非笛卡尔积），scenario_type=组合查询。"
            )
        tcs = await _generate_batch_once(
            batch=batch,
            tc_prompt=tc_prompt,
            project_description=project_description,
            agent_type=agent_type,
            agent_id=agent_id,
            tc_counter=tc_counter,
            user_hint=user_hint,
            content_parts=content_parts,
            flow_mode=flow_mode,
        )

        if tcs and len(tcs) < min_needed:
            logger.info(
                "Batch %d got %d TCs < required %d; supplemental generation",
                idx + 1,
                len(tcs),
                min_needed,
            )
            if flow_mode:
                extra_hint = (
                    f"上一轮仅生成 {len(tcs)} 条，不足 {min_needed} 条。"
                    f"请继续为以下流程各补 1 条文档主路径用例："
                    + "、".join(fp.name for fp in batch)
                    + "。不要扩异常/边界；不要重复已有标题；scenario_type=文档流程。"
                )
            else:
                extra_hint = (
                    f"上一轮仅生成 {len(tcs)} 条，不足 {min_needed} 条。"
                    f"请继续为以下测试项补齐："
                    + "、".join(fp.name for fp in batch)
                    + f"。每个测试项至少 {per_item} 条（正常/异常/边界），"
                    f"不要重复已有标题；每条必须带 fp_name 与 scenario_type。"
                    f"查询/组合查询相关项须补齐单条件与两两组合（scenario_type=组合查询）。"
                )
            extra = await _generate_batch_once(
                batch=batch,
                tc_prompt=tc_prompt,
                project_description=project_description,
                agent_type=agent_type,
                agent_id=agent_id,
                tc_counter=tc_counter + len(tcs),
                user_hint=extra_hint,
                content_parts=content_parts,
                flow_mode=flow_mode,
            )
            if extra:
                tcs = _merge_tcs_by_title(tcs, extra)

        if not tcs:
            warnings.append(
                f"Batch {idx + 1} ({fp_names}) returned no test cases after {MAX_RETRIES} retries"
            )
        else:
            # Re-number sequentially after merge; scrub filename-like modules
            for i, tc in enumerate(tcs):
                tc.test_case_id = f"TC-{tc_counter + i + 1:03d}"
                tc.module = normalize_module_path(tc.module or "")
                if (not tc.module or tc.module == "通用") and len(batch) == 1:
                    tc.module = normalize_module_path(batch[0].module or "")
            tc_counter += len(tcs)
            all_tcs.extend(tcs)
            if len(tcs) < min_needed:
                unit = "流程" if flow_mode else "测试项"
                warnings.append(
                    f"Batch {idx + 1} ({fp_names}) 仅生成 {len(tcs)} 条用例，"
                    f"低于期望 {min_needed} 条（每{unit}至少 {per_item} 条）"
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
