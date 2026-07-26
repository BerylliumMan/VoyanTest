# app/gen/agents/tc_generator.py
# TC Generator agent — generates test cases from functional points.
import logging

from app.gen.agents.base import BaseAgent
from app.gen.feature_extractor import generate_test_cases_for_fps
from app.gen.prompts import pick_tc_prompt_key

logger = logging.getLogger(__name__)


class TCGenerator(BaseAgent[dict, list[dict]]):
    name = "tc_generator"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.last_warnings: list[str] = []

    async def run(self, input_data: dict) -> list[dict]:
        fps = input_data.get("fps", [])
        project_description = input_data.get("project_description", "")
        db = self.config.get("db")
        agent_type = self.config.get("agent_type", "generation")
        agent_id = self.config.get("agent_id")
        prompts = self.config.get("prompts", {})
        # Resolved content is always stored under tc_generate by upload;
        # also accept tc_generate_ui key if present.
        tc_prompt = None
        if prompts:
            tc_prompt = prompts.get("tc_generate") or prompts.get("tc_generate_ui")
        if tc_prompt is None and db is not None:
            from app.runtime_config import resolve_prompt_for_agent
            tc_key = self.config.get("tc_prompt_key") or pick_tc_prompt_key(
                self.config.get("skills")
            )
            tc_prompt = await resolve_prompt_for_agent(
                db, agent_type, tc_key, agent_id=agent_id,
            )
        result = await generate_test_cases_for_fps(
            fps,
            project_description=project_description,
            progress_callback=self.config.get("progress_callback"),
            tc_prompt=tc_prompt,
            agent_type=agent_type,
            agent_id=agent_id,
        )
        self.last_warnings = result.get("warnings", [])
        return result.get("test_cases", [])
