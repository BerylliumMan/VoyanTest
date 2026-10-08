# tests/unit/test_agent_definition_semantics.py
"""AgentDefinition 激活互斥集合语义守卫。

generation 允许多个同时启用（「AI 用例生成」页按用途选择）；
execution/recording 仍保持同类型唯一活跃。
"""
from __future__ import annotations


def test_generation_not_exclusive():
    from app.crud.agent_definition import _EXCLUSIVE_ACTIVE_TYPES

    assert "generation" not in _EXCLUSIVE_ACTIVE_TYPES


def test_execution_recording_still_exclusive():
    from app.crud.agent_definition import _EXCLUSIVE_ACTIVE_TYPES

    assert _EXCLUSIVE_ACTIVE_TYPES == frozenset({"execution", "recording"})
