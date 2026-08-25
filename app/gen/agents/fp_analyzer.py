# app/gen/agents/fp_analyzer.py
# FP Analyzer agent — extracts test items from text or multimodal parts.
from __future__ import annotations

import logging
from typing import Any

from app.gen.agents.base import BaseAgent
from app.gen.feature_extractor import extract_functional_points

logger = logging.getLogger(__name__)


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

        return await extract_functional_points(
            text=text,
            content_parts=content_parts,
            project_description=self.config.get("project_description", ""),
            fp_prompt=fp_prompt,
            progress_callback=self.config.get("progress_callback"),
            agent_type=agent_type,
            agent_id=agent_id,
            cancel_checker=self.config.get("cancel_checker"),
        )
