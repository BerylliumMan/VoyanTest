"""040 US6：接口 AI 对话工具注册与门控。"""
import core.tools.builtin.api_ai  # noqa: F401
from core.tools.loop import default_tool_filter
from core.tools.registry import REGISTRY


def test_api_ai_tools_registered():
    names = {t.name for t in REGISTRY.all()}
    assert {"analyze_api_failure", "check_api_contract", "generate_api_from_text"} <= names


def test_api_ai_tools_gated_by_global_switch(monkeypatch):
    import app.config as cfg

    class S:
        api_ai_enabled = False
        chat_script_enabled = True

    monkeypatch.setattr(cfg, "get_settings", lambda: S())
    assert default_tool_filter("analyze_api_failure") is False
    assert default_tool_filter("check_api_contract") is False
    assert default_tool_filter("generate_api_from_text") is False
    assert default_tool_filter("list_projects") is True
