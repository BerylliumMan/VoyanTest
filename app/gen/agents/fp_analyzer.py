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
        agent_type = self.config.get("agent_type", "generation")
        agent_id = self.config.get("agent_id")
        prompts = self.config.get("prompts", {})
        fp_prompt = prompts.get("fp_extract") if prompts else None
        if fp_prompt is None and db is not None:
            from app.runtime_config import resolve_prompt_for_agent
            fp_prompt = await resolve_prompt_for_agent(
                db, agent_type, "fp_extract", agent_id=agent_id,
            )
        result = await extract_functional_points(
            text=input_data,
            project_description=self.config.get("project_description", ""),
            fp_prompt=fp_prompt,
            progress_callback=self.config.get("progress_callback"),
            agent_type=agent_type,
            agent_id=agent_id,
        )
        return result
