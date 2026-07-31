# app/gen/agents/pipeline.py
# Pipeline orchestrator — chains the 4 agents together.
from __future__ import annotations

import logging
from typing import Any

from app.gen.agents.fp_analyzer import FPAnalyzer
from app.gen.agents.tc_generator import TCGenerator
from app.gen.agents.validator import validate_test_cases

from app.gen.cancel import GenAnalysisCancelled

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.db = self.config.get("db")
        self.progress_callback = self.config.get("progress_callback")
        self.fp_analyzer = FPAnalyzer(self.config)
        self.tc_generator = TCGenerator(self.config)

    def _progress(self, current: int, total: int, message: str) -> None:
        cb = self.progress_callback
        if cb:
            try:
                cb(current, total, message)
            except Exception:
                logger.debug("progress_callback failed", exc_info=True)

    def _check_cancelled(self) -> None:
        checker = self.config.get("cancel_checker")
        if checker and checker():
            raise GenAnalysisCancelled()

    async def run(
        self,
        text: str,
        files: list | None = None,
        content_parts: list[dict[str, Any]] | None = None,
    ) -> dict:
        # Step 1: Parse input (text / multimodal parts already prepared by caller)
        self._check_cancelled()
        self._progress(1, 4, "正在解析文档")
        full_text = text or ""

        # Step 2: Extract test items
        self._check_cancelled()
        self._progress(2, 4, "正在提取测试项")
        fps = await self.fp_analyzer.run({
            "text": full_text,
            "content_parts": content_parts,
        })

        # Step 3: Generate test cases
        self._check_cancelled()
        self._progress(3, 4, "正在生成用例")
        tcs = await self.tc_generator.run({
            "fps": fps,
            "project_description": self.config.get("project_description", ""),
            "content_parts": content_parts,
        })
        warnings = list(getattr(self.tc_generator, "last_warnings", None) or [])

        # Step 4: Validate
        self._check_cancelled()
        self._progress(4, 4, "正在校验用例")
        from app.gen.prompts import case_kind_from_tc_prompt_key
        tc_key = self.config.get("tc_prompt_key") or getattr(self.tc_generator, "prompt_key", None)
        require_structured = case_kind_from_tc_prompt_key(tc_key) == "ui"
        v_result = validate_test_cases(tcs, fps, require_structured=require_structured)
        warnings.extend(v_result["warnings"])
        if not v_result["passed"]:
            warnings.append(f"质量校验: {v_result['valid_count']}/{len(tcs)} 个用例通过")
        # Keep invalid cases (with validation_errors) so users can fix & import
        tcs = list(v_result["valid_cases"]) + list(v_result.get("invalid_cases") or [])

        return {
            "functional_points": fps,
            "test_cases": tcs,
            "warnings": warnings,
        }
