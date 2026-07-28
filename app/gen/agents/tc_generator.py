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
        # Resolved content is stored under tc_generate by upload; accept aliases.
        tc_prompt = None
        if prompts:
            tc_prompt = (
                prompts.get("tc_generate")
                or prompts.get("tc_generate_ui")
                or prompts.get("tc_generate_flow")
            )
        if tc_prompt is None and db is not None:
            from app.runtime_config import resolve_prompt_for_agent
            tc_key = self.config.get("tc_prompt_key") or pick_tc_prompt_key(
                self.config.get("skills")
            )
            tc_prompt = await resolve_prompt_for_agent(
                db, agent_type, tc_key, agent_id=agent_id,
            )
        from app.gen.prompts import min_tcs_per_item as _min_tcs
        min_tcs = self.config.get("min_tcs_per_item")
        if min_tcs is None:
            min_tcs = _min_tcs(
                self.config.get("skills"),
                tc_prompt_key=self.config.get("tc_prompt_key"),
            )
        result = await generate_test_cases_for_fps(
            fps,
            project_description=project_description,
            progress_callback=self.config.get("progress_callback"),
            tc_prompt=tc_prompt,
            agent_type=agent_type,
            agent_id=agent_id,
            cancel_checker=self.config.get("cancel_checker"),
            min_tcs_per_item=int(min_tcs),
            flow_mode=int(min_tcs) <= 1,
        )
        self.last_warnings = result.get("warnings", [])
        return result.get("test_cases", [])
