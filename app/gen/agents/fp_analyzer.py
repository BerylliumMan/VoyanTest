# app/gen/agents/fp_analyzer.py
# FP Analyzer agent — extracts functional points from text.
from app.gen.agents.base import BaseAgent
from app.gen.feature_extractor import extract_functional_points
from app.gen.model_client import get_context_budget


class FPAnalyzer(BaseAgent[str, list[dict]]):
    name = "fp_analyzer"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    async def run(self, input_data: str) -> list[dict]:
        db = self.config.get("db")
        fp_prompt = None
        if db is not None:
            from app.runtime_config import resolve_prompt_for_agent
            fp_prompt = await resolve_prompt_for_agent(db, "generation", "fp_extract")
        # FP_EXTRACT_PROMPT is used as fallback inside extract_functional_points when fp_prompt is None
        result = await extract_functional_points(
            text=input_data,
            project_description=self.config.get("project_description", ""),
            fp_prompt=fp_prompt,
        )
        # 直接返回 FunctionalPoint 对象（pipeline/tc_generator/报告均使用 dataclass 属性访问）
        return result
