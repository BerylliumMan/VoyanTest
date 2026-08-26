# app/gen/agents/fp_analyzer.py
# FP Analyzer agent — extracts test items from text or multimodal parts.
from __future__ import annotations

import logging
from typing import Any

from app.gen.agents.base import BaseAgent
from app.gen.feature_extractor import extract_functional_points

logger = logging.getLogger(__name__)


def _strip_ignorable(s: str) -> str:
    import re as _re
    return _re.sub(r"[\s\u3000、，。；：:；·!！?？【】\[\]()（）\"'`~～—\-_/]", "", s or "")


def bigrams(s: str) -> set:
    clean = _strip_ignorable(s)
    return {clean[i:i + 2] for i in range(len(clean) - 1)} if len(clean) >= 2 else (
        {clean} if clean else set())


def trigrams(s: str) -> set:
    clean = _strip_ignorable(s)
    if len(clean) < 3:
        return bigrams(clean) or ({clean} if clean else set())
    return {clean[i:i + 3] for i in range(len(clean) - 2)}


def grounded(name: str, source_text: str, desc: str | None = None) -> bool:
    """027 迭代：name+desc 联合锚定，任一 3-gram 命中原文即视为有依据。

    v1 的「2-gram 命中率≥50%」会误杀语义改写的真实功能
    （实测『使用正确账号登录』命中率仅 29% 被杀）；desc 通常更贴原文，
    联合后既救回改写项，3-gram 又比 2-gram 更能拒绝纯通用词幻觉。
    """
    text = _strip_ignorable(f"{name or ''} {desc or ''}")
    if not text or not source_text:
        return False
    grams = trigrams(text)
    return any(g in source_text for g in grams)


def grounding_filter(fps: list, source_text: str) -> tuple:
    """027 反幻觉：name+desc 无任何 3-gram 锚定的 FP 丢弃。空 source 全保留。"""
    if not (source_text or "").strip():
        return list(fps or []), []
    kept, dropped = [], []
    for fp in fps or []:
        if isinstance(fp, dict):
            name = str(fp.get("name") or "")
            desc = str(fp.get("desc") or "")
        else:
            name = str(getattr(fp, "name", "") or "")
            desc = str(getattr(fp, "desc", "") or "")
        if grounded(name, source_text, desc):
            kept.append(fp)
        else:
            dropped.append({"name": name})
    if dropped:
        logger.warning(
            "grounding_filter 丢弃 %d 个无锚定 FP: %s",
            len(dropped), [f["name"] for f in dropped],
        )
    return kept, dropped



class FPAnalyzer(BaseAgent[Any, list]):
    name = "fp_analyzer"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    async def run(self, input_data: Any) -> list:
        db = self.config.get("db")
        agent_type = self.config.get("agent_type", "generation")
        agent_id = self.config.get("agent_id")
        prompts = self.config.get("prompts", {})
        fp_key = self.config.get("fp_prompt_key") or "fp_extract"
        fp_prompt = prompts.get("fp_extract") if prompts else None
        if fp_prompt is None and prompts:
            fp_prompt = prompts.get(fp_key)
        if fp_prompt is None and db is not None:
            from app.runtime_config import resolve_prompt_for_agent
            from app.gen.prompts import pick_fp_prompt_key
            fp_key = self.config.get("fp_prompt_key") or pick_fp_prompt_key(
                self.config.get("skills")
            )
            fp_prompt = await resolve_prompt_for_agent(
                db, agent_type, fp_key, agent_id=agent_id,
            )
        # 026-gen-exec-fixes: 过短提示词回退硬编码常量（保证 LLM 拿到完整指令）
        MIN_PROMPT_CHARS = 80
        if fp_prompt is not None and len(fp_prompt.strip()) < MIN_PROMPT_CHARS:
            logger.warning(
                "fp_prompt 过短(%d chars, key=%s)，回退硬编码 FP_EXTRACT_PROMPT",
                len(fp_prompt.strip()), fp_key,
            )
            fp_prompt = None

        text = None
        content_parts = None
        if isinstance(input_data, dict):
            text = input_data.get("text")
            content_parts = input_data.get("content_parts")
        else:
            text = input_data

        result = await extract_functional_points(
            text=text,
            content_parts=content_parts,
            project_description=self.config.get("project_description", ""),
            fp_prompt=fp_prompt,
            progress_callback=self.config.get("progress_callback"),
            agent_type=agent_type,
            agent_id=agent_id,
            cancel_checker=self.config.get("cancel_checker"),
        )

        # 027-e2e-fixes: FP 反幻觉 grounding 过滤（纯文本文档才可锚定）
        try:
            source_text = text or "".join(
                str(p.get("text") or "") for p in (content_parts or []) if isinstance(p, dict)
            )
            if isinstance(result, list) and result:
                kept, dropped = [], []
                for _fp in result:
                    _name = (_fp.get("name") if isinstance(_fp, dict)
                             else getattr(_fp, "name", "")) or ""
                    if grounded(str(_name), source_text or ""):
                        kept.append(_fp)
                    else:
                        dropped.append({"name": str(_name)})
                if dropped:
                    logger.warning(
                        "FP 反幻觉过滤: 保留 %d / 丢弃 %d %s",
                        len(kept), len(dropped), [d["name"] for d in dropped],
                    )
                result = kept
        except Exception:
            logger.warning("grounding_filter 执行失败（跳过过滤）", exc_info=True)

        return result
