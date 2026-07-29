"""Tests for prompt resolution pipeline — seed data, resolve_prompt_for_agent, llm_wrapper."""
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime_config import resolve_prompt_for_agent
from app import db_models
from core.llm_wrapper import generate_tool_call, verify_expected_result


# ═══════════════════════════════════════════════════════════
# Group 1: Seed data validation
# ═══════════════════════════════════════════════════════════

class TestSeedData:
    """验证 seed_defaults 种子提示词定义的完整性和正确性。"""

    @pytest.fixture(scope="class")
    def seed_source(self):
        """返回全部种子提示词 + Agent system_prompt 正文。"""
        from app.seed_defaults import DEFAULT_AGENTS, get_seed_prompts
        parts = [meta["content"] for meta in get_seed_prompts().values()]
        parts.extend(a.get("system_prompt") or "" for a in DEFAULT_AGENTS)
        return "\n".join(parts)

    def test_has_all_four_keys(self, seed_source):
        """种子包含 fp_extract, tc_generate, tc_generate_ui, operation_translate, verify_expected。"""
        from app.seed_defaults import get_seed_prompts
        keys = set(get_seed_prompts())
        expected = {
            "fp_extract",
            "fp_extract_flow",
            "tc_generate",
            "tc_generate_ui",
            "tc_generate_flow",
            "operation_translate",
            "cdp_convert",
            "verify_expected",
            "execution_system",
        }
        assert keys == expected, f"种子 key: {keys}"

    @pytest.mark.parametrize("marker", [
        "JSON", "functional_points", "测试项",
        "浏览器自动化", "操作类型", "三级验证", "verdict",
    ])
    def test_content_markers(self, seed_source, marker):
        """每个种子内容包含关键业务标记（JSON、测试项等）。"""
        assert marker in seed_source, f"缺少标记: {marker}"


# ═══════════════════════════════════════════════════════════
# Group 2: resolve_prompt_for_agent priority chain
# ═══════════════════════════════════════════════════════════

