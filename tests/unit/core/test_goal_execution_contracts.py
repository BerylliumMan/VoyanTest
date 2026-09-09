from core.goal_agent_loop import _parse_goal_action
from core.script_templates import _emit_close_overlays


def test_goal_action_accepts_json_embedded_in_reasoning_text():
    decision = _parse_goal_action(
        'Reasoning... {"status":"continue","action":"click","selector":"e7"}'
    )
    assert decision.action == "click"
    assert decision.selector == "e7"


def test_close_overlay_script_cannot_succeed_without_real_click():
    script = "\n".join(_emit_close_overlays())
    assert "closed_count += 1" in script
    assert "no visible dialog or notification to close" in script
    assert "evaluate('el => el.remove()')" not in script
    assert "get_by_role" in script
