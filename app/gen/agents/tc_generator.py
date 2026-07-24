# app/gen/agents/tc_generator.py
# TC Generator agent — generates test cases from functional points.
import logging

from app.gen.agents.base import BaseAgent
from app.gen.feature_extractor import generate_test_cases_for_fps

logger = logging.getLogger(__name__)


class TCGenerator(BaseAgent[dict, list[dict]]):
    name = "tc_generator"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    async def run(self, input_data: dict) -> list[dict]:
        fps = input_data.get("fps", [])
        project_description = input_data.get("project_description", "")
        db = self.config.get("db")
        tc_prompt = None
        if db is not None:
            from app.runtime_config import resolve_prompt_for_agent
            tc_prompt = await resolve_prompt_for_agent(db, "generation", "tc_generate")
        result = await generate_test_cases_for_fps(
            fps,
            project_description=project_description,
            tc_prompt=tc_prompt,
        )
        tcs = result.get("test_cases", [])
        return tcs