class TestResolvePromptForAgent:
    """验证 resolve_prompt_for_agent 的三层优先级：prompt_overrides > system_prompt > PromptTemplate。"""

    async def _seed_agent(self, db, **kw):
        """创建测试用 AgentDefinition 并提交。"""
        agent = db_models.AgentDefinition(
            name="test", agent_type="generation", is_active=1, **kw,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent

    @pytest.mark.asyncio
    async def test_prompt_overrides_wins(self, db):
        """prompt_overrides[key] 作 skill 正文，system_prompt 作为角色前缀拼接。"""
        await self._seed_agent(
            db, prompt_overrides={"fp_extract": "覆盖: {text}"},
            system_prompt="系统提示词",
        )
        result = await resolve_prompt_for_agent(
            db, "generation", "fp_extract", {"text": "测试"},
        )
        assert result == "系统提示词\n\n覆盖: 测试"

    @pytest.mark.asyncio
    async def test_system_prompt_fallback(self, db):
        """无 override 时 system_prompt 作为前缀，skill 正文来自 PromptTemplate/fallback。"""
        await self._seed_agent(
            db, prompt_overrides={}, system_prompt="系统提示词: {text}",
        )
        result = await resolve_prompt_for_agent(
            db, "generation", "fp_extract", {"text": "测试"},
        )
        assert result.startswith("系统提示词: 测试\n\n")
        assert "测试" in result

    @pytest.mark.asyncio
    async def test_template_fallback(self, db):
        """无 AgentDefinition 时回退到 PromptTemplate.active_version。"""
        from app.db_models import PromptTemplate
        pt = PromptTemplate(
            key="fp_extract", name="t", category="gen",
            content="DB: {text}", variables=["text"], version=1, is_active=True,
        )
        db.add(pt)
        await db.commit()
        result = await resolve_prompt_for_agent(
            db, "generation", "fp_extract", {"text": "测试"},
        )
        assert result == "DB: 测试"

    @pytest.mark.asyncio
    async def test_variable_substitution(self, db):
        """变量替换支持 {var} 和 {{var}} 两种语法。"""
        await self._seed_agent(
            db, prompt_overrides={"fp_extract": "{a} {{b}}"},
        )
        result = await resolve_prompt_for_agent(
            db, "generation", "fp_extract", {"a": "A", "b": "B"},
        )
        assert result == "A B"

    @pytest.mark.asyncio
    async def test_ultimate_fallback(self, db):
        """无 AgentDefinition 也无 PromptTemplate 时返回基础 fallback。"""
        result = await resolve_prompt_for_agent(
            db, "nonexistent", "no_key", {"text": "测试"},
        )
        assert "测试" in result


# ═══════════════════════════════════════════════════════════
# Group 3: llm_wrapper DB integration
# ═══════════════════════════════════════════════════════════

class TestLlmWrapper:
    """验证 generate_tool_call / verify_expected_result 的 prompt 解析路径。"""

    @staticmethod
    def _raise_client():
        c = AsyncMock()
        c.chat.completions.create.side_effect = ValueError("stop")
        return c

    @pytest.mark.asyncio
    @patch("core.llm_wrapper._resolve_config", new_callable=AsyncMock)
    @patch("core.llm_wrapper.create_openai_client")
    @patch("app.runtime_config.resolve_prompt_for_agent", new_callable=AsyncMock)
    async def test_tool_call_db_resolution(self, mock_resolve, mock_create, mock_config, db):
        """agent_type + db → resolve_prompt_for_agent(..., "operation_translate") 被调用。"""
        mock_config.return_value = ("key", "base", "model")
        mock_resolve.return_value = "custom"
        mock_create.return_value = self._raise_client()
        with pytest.raises(ValueError, match="stop"):
            await generate_tool_call("step", "dom", agent_type="execution", db=db)
        mock_resolve.assert_called_once_with(db, "execution", "operation_translate")

    @pytest.mark.asyncio
    @patch("core.llm_wrapper._resolve_config", new_callable=AsyncMock)
    @patch("core.llm_wrapper.create_openai_client")
    @patch("app.runtime_config.resolve_prompt_for_agent", new_callable=AsyncMock)
    async def test_tool_call_fallback(self, mock_resolve, mock_create, mock_config):
        """无 agent_type/db → 不调用 resolve_prompt_for_agent, 使用 SYSTEM_PROMPT。"""
        mock_config.return_value = ("key", "base", "model")
        mock_create.return_value = self._raise_client()
        with pytest.raises(ValueError, match="stop"):
            await generate_tool_call("step", "dom")
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    @patch("core.llm_wrapper._resolve_config", new_callable=AsyncMock)
    @patch("core.llm_wrapper.create_openai_client")
    @patch("app.runtime_config.resolve_prompt_for_agent", new_callable=AsyncMock)
    async def test_verify_db_resolution(self, mock_resolve, mock_create, mock_config, db):
        """verify_expected_result → resolve_prompt_for_agent(..., "verify_expected") 被调用。"""
        mock_config.return_value = ("key", "base", "model")
        mock_resolve.return_value = "custom verify"
        mock_create.return_value = self._raise_client()
        result = await verify_expected_result("expected", "dom", agent_type="execution", db=db)
        mock_resolve.assert_called_once_with(db, "execution", "verify_expected")
        assert result.passed is False  # LLM 调用被 mock 中断

    @pytest.mark.asyncio
    @patch("core.llm_wrapper._resolve_config", new_callable=AsyncMock)
    @patch("core.llm_wrapper.create_openai_client")
    @patch("app.runtime_config.resolve_prompt_for_agent", new_callable=AsyncMock)
    async def test_verify_fallback(self, mock_resolve, mock_create, mock_config):
        """无 agent_type/db → 不调用 resolve_prompt_for_agent, 使用 VERIFY_PROMPT。"""
        mock_config.return_value = ("key", "base", "model")
        mock_create.return_value = self._raise_client()
        result = await verify_expected_result("expected", "dom")
        mock_resolve.assert_not_called()
        assert result.passed is False
