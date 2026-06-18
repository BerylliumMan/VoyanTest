"""Tests for core/self_healing.py — AI-powered selector healing.

The module exposes two public coroutines:
    - heal_selector():     LLM-driven candidate generation
    - try_heal_and_retry(): end-to-end heal-and-verify loop

Plus a private lazy client cache helper ``_get_cached_client``.

These tests focus on behaviour at module boundaries: we mock the LLM
client and the MCP manager so no real network or browser calls are made.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.self_healing as self_healing


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """每个测试前重置全局 LLM 客户端缓存。"""
    self_healing._cached_client = None
    yield
    self_healing._cached_client = None


def _make_completion(content: str):
    """构造一个 mock 的 OpenAI ChatCompletion 响应对象。"""
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = content
    return completion


def _make_mcp(snapshot: str = "x" * 50, snapshot_success: bool = True) -> AsyncMock:
    """构造一个最小可用的 mock MCP manager。"""
    mcp = AsyncMock()
    mcp.call_tool = AsyncMock(return_value={
        "success": snapshot_success,
        "text": snapshot,
    })
    return mcp


def _make_llm_client(content: str) -> MagicMock:
    """构造一个最小可用的 mock AsyncOpenAI 客户端。"""
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_make_completion(content)
    )
    return client


# ---------------------------------------------------------------------------
# _get_cached_client
# ---------------------------------------------------------------------------


class TestGetCachedClient:
    @pytest.mark.asyncio
    async def test_caches_client_after_first_call(self):
        fake = MagicMock(name="client")
        with patch(
            "core.llm_wrapper.create_openai_client", return_value=fake
        ) as mock_create:
            c1 = await self_healing._get_cached_client()
            c2 = await self_healing._get_cached_client()

        assert c1 is fake
        assert c2 is fake
        # 第二次调用应该命中缓存 — create_openai_client 只能被调用一次
        assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_create_raises(self):
        with patch(
            "core.llm_wrapper.create_openai_client",
            side_effect=RuntimeError("init failed"),
        ):
            result = await self_healing._get_cached_client()

        assert result is None
        # 缓存应该被设置为 None（被再次调用时尝试重建）
        assert self_healing._cached_client is None

    @pytest.mark.asyncio
    async def test_returns_cached_value_when_already_set(self):
        existing = MagicMock(name="existing")
        self_healing._cached_client = existing

        with patch("core.llm_wrapper.create_openai_client") as mock_create:
            result = await self_healing._get_cached_client()

        assert result is existing
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# heal_selector — DOM snapshot failure modes
# ---------------------------------------------------------------------------


class TestHealSelectorSnapshotFailures:
    @pytest.mark.asyncio
    async def test_returns_empty_when_snapshot_tool_raises(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(side_effect=RuntimeError("mcp boom"))

        result = await self_healing.heal_selector(
            mcp, original_selector="#missing", step_description="点击登录",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_snapshot_unsuccessful(self):
        mcp = _make_mcp(snapshot="<html/>", snapshot_success=False)

        result = await self_healing.heal_selector(
            mcp, original_selector="#x", step_description="step",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_snapshot_too_short(self):
        mcp = _make_mcp(snapshot="abc")  # len < 10

        result = await self_healing.heal_selector(
            mcp, original_selector="#x", step_description="step",
        )
        assert result == []


# ---------------------------------------------------------------------------
# heal_selector — LLM client & response handling
# ---------------------------------------------------------------------------


class TestHealSelectorLLMPaths:
    @pytest.mark.asyncio
    async def test_returns_empty_when_llm_client_unavailable(self):
        mcp = _make_mcp()
        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=None)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_parses_plain_json_candidates(self):
        candidates = [
            {"selector": "text=登录", "confidence": 0.95, "reason": "匹配"},
            {"selector": "#login", "confidence": 0.7, "reason": "id 选择器"},
        ]
        mcp = _make_mcp()
        client = _make_llm_client(json.dumps(candidates))

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="点击登录",
            )

        assert len(result) == 2
        assert result[0]["selector"] == "text=登录"
        assert result[0]["confidence"] == 0.95
        assert result[0]["reason"] == "匹配"

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        candidates = [{"selector": "#x", "confidence": 0.5, "reason": "ok"}]
        wrapped = "```json\n" + json.dumps(candidates) + "\n```"
        mcp = _make_mcp()
        client = _make_llm_client(wrapped)

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        assert len(result) == 1
        assert result[0]["selector"] == "#x"

    @pytest.mark.asyncio
    async def test_returns_empty_on_invalid_json(self):
        mcp = _make_mcp()
        client = _make_llm_client("not json {{")

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_llm_call_raises(self):
        mcp = _make_mcp()
        client = MagicMock()
        client.chat = MagicMock()
        client.chat.completions = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_response_not_a_list(self):
        mcp = _make_mcp()
        client = _make_llm_client(json.dumps({"selector": "#x", "confidence": 0.9}))

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_filters_invalid_candidate_entries(self):
        candidates = [
            {"selector": "#ok", "confidence": 0.9, "reason": "valid"},
            "not a dict",                       # 字符串
            {"confidence": 0.5},                 # 缺少 selector
            {"selector": "#also-ok"},            # 缺少 confidence/reason
        ]
        mcp = _make_mcp()
        client = _make_llm_client(json.dumps(candidates))

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        # 字符串条目和无 selector 的被过滤；其他被规范化
        assert len(result) == 2
        selectors = [r["selector"] for r in result]
        assert "#ok" in selectors
        assert "#also-ok" in selectors
        # 缺失字段被填充默认值
        also = next(r for r in result if r["selector"] == "#also-ok")
        assert also["confidence"] == 0
        assert also["reason"] == ""

    @pytest.mark.asyncio
    async def test_caps_results_to_three(self):
        candidates = [
            {"selector": f"#s{i}", "confidence": 1.0 - i * 0.1, "reason": f"r{i}"}
            for i in range(10)
        ]
        mcp = _make_mcp()
        client = _make_llm_client(json.dumps(candidates))

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        assert len(result) == 3
        assert result[0]["selector"] == "#s0"
        assert result[2]["selector"] == "#s2"

    @pytest.mark.asyncio
    async def test_handles_empty_content(self):
        mcp = _make_mcp()
        # 响应 content 为空字符串 — 解析会失败并返回 []
        client = _make_llm_client("")

        with patch.object(
            self_healing, "_get_cached_client", AsyncMock(return_value=client)
        ):
            result = await self_healing.heal_selector(
                mcp, original_selector="#x", step_description="step",
            )

        assert result == []


# ---------------------------------------------------------------------------
# try_heal_and_retry
# ---------------------------------------------------------------------------


class TestTryHealAndRetry:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_candidates(self):
        mcp = _make_mcp()
        with patch.object(
            self_healing, "heal_selector", AsyncMock(return_value=[])
        ):
            result = await self_healing.try_heal_and_retry(
                mcp,
                step_dict={"description": "step desc"},
                step_obj=None,
                step_description="step desc",
                error="element not found",
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_first_css_selector_that_evaluates_to_found(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "found"})

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[{
                "selector": "#login", "confidence": 0.9, "reason": "id 匹配",
            }]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp,
                step_dict={"description": "点击登录"},
                step_obj=None,
                step_description="点击登录",
                error="element not found",
            )

        assert result == "#login"
        assert mcp.call_tool.await_count == 1
        assert mcp.call_tool.await_args.args[0] == "browser_evaluate"

    @pytest.mark.asyncio
    async def test_uses_step_description_fallback(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "found"})

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[{
                "selector": "#x", "confidence": 0.5, "reason": "x",
            }]),
        ) as mock_heal:
            await self_healing.try_heal_and_retry(
                mcp,
                step_dict={},  # 缺少 description → 使用 step_description
                step_obj=None,
                step_description="fallback desc",
                error="oops",
            )
        # 验证 fallback: original_selector 应该等于 step_description
        assert mock_heal.await_args.kwargs["original_selector"] == "fallback desc"

    @pytest.mark.asyncio
    async def test_text_selector_path(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "found"})

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[{
                "selector": "text=登录", "confidence": 0.8, "reason": "text",
            }]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x",
            )
        assert result == "text=登录"
        js_expr = mcp.call_tool.await_args.args[1]["expression"]
        assert "text=" in js_expr

    @pytest.mark.asyncio
    async def test_continues_to_next_candidate_when_not_found(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(side_effect=[
            {"success": True, "text": "missing"},  # 第一个失败（不包含 "found"）
            {"success": True, "text": "found"},     # 第二个成功
        ])

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[
                {"selector": "#miss", "confidence": 0.9, "reason": "r1"},
                {"selector": "#hit", "confidence": 0.7, "reason": "r2"},
            ]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x",
            )
        assert result == "#hit"

    @pytest.mark.asyncio
    async def test_continues_when_evaluate_raises(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(side_effect=[
            RuntimeError("eval failed"),
            {"success": True, "text": "found"},
        ])

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[
                {"selector": "#a", "confidence": 0.9, "reason": "r1"},
                {"selector": "#b", "confidence": 0.7, "reason": "r2"},
            ]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x",
            )
        assert result == "#b"

    @pytest.mark.asyncio
    async def test_continues_when_evaluate_returns_error(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(side_effect=[
            {"success": True, "text": "error: bad selector"},
            {"success": True, "text": "found"},
        ])

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[
                {"selector": "#bad", "confidence": 0.9, "reason": "r1"},
                {"selector": "#good", "confidence": 0.7, "reason": "r2"},
            ]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x",
            )
        assert result == "#good"

    @pytest.mark.asyncio
    async def test_continues_when_evaluate_unsuccessful(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(side_effect=[
            {"success": False, "text": "timeout"},   # 评估未成功
            {"success": True, "text": "found"},
        ])

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[
                {"selector": "#a", "confidence": 0.9, "reason": "r1"},
                {"selector": "#b", "confidence": 0.7, "reason": "r2"},
            ]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x",
            )
        assert result == "#b"

    @pytest.mark.asyncio
    async def test_returns_none_when_all_candidates_fail(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value={"success": True, "text": "missing"})

        with patch.object(
            self_healing,
            "heal_selector",
            AsyncMock(return_value=[
                {"selector": "#a", "confidence": 0.9, "reason": "r1"},
                {"selector": "#b", "confidence": 0.7, "reason": "r2"},
            ]),
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x",
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        """Healing 超过 healing_timeout 时应返回 None。"""
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock()

        async def slow_heal(*args, **kwargs):
            await asyncio.sleep(5)
            return [{"selector": "#x", "confidence": 0.9, "reason": "r"}]

        with patch.object(self_healing, "heal_selector", side_effect=slow_heal):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x", healing_timeout=0.05,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_respects_max_candidates_limit(self):
        """只测试最多 max_candidates 个候选。"""
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(side_effect=[
            {"success": True, "text": "missing"},
            {"success": True, "text": "missing"},
        ])

        candidates = [
            {"selector": f"#s{i}", "confidence": 0.9 - i * 0.1, "reason": f"r{i}"}
            for i in range(5)
        ]
        with patch.object(
            self_healing, "heal_selector", AsyncMock(return_value=candidates)
        ):
            result = await self_healing.try_heal_and_retry(
                mcp, step_dict={"description": "x"}, step_obj=None,
                step_description="x", max_candidates=2,
            )
        assert result is None
        assert mcp.call_tool.await_count == 2
