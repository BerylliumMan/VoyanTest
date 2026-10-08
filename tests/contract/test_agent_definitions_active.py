# tests/contract/test_agent_definitions_active.py
"""AgentDefinition 激活语义契约测试 — generation 允许多活跃 / execution 互斥。"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.contract


async def _create(db, name: str, agent_type: str, active: bool):
    from app.crud.agent_definition import create_agent_definition
    from app.models.schemas import AgentDefinitionCreate

    return await create_agent_definition(
        db,
        AgentDefinitionCreate(name=name, agent_type=agent_type, is_active=active),
    )


class TestGenerationMultiActive:
    """generation 允许多个同时启用（生成页按用途选择）。"""

    @pytest.mark.asyncio
    async def test_activating_second_generation_keeps_first(self, db):
        """激活第二个 generation → 第一个保持激活（修复前被降活）。"""
        from app.crud.agent_definition import get_agent_definition, update_agent_definition
        from app.models.schemas import AgentDefinitionUpdate

        first = await _create(db, "gen-multi-a", "generation", True)
        second = await _create(db, "gen-multi-b", "generation", False)

        await update_agent_definition(db, second.id, AgentDefinitionUpdate(is_active=True))

        first_after = await get_agent_definition(db, first.id)
        second_after = await get_agent_definition(db, second.id)
        assert bool(first_after.is_active) is True
        assert bool(second_after.is_active) is True

    @pytest.mark.asyncio
    async def test_activating_third_generation_keeps_others(self, db):
        """三个 generation 全激活 → 全部保持激活。"""
        from app.crud.agent_definition import get_agent_definition, update_agent_definition
        from app.models.schemas import AgentDefinitionUpdate

        a = await _create(db, "gen-tri-a", "generation", True)
        b = await _create(db, "gen-tri-b", "generation", True)
        c = await _create(db, "gen-tri-c", "generation", False)

        await update_agent_definition(db, c.id, AgentDefinitionUpdate(is_active=True))

        states = []
        for obj in (a, b, c):
            fresh = await get_agent_definition(db, obj.id)
            states.append(bool(fresh.is_active))
        assert states == [True, True, True]


class TestCreatePathExclusivity:
    """创建即激活路径与 update 路径同规则。"""

    @pytest.mark.asyncio
    async def test_create_execution_active_deactivates_others(self, db):
        """第二个 execution 创建即激活 → 第一个被降活（唯一活跃）。"""
        from app.crud.agent_definition import get_agent_definition

        first = await _create(db, "exec-create-a", "execution", True)
        second = await _create(db, "exec-create-b", "execution", True)

        first_after = await get_agent_definition(db, first.id)
        second_after = await get_agent_definition(db, second.id)
        assert bool(first_after.is_active) is False
        assert bool(second_after.is_active) is True

    @pytest.mark.asyncio
    async def test_create_generation_active_keeps_others(self, db):
        """第二个 generation 创建即激活 → 互不影响（允许多活跃）。"""
        from app.crud.agent_definition import get_agent_definition

        first = await _create(db, "gen-create-a", "generation", True)
        second = await _create(db, "gen-create-b", "generation", True)

        first_after = await get_agent_definition(db, first.id)
        second_after = await get_agent_definition(db, second.id)
        assert bool(first_after.is_active) is True
        assert bool(second_after.is_active) is True


class TestExecutionStillExclusive:
    """execution/recording 同类型仍互斥（唯一活跃语义不变）。"""

    @pytest.mark.asyncio
    async def test_activating_second_execution_deactivates_first(self, db):
        from app.crud.agent_definition import get_agent_definition, update_agent_definition
        from app.models.schemas import AgentDefinitionUpdate

        first = await _create(db, "exec-excl-a", "execution", True)
        second = await _create(db, "exec-excl-b", "execution", False)

        await update_agent_definition(db, second.id, AgentDefinitionUpdate(is_active=True))

        first_after = await get_agent_definition(db, first.id)
        second_after = await get_agent_definition(db, second.id)
        assert bool(first_after.is_active) is False
        assert bool(second_after.is_active) is True

    @pytest.mark.asyncio
    async def test_activating_second_recording_deactivates_first(self, db):
        from app.crud.agent_definition import get_agent_definition, update_agent_definition
        from app.models.schemas import AgentDefinitionUpdate

        first = await _create(db, "rec-excl-a", "recording", True)
        second = await _create(db, "rec-excl-b", "recording", False)

        await update_agent_definition(db, second.id, AgentDefinitionUpdate(is_active=True))

        first_after = await get_agent_definition(db, first.id)
        second_after = await get_agent_definition(db, second.id)
        assert bool(first_after.is_active) is False
        assert bool(second_after.is_active) is True
