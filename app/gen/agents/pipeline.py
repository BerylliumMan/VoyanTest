# app/gen/agents/pipeline.py
# Pipeline orchestrator — chains the 4 agents together.
import logging

from app.gen.agents.parser import Parser
from app.gen.agents.fp_analyzer import FPAnalyzer
from app.gen.agents.tc_generator import TCGenerator
from app.gen.agents.validator import validate_test_cases

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.db = self.config.get("db")
        self.progress_callback = self.config.get("progress_callback")
        self.parser = Parser()
        self.fp_analyzer = FPAnalyzer(self.config)
        self.tc_generator = TCGenerator(self.config)

    def _progress(self, current: int, total: int, message: str) -> None:
        cb = self.progress_callback
        if cb:
            try:
                cb(current, total, message)
            except Exception:
                logger.debug("progress_callback failed", exc_info=True)

    async def run(self, text: str, files: list | None = None) -> dict:
        # Step 1: Parse input
        self._progress(1, 4, "正在解析文档")
        chunks = await self.parser.run({"text": text, "files": files or []})
        full_text = "\n".join(chunks)

        # Step 2: Extract functional points
        self._progress(2, 4, "正在提取功能点")
        fps = await self.fp_analyzer.run(full_text)

        # Step 3: Generate test cases
        self._progress(3, 4, "正在生成用例")
        tcs = await self.tc_generator.run({
            "fps": fps,
            "project_description": self.config.get("project_description", ""),
        })
        warnings = list(getattr(self.tc_generator, "last_warnings", None) or [])

        # Step 4: Validate
        self._progress(4, 4, "正在校验用例")
        v_result = validate_test_cases(tcs, fps)
        warnings.extend(v_result["warnings"])
        if not v_result["passed"]:
            warnings.append(f"质量校验: {v_result['valid_count']}/{len(tcs)} 个用例通过")
        tcs = v_result["valid_cases"]

        return {
            "functional_points": fps,
            "test_cases": tcs,
            "warnings": warnings,
        }
