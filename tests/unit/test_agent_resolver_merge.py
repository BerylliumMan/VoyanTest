"""Unit tests for Agent llm_config merge (empty model inherits global)."""
from app.agent_resolver import merge_llm_config


def test_empty_model_keeps_global_default():
    defaults = {"model": "gpt-4o", "api_key": "k", "api_base": "https://x", "temperature": 0.1}
    merged = merge_llm_config(defaults, {"model": "", "temperature": 0.3})
    assert merged["model"] == "gpt-4o"
    assert merged["temperature"] == 0.3
    assert merged["api_key"] == "k"


def test_whitespace_model_keeps_global_default():
    defaults = {"model": "gpt-4o"}
    assert merge_llm_config(defaults, {"model": "   "})["model"] == "gpt-4o"


def test_explicit_model_overrides():
    defaults = {"model": "gpt-4o", "temperature": 0.1}
    merged = merge_llm_config(defaults, {"model": "claude-3"})
    assert merged["model"] == "claude-3"


def test_none_override_ignored():
    defaults = {"model": "gpt-4o", "api_base": "https://x"}
    merged = merge_llm_config(defaults, {"model": None, "api_base": None})
    assert merged["model"] == "gpt-4o"
    assert merged["api_base"] == "https://x"
