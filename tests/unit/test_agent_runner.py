# tests/unit/test_agent_runner.py
"""Unit tests for AgentRunner, AgentContext, ConstraintEnforcer, and _should_use_agent_runner."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ==================== AgentContext ====================


class TestAgentContextAddTurn:
    """测试 AgentContext.add_turn — 添加轮次记录并触发压缩。"""

    def test_add_single_turn(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=10)
        ctx.add_turn("user", "目标: 登录系统")
        assert len(ctx.turns) == 1
        assert ctx.turns[0].role == "user"
        assert ctx.turns[0].content == "目标: 登录系统"

    def test_add_turn_with_tool_calls(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=10)
        ctx.add_turn("assistant", "点击登录按钮",
                     tool_calls=[{"action": "click", "selector": "e15"}])
        assert len(ctx.turns) == 1
        assert ctx.turns[0].tool_calls is not None
        assert ctx.turns[0].tool_calls[0]["action"] == "click"

    def test_add_turn_with_tool_result(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=10)
        ctx.add_turn("tool", "操作成功", tool_result="click 完成")
        assert ctx.turns[0].tool_result == "click 完成"

    def test_compression_triggers_when_exceeds_double_max(self):
        """添加超过 max_turns*2 条记录时应触发压缩。"""
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=3)  # 超过 6 轮触发压缩
        for i in range(7):
            ctx.add_turn("assistant", f"action {i}")
        # 压缩后 turns 应约等于 max_turns
        assert len(ctx.turns) == 3
        assert ctx.summary != ""


class TestAgentContextGetContext:
    """测试 AgentContext.get_context — 获取格式化的上下文字符串。"""

    def test_empty_context(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext()
        result = ctx.get_context()
        assert result == "(空上下文)"

    def test_context_with_turns(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext()
        ctx.add_turn("user", "目标: 测试")
        ctx.add_turn("assistant", "执行 click")
        result = ctx.get_context()
        assert "最近对话" in result
        assert "[user] 目标: 测试" in result
        assert "[assistant] 执行 click" in result

    def test_context_with_summary(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=2)
        for i in range(6):
            ctx.add_turn("assistant", f"action {i}")
        result = ctx.get_context()
        assert "历史摘要" in result
        assert "最近对话" in result


class TestAgentContextEstimateTokens:
    """测试 AgentContext.estimate_tokens — token 估算。"""

    def test_empty_returns_one(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext()
        assert ctx.estimate_tokens() == 1

    def test_estimation_proportional_to_chars(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext()
        ctx.add_turn("user", "x" * 400)
        tokens = ctx.estimate_tokens()
        # ~4 chars/token → 约 100 tokens
        assert 80 <= tokens <= 120


class TestAgentContextToDict:
    """测试 AgentContext.to_dict — 导出为可序列化字典。"""

    def test_empty_to_dict(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=10)
        d = ctx.to_dict()
        assert d["turns"] == []
        assert d["summary"] == ""
        assert d["max_turns"] == 10
        assert "estimated_tokens" in d

    def test_to_dict_with_turns(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext()
        ctx.add_turn("user", "hello", tool_calls=[{"a": 1}])
        d = ctx.to_dict()
        assert len(d["turns"]) == 1
        assert d["turns"][0]["role"] == "user"
        assert d["turns"][0]["tool_calls"] == [{"a": 1}]


class TestAgentContextClear:
    """测试 AgentContext.clear — 清空上下文。"""

    def test_clear_removes_all(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext()
        ctx.add_turn("user", "test")
        ctx.summary = "some summary"
        ctx.clear()
        assert ctx.turns == []
        assert ctx.summary == ""


class TestAgentContextCompress:
    """测试 AgentContext._compress — 压缩逻辑细节。"""

    def test_compress_noop_when_under_threshold(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=5)
        for i in range(5):  # 5 < 5*2=10, 不应压缩
            ctx.add_turn("assistant", f"action {i}")
        assert ctx.summary == ""
        assert len(ctx.turns) == 5

    def test_compress_retains_most_recent(self):
        from core.agent_runner.context import AgentContext
        ctx = AgentContext(max_turns=3)
        for i in range(7):  # 7 > 3*2=6, 会压缩
            ctx.add_turn("assistant", f"action {i}")
        assert len(ctx.turns) == 3
        # 保留最近 3 轮: action 4, 5, 6
        assert any("action 6" in t.content for t in ctx.turns)


# ==================== ConstraintEnforcer ====================


class TestValidateUrl:
    """测试 validate_url — URL 白名单校验。"""

    def test_valid_http_url(self):
        from core.agent_runner.constraint import validate_url
        ok, reason = validate_url("http://example.com")
        assert ok is True
        assert reason == ""

    def test_valid_https_url(self):
        from core.agent_runner.constraint import validate_url
        ok, _ = validate_url("https://example.com/path?q=1")
        assert ok is True

    def test_reject_empty_url(self):
        from core.agent_runner.constraint import validate_url
        ok, reason = validate_url("")
        assert ok is False
        assert "为空" in reason

    def test_reject_file_protocol(self):
        from core.agent_runner.constraint import validate_url
        ok, reason = validate_url("file:///etc/passwd")
        assert ok is False
        assert "协议" in reason

    def test_reject_javascript_protocol(self):
        from core.agent_runner.constraint import validate_url
        ok, reason = validate_url("javascript:alert(1)")
        assert ok is False
        assert "协议" in reason

    def test_reject_data_protocol(self):
        from core.agent_runner.constraint import validate_url
        ok, reason = validate_url("data:text/html,<script>alert(1)</script>")
        assert ok is False
        assert "协议" in reason

    def test_reject_no_schema(self):
        from core.agent_runner.constraint import validate_url
        ok, reason = validate_url("example.com")
        assert ok is False
        assert "缺少协议头" in reason

    def test_reject_url_too_long(self):
        from core.agent_runner.constraint import validate_url
        long_url = "http://example.com/" + "a" * 3000
        ok, reason = validate_url(long_url)
        assert ok is False
        assert "长度限制" in reason

    def test_domain_whitelist_allows_match(self):
        from core.agent_runner.constraint import (
            validate_url, set_allowed_domains, ALLOWED_DOMAINS,
        )
        try:
            set_allowed_domains(["example.com", "*.test.org"])
            ok, _ = validate_url("http://example.com/page")
            assert ok is True
        finally:
            set_allowed_domains(None)  # 恢复全局状态

    def test_domain_whitelist_rejects_unmatched(self):
        from core.agent_runner.constraint import (
            validate_url, set_allowed_domains, ALLOWED_DOMAINS,
        )
        try:
            set_allowed_domains(["example.com"])
            ok, reason = validate_url("http://evil.com")
            assert ok is False
            assert "白名单" in reason
        finally:
            set_allowed_domains(None)

    def test_domain_whitelist_wildcard(self):
        from core.agent_runner.constraint import (
            validate_url, set_allowed_domains, ALLOWED_DOMAINS,
        )
        try:
            set_allowed_domains(["*.example.com"])
            ok, _ = validate_url("https://sub.example.com/path")
            assert ok is True
        finally:
            set_allowed_domains(None)

    def test_localhost_allowed_without_domain_restriction(self):
        from core.agent_runner.constraint import validate_url
        ok, _ = validate_url("http://localhost:8000/api")
        assert ok is True

    def test_custom_port_url(self):
        from core.agent_runner.constraint import validate_url
        ok, _ = validate_url("https://example.com:8443/path")
        assert ok is True


class TestTruncateSnapshot:
    """测试 truncate_snapshot — 快照截断。"""

    def test_short_snapshot_unchanged(self):
        from core.agent_runner.constraint import truncate_snapshot
        short = "short snapshot text"
        result = truncate_snapshot(short, max_tokens=100)
        assert result == short

    def test_long_snapshot_truncated(self):
        from core.agent_runner.constraint import truncate_snapshot
        long_text = "x" * 10000
        result = truncate_snapshot(long_text, max_tokens=10)  # 10*4=40 chars
        assert "截断" in result
        assert len(result) < len(long_text)

    def test_exact_boundary_no_truncation(self):
        from core.agent_runner.constraint import truncate_snapshot
        # 10 tokens * 4 chars = 40 chars
        exact = "a" * 40
        result = truncate_snapshot(exact, max_tokens=10)
        assert result == exact

    def test_one_char_over_triggers_truncation(self):
        from core.agent_runner.constraint import truncate_snapshot
        # 41 chars > 40 → 触发截断
        over = "a" * 41
        result = truncate_snapshot(over, max_tokens=10)
        assert "截断" in result

    def test_zero_tokens_returns_empty_marker(self):
        from core.agent_runner.constraint import truncate_snapshot
        text = "hello world"
        result = truncate_snapshot(text, max_tokens=0)
        assert "截断" in result

    def test_very_small_budget_preserves_head(self):
        from core.agent_runner.constraint import truncate_snapshot
        text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # 26 chars
        result = truncate_snapshot(text, max_tokens=1)  # 4 chars budget
        # 头部: 4*0.7=2 chars "AB", 尾部: 4*0.3-20=-19 → 0
        assert result.startswith("AB")


class TestTruncateToolArgs:
    """测试 truncate_tool_args — 工具参数截断。"""

    def test_short_args_unchanged(self):
        from core.agent_runner.constraint import truncate_tool_args
        args = {"key": "short"}
        result = truncate_tool_args(args)
        assert result["key"] == "short"

    def test_long_string_truncated(self):
        from core.agent_runner.constraint import truncate_tool_args
        long_val = "x" * 600
        result = truncate_tool_args({"key": long_val}, max_str_len=500)
        assert len(result["key"]) == 500 + len(f"...[+{len(long_val) - 500} chars]")

    def test_non_string_values_preserved(self):
        from core.agent_runner.constraint import truncate_tool_args
        args = {"count": 42, "flag": True, "nested": [1, 2, 3]}
        result = truncate_tool_args(args)
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["nested"] == [1, 2, 3]


class TestMakeRunKey:
    """测试 make_run_key — 幂等键生成。"""

    def test_format(self):
        from core.agent_runner.constraint import make_run_key
        key = make_run_key(42)
        assert key.startswith("run_42_0_")

    def test_with_batch_id(self):
        from core.agent_runner.constraint import make_run_key
        key = make_run_key(7, batch_id=88)
        assert key.startswith("run_7_88_")

    def test_uniqueness(self):
        from core.agent_runner.constraint import make_run_key
        keys = {make_run_key(1) for _ in range(20)}
        assert len(keys) == 20  # 每个 key 应唯一


# ==================== _should_use_agent_runner ====================


class TestShouldUseAgentRunner:
    """测试 _should_use_agent_runner — AgentRunner 条件分发判断。"""

    def make_agent_def(self, tools=None):
        """构造 mock agent_def 对象（含 ota skill，聚焦 tools 逻辑）。"""
        m = MagicMock()
        m.tools = tools
        m.skills = ["ota"]
        return m

    @pytest.mark.asyncio
    async def test_returns_false_when_none(self):
        from core.runner._orchestrator import _should_use_agent_runner
        result = await _should_use_agent_runner(None)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_tools(self):
        from core.runner._orchestrator import _should_use_agent_runner
        agent_def = self.make_agent_def(tools=None)
        result = await _should_use_agent_runner(agent_def)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_empty_tools(self):
        from core.runner._orchestrator import _should_use_agent_runner
        agent_def = self.make_agent_def(tools=[])
        result = await _should_use_agent_runner(agent_def)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_all_tools_disabled(self):
        from core.runner._orchestrator import _should_use_agent_runner
        agent_def = self.make_agent_def(tools=[
            {"name": "click", "enabled": False},
            {"name": "type", "enabled": False},
        ])
        result = await _should_use_agent_runner(agent_def)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_at_least_one_enabled(self):
        from core.runner._orchestrator import _should_use_agent_runner
        agent_def = self.make_agent_def(tools=[
            {"name": "click", "enabled": False},
            {"name": "type", "enabled": True},
        ])
        result = await _should_use_agent_runner(agent_def)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_enabled_not_explicitly_false(self):
        """工具字典中没有 enabled 字段视为默认启用。"""
        from core.runner._orchestrator import _should_use_agent_runner
        agent_def = self.make_agent_def(tools=[
            {"name": "click"},  # 无 enabled 字段
        ])
        result = await _should_use_agent_runner(agent_def)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_multiple_enabled(self):
        from core.runner._orchestrator import _should_use_agent_runner
        agent_def = self.make_agent_def(tools=[
            {"name": "a", "enabled": True},
            {"name": "b", "enabled": True},
            {"name": "c", "enabled": True},
        ])
        result = await _should_use_agent_runner(agent_def)
        assert result is True


# ==================== ToolRegistry ====================


class TestToolRegistry:
    """测试 ToolRegistry — 工具注册表。"""

    def test_get_actions_list(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        registry = ToolRegistry(mcp)
        actions = registry.get_actions_list()
        assert "click" in actions
        assert "fill" in actions
        assert "done" not in actions  # done 是特殊 action，不在工具列表
        assert "error" not in actions

    def test_get_tool_definitions(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        registry = ToolRegistry(mcp)
        defs = registry.get_tool_definitions()
        actions = {d["action"] for d in defs}
        assert len(defs) == len(actions)
        assert {"click", "fill", "hover", "press_key", "drag", "scroll"} <= actions
        assert {"dialog", "upload", "tabs", "check", "double_click"} <= actions
        assert all("name" in d and "action" in d for d in defs)

    @pytest.mark.asyncio
    async def test_execute_done_returns_done_marker(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        registry = ToolRegistry(mcp)
        result = await registry.execute({"action": "done", "value": "目标已达成"})
        assert result["success"] is True
        assert result["done"] is True
        assert result["summary"] == "目标已达成"

    @pytest.mark.asyncio
    async def test_execute_error_returns_failure(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        registry = ToolRegistry(mcp)
        result = await registry.execute({"action": "error", "value": "无法找到元素"})
        assert result["success"] is False
        assert "无法找到元素" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_snapshot_returns_success(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        registry = ToolRegistry(mcp)
        result = await registry.execute({"action": "snapshot"})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_execute_unknown_action_returns_error(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        registry = ToolRegistry(mcp)
        result = await registry.execute({"action": "unknown_action"})
        assert result["success"] is False
        assert "未知" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_click_delegates_to_mcp(self):
        from core.agent_runner.runner import ToolRegistry
        mcp = MagicMock()
        mcp.execute_tool_call = AsyncMock(return_value={"success": True})
        registry = ToolRegistry(mcp)
        result = await registry.execute({"action": "click", "selector": "e15"})
        assert result == {"success": True}
        mcp.execute_tool_call.assert_called_once()


# ==================== AgentRunner ====================


class TestAgentRunnerInit:
    """测试 AgentRunner.__init__ — 初始化。"""

    def test_init_sets_fields(self):
        from core.agent_runner.runner import AgentRunner
        mcp = MagicMock()
        llm = MagicMock()
        runner = AgentRunner(
            mcp_manager=mcp,
            goal="登录系统",
            llm_client=llm,
            model="gpt-4",
            max_turns=15,
            context_max_turns=5,
            tool_timeout_ms=20000,
        )
        assert runner.goal == "登录系统"
        assert runner.max_turns == 15
        assert runner.tool_timeout_ms == 20000
        assert runner.turns_used == 0
        assert runner.current_url == ""
        assert runner.tool_registry is not None
        assert runner.context is not None

    def test_default_max_turns(self):
        from core.agent_runner.runner import AgentRunner
        mcp = MagicMock()
        llm = MagicMock()
        runner = AgentRunner(mcp_manager=mcp, goal="test", llm_client=llm)
        assert runner.max_turns == 30

    def test_custom_system_prompt(self):
        from core.agent_runner.runner import AgentRunner
        mcp = MagicMock()
        llm = MagicMock()
        custom = "You are a custom agent."
        runner = AgentRunner(
            mcp_manager=mcp, goal="test", llm_client=llm,
            system_prompt=custom,
        )
        assert runner._system_prompt == custom


class TestAgentRunnerMakeResult:
    """测试 AgentRunner._make_result — 构建返回结果。"""

    def test_completed_result(self):
        from core.agent_runner.runner import AgentRunner
        mcp = MagicMock()
        llm = MagicMock()
        runner = AgentRunner(mcp_manager=mcp, goal="test", llm_client=llm)
        result = runner._make_result("completed", 5, result="目标达成", start_time=None)
        assert result["status"] == "completed"
        assert result["turns_used"] == 5
        assert result["result"] == "目标达成"
        assert result["error"] is None
        assert result["context"] is None  # completed 不返回 context

    def test_failed_result(self):
        from core.agent_runner.runner import AgentRunner
        runner = AgentRunner(mcp_manager=MagicMock(), goal="test", llm_client=MagicMock())
        result = runner._make_result("failed", 10, error="超时")
        assert result["status"] == "failed"
        assert result["error"] == "超时"
        assert isinstance(result["duration_ms"], float)

    def test_error_result_has_context(self):
        from core.agent_runner.runner import AgentRunner
        runner = AgentRunner(mcp_manager=MagicMock(), goal="test", llm_client=MagicMock())
        runner.context.add_turn("user", "hello")
        result = runner._make_result("error", 2, error="崩溃")
        # error status != completed，应保留 context
        assert result["context"] is not None
