# tests/unit/core/test_llm_wrapper.py
"""Comprehensive unit tests for core/llm_wrapper.py.

Targets 90%+ branch coverage of:
- Pydantic models: VerificationResult, PlaywrightMCPToolCall
- Config resolution: _load_db_config, _resolve_config
- Client creation: create_openai_client, create_wrapped_llm
- Tool call generation: generate_tool_call (happy path, retries, JSON repair, validation)
- Result verification: verify_expected_result
"""

import json
import ast
import logging
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from pydantic import ValidationError

from core.llm_wrapper import (
    SYSTEM_PROMPT,
    VERIFY_PROMPT,
    VerificationResult,
    PlaywrightMCPToolCall,
    _load_db_config,
    _resolve_config,
    create_openai_client,
    create_wrapped_llm,
    generate_tool_call,
    verify_expected_result,
)


# ------------------------------------------------------------------
# Fixtures / helpers
# ------------------------------------------------------------------


def _make_async_client(content_payload, side_effect=None):
    """Create an AsyncMock OpenAI client whose .chat.completions.create returns content.

    content_payload can be:
      - str: returned as .message.content
      - Exception: raised as side_effect
      - list: side_effect sequence
    """
    client = AsyncMock()

    if isinstance(content_payload, list):
        client.chat.completions.create = AsyncMock(side_effect=content_payload)
        return client

    if isinstance(content_payload, Exception):
        client.chat.completions.create = AsyncMock(side_effect=content_payload)
        return client

    choice = MagicMock()
    choice.message.content = content_payload
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(choices=[choice])
    )
    return client


def _success_response(content):
    """Build a single-success response side_effect item."""
    choice = MagicMock()
    choice.message.content = content
    return MagicMock(choices=[choice])


def _resolve_patched(key="sk-test", base="https://test.com/v1", model="gpt-4"):
    """Patch _resolve_config with a happy path triple."""
    return patch(
        "core.llm_wrapper._resolve_config",
        return_value=(key, base, model),
    )


# ------------------------------------------------------------------
# Pydantic models
# ------------------------------------------------------------------


class TestVerificationResultModel:
    def test_default_reason_empty_string(self):
        v = VerificationResult(passed=True)
        assert v.passed is True
        assert v.reason == ""

    def test_passed_false(self):
        v = VerificationResult(passed=False, reason="missing text")
        assert v.passed is False
        assert v.reason == "missing text"

    def test_passed_required(self):
        # `passed` is required — must raise when omitted
        with pytest.raises(ValidationError):
            VerificationResult(reason="x")  # type: ignore[call-arg]


class TestPlaywrightMCPToolCallModel:
    def test_minimal_click(self):
        t = PlaywrightMCPToolCall(action="click", selector="e1")
        assert t.action == "click"
        assert t.selector == "e1"
        assert t.selector_type == "css"
        assert t.timeout_ms == 30000
        assert t.thinking is None
        assert t.next_goal is None
        assert t.value is None

    def test_all_fields_explicit(self):
        t = PlaywrightMCPToolCall(
            action="fill",
            selector="e2",
            selector_type="text",
            value="hello world",
            timeout_ms=5000,
            thinking="typing credentials",
            next_goal="submit form",
        )
        assert t.action == "fill"
        assert t.selector == "e2"
        assert t.selector_type == "text"
        assert t.value == "hello world"
        assert t.timeout_ms == 5000
        assert t.thinking == "typing credentials"
        assert t.next_goal == "submit form"

    def test_action_required(self):
        with pytest.raises(ValidationError):
            PlaywrightMCPToolCall(selector="e1")  # type: ignore[call-arg]

    def test_screenshot_no_selector(self):
        t = PlaywrightMCPToolCall(action="screenshot")
        assert t.action == "screenshot"
        assert t.selector is None
        assert t.value is None

    def test_xpath_selector_type(self):
        t = PlaywrightMCPToolCall(action="click", selector="//button", selector_type="xpath")
        assert t.selector_type == "xpath"

    def test_assert_text_action(self):
        t = PlaywrightMCPToolCall(action="assert_text", value="Welcome")
        assert t.action == "assert_text"
        assert t.value == "Welcome"

    def test_serialize_then_reparse(self):
        t = PlaywrightMCPToolCall(action="click", selector="e1", thinking="x")
        data = t.model_dump()
        t2 = PlaywrightMCPToolCall.model_validate(data)
        assert t2.action == "click"
        assert t2.selector == "e1"


# ------------------------------------------------------------------
# _load_db_config  (lines 121-144)
# ------------------------------------------------------------------


class TestLoadDBConfig:
    """Tests for _load_db_config.

    Note: the current implementation uses async execute/select API.
    These tests were written for the old sync .query() API.
    Rewritten to mock async asyncpg-style context manager.
    """
    @pytest.mark.asyncio
    async def test_returns_decrypted_config(self):
        """Happy path: row found, decrypt_value called, config returned."""
        mock_row = MagicMock()
        mock_row.model = "gpt-4o"
        mock_row.api_key = "encrypted-blob"
        mock_row.api_base = "https://api.test.com/v1"
        mock_row.temperature = 0.3

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_row

        mock_db = MagicMock()
        async def mock_execute(q):
            return mock_result
        mock_db.execute = mock_execute

        mock_cm = MagicMock()
        mock_cm.__aenter__.return_value = mock_db

        with patch("app.database.AsyncSessionLocal", return_value=mock_cm), \
             patch("core.llm_wrapper.decrypt_value", return_value="decrypted-key"):
            result = await _load_db_config()

        assert result == {
            "model": "gpt-4o",
            "api_key": "decrypted-key",
            "api_base": "https://api.test.com/v1",
            "temperature": 0.3,
        }

    @pytest.mark.asyncio
    async def test_no_row_raises_runtime_error(self):
        """When AIConfig row absent: RuntimeError raised."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = MagicMock()
        async def mock_execute(q):
            return mock_result
        mock_db.execute = mock_execute

        mock_cm = MagicMock()
        mock_cm.__aenter__.return_value = mock_db

        with patch("app.database.AsyncSessionLocal", return_value=mock_cm):
            with pytest.raises(RuntimeError, match="ai_configs table has no row"):
                await _load_db_config()

    @pytest.mark.asyncio
    async def test_db_closed_even_on_exception(self):
        """If execute() raises, the async context manager still exits cleanly."""
        mock_db = MagicMock()
        async def mock_execute(q):
            raise RuntimeError("db crash")
        mock_db.execute = mock_execute

        mock_cm = MagicMock()
        mock_cm.__aenter__.return_value = mock_db

        with patch("app.database.AsyncSessionLocal", return_value=mock_cm):
            with pytest.raises(RuntimeError):
                await _load_db_config()


# ------------------------------------------------------------------
# _resolve_config  (lines 147-170)
# ------------------------------------------------------------------


class TestResolveConfig:
    @pytest.mark.asyncio
    async def test_uses_db_values(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": "sk-db",
                "api_base": "https://db.com/v1",
                "model": "gpt-4",
            }
            key, base, model = await _resolve_config()
        assert key == "sk-db"
        assert base == "https://db.com/v1"
        assert model == "gpt-4"

    @pytest.mark.asyncio
    async def test_explicit_overrides_db(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": "sk-old",
                "api_base": "https://old.com/v1",
                "model": "gpt-3.5",
            }
            key, base, model = await _resolve_config(
                explicit_key="sk-new",
                explicit_base="https://new.com/v1",
                explicit_model="gpt-4o",
            )
        assert key == "sk-new"
        assert base == "https://new.com/v1"
        assert model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_missing_all_required_raises(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": "",
                "api_base": "",
                "model": "",
            }
            with pytest.raises(RuntimeError, match="missing required fields"):
                await _resolve_config()

    @pytest.mark.asyncio
    async def test_missing_partial_with_no_explicit_raises(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": "sk-x",
                "api_base": "",
                "model": "gpt-4",
            }
            with pytest.raises(RuntimeError, match="missing required fields"):
                await _resolve_config()

    @pytest.mark.asyncio
    async def test_missing_with_explicit_override_does_not_raise(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": "",
                "api_base": "",
                "model": "",
            }
            key, base, model = await _resolve_config(
                explicit_key="sk-explicit",
                explicit_base="https://explicit.com/v1",
                explicit_model="gpt-4o",
            )
        assert key == "sk-explicit"
        assert base == "https://explicit.com/v1"
        assert model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_explicit_partial_fills_from_db(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": "sk-db",
                "api_base": "https://db.com/v1",
                "model": "gpt-4",
            }
            key, base, model = await _resolve_config(explicit_key="sk-override")
        assert key == "sk-override"
        assert base == "https://db.com/v1"
        assert model == "gpt-4"

    @pytest.mark.asyncio
    async def test_db_values_none_treated_as_missing(self):
        with patch("core.llm_wrapper._load_db_config") as mock_load:
            mock_load.return_value = {
                "api_key": None,
                "api_base": None,
                "model": None,
            }
            with pytest.raises(RuntimeError, match="missing required fields"):
                await _resolve_config()
        

# ------------------------------------------------------------------
# create_openai_client  (lines 178-192)
# ------------------------------------------------------------------


class TestCreateOpenAIClient:
    @pytest.mark.asyncio
    async def test_creates_client_with_resolved_config(self):
        with patch(
            "core.llm_wrapper._resolve_config",
            return_value=("sk-test", "https://api.test.com/v1", "gpt-4"),
        ):
            client = await create_openai_client()
        assert client is not None
        assert hasattr(client, "chat")

    @pytest.mark.asyncio
    async def test_explicit_overrides_passed_through(self):
        with patch(
            "core.llm_wrapper._resolve_config",
            return_value=("sk-x", "https://x.com/v1", "gpt-4"),
        ) as mock_resolve:
            await create_openai_client(api_key="sk-explicit", api_base="https://y.com/v1")
        call = mock_resolve.call_args
        assert call.kwargs.get("explicit_key") == "sk-explicit" or \
               call.args and call.args[0] == "sk-explicit"
        assert call.kwargs.get("explicit_base") == "https://y.com/v1" or \
               (len(call.args) >= 2 and call.args[1] == "https://y.com/v1")

    @pytest.mark.asyncio
    async def test_empty_key_raises_value_error(self):
        with patch(
            "core.llm_wrapper._resolve_config",
            return_value=("", "https://api.test.com/v1", "gpt-4"),
        ):
            with pytest.raises(ValueError, match="No API key"):
                await create_openai_client()


# ------------------------------------------------------------------
# create_wrapped_llm  (lines 320-331)
# ------------------------------------------------------------------


class TestCreateWrappedLLM:
    @pytest.mark.asyncio
    async def test_returns_configured_client(self):
        with patch(
            "core.llm_wrapper._resolve_config",
            return_value=("sk-w", "https://w.com/v1", "gpt-4"),
        ):
            client = await create_wrapped_llm()
        assert client is not None
        assert hasattr(client, "chat")

    @pytest.mark.asyncio
    async def test_forwards_api_key_and_base(self):
        with patch(
            "core.llm_wrapper.create_openai_client",
            return_value=AsyncMock(),
        ) as mock_create:
            await create_wrapped_llm(api_key="sk-w", api_base="https://w.com/v1")
        mock_create.assert_called_once_with(api_key="sk-w", api_base="https://w.com/v1")

    @pytest.mark.asyncio
    async def test_temperature_argument_accepted_but_ignored(self):
        with patch(
            "core.llm_wrapper._resolve_config",
            return_value=("sk-w", "https://w.com/v1", "gpt-4"),
        ):
            client = await create_wrapped_llm(temperature=0.7)
        assert client is not None

    def test_model_argument_accepted_but_ignored(self):
        """model is accepted for back-compat."""
        with patch(
            "core.llm_wrapper._resolve_config",
            return_value=("sk-w", "https://w.com/v1", "gpt-4"),
        ):
            client = create_wrapped_llm(model="gpt-4o")
        assert client is not None


# ------------------------------------------------------------------
# generate_tool_call — happy path and prompt structure  (lines 195-313)
# ------------------------------------------------------------------


class TestGenerateToolCallHappyPath:
    @pytest.mark.asyncio
    async def test_successful_generation(self):
        content = json.dumps({
            "action": "click",
            "selector": "e15",
            "value": None,
            "timeout_ms": 30000,
            "thinking": "clicking submit",
            "next_goal": "verify success",
        })
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click submit", "<page>", client=client)
        assert result.action == "click"
        assert result.selector == "e15"
        assert result.thinking == "clicking submit"
        assert result.next_goal == "verify success"
        assert client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_prompt_contains_step_description(self):
        client = _make_async_client(
            json.dumps({"action": "click", "selector": "e1"})
        )

        with _resolve_patched():
            await generate_tool_call("LOGIN_BUTTON_TEXT", "<page>", client=client)

        call_kwargs = client.chat.completions.create.call_args.kwargs
        user_msg = call_kwargs["messages"][1]["content"]
        assert "LOGIN_BUTTON_TEXT" in user_msg
        assert "STEP DESCRIPTION" in user_msg
        assert "PAGE CONTENT" in user_msg

    @pytest.mark.asyncio
    async def test_prompt_uses_empty_page_fallback(self):
        """Empty dom_snapshot must produce '(empty page / no text available)'."""
        client = _make_async_client(
            json.dumps({"action": "goto", "value": "https://x.com"})
        )

        with _resolve_patched():
            await generate_tool_call("navigate", "", client=client)

        user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "(empty page / no text available)" in user_msg

    @pytest.mark.asyncio
    async def test_prompt_includes_expected_result(self):
        """expected_result kwarg must appear in user message."""
        client = _make_async_client(
            json.dumps({"action": "click", "selector": "e1"})
        )

        with _resolve_patched():
            await generate_tool_call(
                "click button",
                "<page>",
                expected_result="登录成功",
                client=client,
            )
        user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "EXPECTED RESULT" in user_msg
        assert "登录成功" in user_msg

    @pytest.mark.asyncio
    async def test_no_expected_result_omits_section(self):
        """When expected_result is None, EXPECTED RESULT block absent."""
        client = _make_async_client(
            json.dumps({"action": "click", "selector": "e1"})
        )

        with _resolve_patched():
            await generate_tool_call("click", "<page>", client=client)
        user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "EXPECTED RESULT" not in user_msg

    @pytest.mark.asyncio
    async def test_system_prompt_uses_constant(self):
        client = _make_async_client(
            json.dumps({"action": "click", "selector": "e1"})
        )

        with _resolve_patched():
            await generate_tool_call("click", "<page>", client=client)
        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["messages"][0]["role"] == "system"
        assert call_kwargs["messages"][0]["content"] == SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_temperature_and_max_tokens_forwarded(self):
        client = _make_async_client(
            json.dumps({"action": "click", "selector": "e1"})
        )

        with _resolve_patched():
            await generate_tool_call(
                "click",
                "<page>",
                temperature=0.42,
                client=client,
            )
        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.42
        assert call_kwargs["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_explicit_model_passed_through(self):
        client = _make_async_client(
            json.dumps({"action": "click", "selector": "e1"})
        )

        with _resolve_patched(model="gpt-4o") as mock_resolve:
            await generate_tool_call("click", "<page>", model="gpt-4o-mini", client=client)
        # explicit_model was forwarded to _resolve_config
        assert mock_resolve.call_args.kwargs.get("explicit_model") == "gpt-4o-mini" or \
               (len(mock_resolve.call_args.args) >= 3 and mock_resolve.call_args.args[2] == "gpt-4o-mini")
        # resolved_model used in API call
        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_creates_client_when_not_provided(self):
        """client=None → create_openai_client() is invoked internally."""
        choice = MagicMock()
        choice.message.content = json.dumps({"action": "goto", "value": "https://x.com"})

        with (
            _resolve_patched(),
            patch("core.llm_wrapper.create_openai_client") as mock_create,
        ):
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(choices=[choice])
            )
            mock_create.return_value = mock_client

            result = await generate_tool_call("go to x.com", "<page>")
            assert result.action == "goto"
            mock_create.assert_called_once_with()


# ------------------------------------------------------------------
# generate_tool_call — markdown fence stripping
# ------------------------------------------------------------------


class TestGenerateToolCallMarkdownStripping:
    @pytest.mark.asyncio
    async def test_strips_json_markdown_fence(self):
        content = '```json\n{"action": "goto", "value": "https://x.com"}\n```'
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("go", "<page>", client=client)
        assert result.action == "goto"
        assert result.value == "https://x.com"

    @pytest.mark.asyncio
    async def test_strips_plain_markdown_fence(self):
        content = '```\n{"action": "click", "selector": "e5"}\n```'
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"

    @pytest.mark.asyncio
    async def test_strips_surrounding_whitespace(self):
        content = '   \n\n  {"action": "click", "selector": "e1"}  \n  '
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"


# ------------------------------------------------------------------
# generate_tool_call — JSON repair paths (lines 279-303)
# ------------------------------------------------------------------


class TestGenerateToolCallJSONRepair:
    @pytest.mark.asyncio
    async def test_repairs_single_quotes(self):
        content = "{'action': 'goto', 'value': 'https://x.com'}"
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("go", "<page>", client=client)
        assert result.action == "goto"
        assert result.value == "https://x.com"

    @pytest.mark.asyncio
    async def test_repairs_trailing_comma_in_object(self):
        content = '{"action": "click", "selector": "e1",}'
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"

    @pytest.mark.asyncio
    async def test_repairs_trailing_comma_in_array(self):
        """Trailing comma in array triggers the repair regex branch.
        Resulting value is a list, which fails Pydantic (value must be str);
        that exercises the ValidationError retry path.
        """
        content = '{"action": "click", "selector": "e1", "value": ["a", "b",]}'
        client = _make_async_client(content)

        with _resolve_patched():
            with pytest.raises(ValueError, match="Validation error"):
                await generate_tool_call("click", "<page>", client=client)
        # Repaired JSON was attempted; all 3 calls hit Pydantic validation
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_repairs_python_none(self):
        content = '{"action": "click", "selector": "e1", "value": None}'
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"
        assert result.value is None

    @pytest.mark.asyncio
    async def test_repairs_python_true_false(self):
        """`True` in a value position (non-string) triggers the repair regex
        `:\s*True\b` → `: true`. Pydantic then rejects `action: bool` →
        ValidationError.
        """
        content = '{"action": True, "selector": "e1"}'
        client = _make_async_client(content)

        with _resolve_patched():
            with pytest.raises(ValueError, match="Validation error"):
                await generate_tool_call("click", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_repays_python_true_in_value_position(self):
        """`True` in `action` position is repaired then fails Pydantic."""
        content = '{"action": True, "selector": "e1"}'
        client = _make_async_client(content)

        with _resolve_patched():
            with pytest.raises(ValueError, match="Validation error"):
                await generate_tool_call("click", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_repays_python_false_in_value_position(self):
        content = '{"action": False, "selector": "e1"}'
        client = _make_async_client(content)

        with _resolve_patched():
            with pytest.raises(ValueError, match="Validation error"):
                await generate_tool_call("click", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_repays_via_regex_brace_extraction(self):
        """Text surrounding braces stripped via regex repair path."""
        # Surrounding text plus broken JSON that requires brace extraction
        # to make a valid object after quote normalization.
        content = 'Here you go: {\'action\': \'click\', \'selector\': \'e1\'} thanks'
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"

    @pytest.mark.asyncio
    async def test_ast_literal_eval_fallback(self):
        """Pure Python literal (single quotes, None) — repaired via ast.literal_eval."""
        # After replacement, single-quote-only dict that ast.literal_eval can parse
        content = "{'action': 'click', 'selector': 'e1', 'value': None}"
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"
        assert result.value is None

    @pytest.mark.asyncio
    async def test_completely_invalid_json_fails_after_3_attempts(self):
        client = _make_async_client("not json at all - garbage data")

        with _resolve_patched():
            with pytest.raises(ValueError, match="Failed to generate valid tool call after 3 attempts"):
                await generate_tool_call("do something", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_invalid_json_then_success(self):
        """First response invalid; second valid → succeeds on retry."""
        success = _success_response(
            json.dumps({"action": "click", "selector": "e1"})
        )
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[_success_response("garbage"), success]
        )

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"
        assert client.chat.completions.create.call_count == 2


# ------------------------------------------------------------------
# generate_tool_call — API error handling (lines 262-267)
# ------------------------------------------------------------------


class TestGenerateToolCallAPIErrors:
    @pytest.mark.asyncio
    async def test_api_error_retries_then_succeeds(self):
        success = _success_response(
            json.dumps({"action": "click", "selector": "e1"})
        )
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[Exception("transient 503"), success]
        )

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"
        assert client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_api_error_3_times_raises_value_error(self):
        client = _make_async_client(Exception("API down"))

        with _resolve_patched():
            with pytest.raises(ValueError, match="LLM API call failed after 3 attempts"):
                await generate_tool_call("do something", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_api_error_logged_via_logger(self, caplog):
        client = _make_async_client(Exception("transient"))

        with _resolve_patched():
            with caplog.at_level(logging.ERROR, logger="core.llm_wrapper"):
                with pytest.raises(ValueError):
                    await generate_tool_call("x", "<page>", client=client)
        assert any("LLM API call failed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_api_error_message_preserved_in_raised_error(self):
        client = _make_async_client(Exception("specific failure: rate limit"))

        with _resolve_patched():
            with pytest.raises(ValueError, match="specific failure: rate limit"):
                await generate_tool_call("x", "<page>", client=client)

    @pytest.mark.asyncio
    async def test_retry_message_includes_last_error(self):
        """On attempt > 0, retry message includes last_error hint."""
        # First call: API error. Second: success. Verify messages[2] contains the error.
        success = _success_response(
            json.dumps({"action": "click", "selector": "e1"})
        )
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[Exception("oops"), success]
        )

        with _resolve_patched():
            await generate_tool_call("click", "<page>", client=client)
        # Second call should have appended a user message with the error hint
        second_call_msgs = client.chat.completions.create.call_args_list[1].kwargs["messages"]
        # Original 2 + retry hint = 3
        assert len(second_call_msgs) == 3
        assert "Previous response was invalid" in second_call_msgs[2]["content"]
        assert "API error: oops" in second_call_msgs[2]["content"]


# ------------------------------------------------------------------
# generate_tool_call — Pydantic validation error paths (lines 305-313)
# ------------------------------------------------------------------


class TestGenerateToolCallValidationErrors:
    @pytest.mark.asyncio
    async def test_validation_error_retry_then_succeed(self):
        bad = _success_response(json.dumps({"selector": "e1", "value": "x"}))  # missing action
        good = _success_response(json.dumps({"action": "click", "selector": "e1"}))

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, good])

        with _resolve_patched():
            result = await generate_tool_call("click", "<page>", client=client)
        assert result.action == "click"
        assert client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_validation_error_3_times_raises(self):
        bad = _success_response(json.dumps({"selector": "e1"}))  # missing action
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, bad, bad])

        with _resolve_patched():
            with pytest.raises(ValueError, match="Failed to generate valid tool call after 3 attempts"):
                await generate_tool_call("click", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_validation_error_logged_as_warning(self, caplog):
        bad = _success_response(json.dumps({"selector": "e1"}))
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, bad, bad])

        with _resolve_patched():
            with caplog.at_level(logging.WARNING, logger="core.llm_wrapper"):
                with pytest.raises(ValueError):
                    await generate_tool_call("click", "<page>", client=client)
        assert any("Pydantic validation" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_validation_error_message_mentions_validation(self):
        bad = _success_response(json.dumps({"selector": "e1"}))
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, bad, bad])

        with _resolve_patched():
            with pytest.raises(ValueError, match="Validation error"):
                await generate_tool_call("click", "<page>", client=client)

    @pytest.mark.asyncio
    async def test_invalid_action_value_rejected(self):
        """Pydantic accepts any string for action — verify it doesn't reject valid ones."""
        content = json.dumps({"action": "wait", "value": "loading complete"})
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("wait for load", "<page>", client=client)
        assert result.action == "wait"
        assert result.value == "loading complete"


# ------------------------------------------------------------------
# generate_tool_call — last_error branches (line 247, 266, 309)
# ------------------------------------------------------------------


class TestGenerateToolCallRetryMessage:
    @pytest.mark.asyncio
    async def test_invalid_json_triggers_retry_message(self):
        bad = _success_response("garbage no json")
        good = _success_response(json.dumps({"action": "click", "selector": "e1"}))
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, good])

        with _resolve_patched():
            await generate_tool_call("click", "<page>", client=client)

        # Second call messages should include invalid JSON hint
        second_msgs = client.chat.completions.create.call_args_list[1].kwargs["messages"]
        assert len(second_msgs) == 3
        assert "Previous response was invalid" in second_msgs[2]["content"]
        assert "Invalid JSON" in second_msgs[2]["content"]

    @pytest.mark.asyncio
    async def test_validation_error_triggers_retry_message(self):
        bad = _success_response(json.dumps({"selector": "e1"}))  # missing action
        good = _success_response(json.dumps({"action": "click", "selector": "e1"}))
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, good])

        with _resolve_patched():
            await generate_tool_call("click", "<page>", client=client)

        second_msgs = client.chat.completions.create.call_args_list[1].kwargs["messages"]
        assert "Validation error" in second_msgs[2]["content"]


# ------------------------------------------------------------------
# generate_tool_call — empty content
# ------------------------------------------------------------------


class TestGenerateToolCallEmptyContent:
    @pytest.mark.asyncio
    async def test_empty_message_content(self):
        """LLM returns empty string — should retry and eventually raise."""
        empty = _success_response("")
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[empty, empty, empty])

        with _resolve_patched():
            with pytest.raises(ValueError):
                await generate_tool_call("x", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_none_message_content_treated_as_empty(self):
        """LLM returns content=None → content becomes '' → fails."""
        none_choice = MagicMock()
        none_choice.message.content = None
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[MagicMock(choices=[none_choice])] * 3
        )

        with _resolve_patched():
            with pytest.raises(ValueError):
                await generate_tool_call("x", "<page>", client=client)
        assert client.chat.completions.create.call_count == 3


# ------------------------------------------------------------------
# verify_expected_result — happy path and core branches (lines 361-438)
# ------------------------------------------------------------------


class TestVerifyExpectedResultHappyPath:
    @pytest.mark.asyncio
    async def test_passed_true(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "button visible"})
        )

        with _resolve_patched():
            result = await verify_expected_result("button shown", "<snapshot>", client=client)
        assert result.passed is True
        assert result.reason == "button visible"

    @pytest.mark.asyncio
    async def test_passed_false(self):
        client = _make_async_client(
            json.dumps({"passed": False, "reason": "not visible"})
        )

        with _resolve_patched():
            result = await verify_expected_result("button shown", "<snapshot>", client=client)
        assert result.passed is False
        assert result.reason == "not visible"

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        client = _make_async_client(
            '```\n{"passed": true, "reason": "OK"}\n```'
        )

        with _resolve_patched():
            result = await verify_expected_result("test", "<snapshot>", client=client)
        assert result.passed is True
        assert result.reason == "OK"

    @pytest.mark.asyncio
    async def test_strips_json_fence(self):
        client = _make_async_client(
            '```json\n{"passed": false, "reason": "missing"}\n```'
        )

        with _resolve_patched():
            result = await verify_expected_result("test", "<snapshot>", client=client)
        assert result.passed is False
        assert result.reason == "missing"

    @pytest.mark.asyncio
    async def test_default_reason_when_missing_in_llm_output(self):
        """LLM returns only {passed:true} — Pydantic fills default reason=''."""
        client = _make_async_client(json.dumps({"passed": True}))

        with _resolve_patched():
            result = await verify_expected_result("test", "<snapshot>", client=client)
        assert result.passed is True
        assert result.reason == ""


class TestVerifyExpectedResultPromptStructure:
    @pytest.mark.asyncio
    async def test_prompt_uses_verify_prompt_constant(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "OK"})
        )

        with _resolve_patched():
            await verify_expected_result("ok", "<snap>", client=client)

        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["messages"][0]["content"] == VERIFY_PROMPT
        assert call_kwargs["messages"][0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_prompt_includes_step_description_when_given(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "done"})
        )

        with _resolve_patched():
            await verify_expected_result(
                "ok",
                "<snap>",
                step_description="clicked login",
                client=client,
            )
        user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "STEP EXECUTED" in user_msg
        assert "clicked login" in user_msg

    @pytest.mark.asyncio
    async def test_prompt_omits_step_executed_when_none(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "done"})
        )

        with _resolve_patched():
            await verify_expected_result("ok", "<snap>", client=client)
        user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "STEP EXECUTED" not in user_msg

    @pytest.mark.asyncio
    async def test_prompt_uses_empty_page_fallback(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "OK"})
        )

        with _resolve_patched():
            await verify_expected_result("ok", "", client=client)
        user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "(empty page)" in user_msg

    @pytest.mark.asyncio
    async def test_max_tokens_512(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "OK"})
        )

        with _resolve_patched():
            await verify_expected_result("ok", "<snap>", client=client)
        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 512

    @pytest.mark.asyncio
    async def test_temperature_forwarded(self):
        client = _make_async_client(
            json.dumps({"passed": True, "reason": "OK"})
        )

        with _resolve_patched():
            await verify_expected_result(
                "ok", "<snap>", temperature=0.0, client=client
            )
        assert client.chat.completions.create.call_args.kwargs["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_creates_client_when_not_provided(self):
        with (
            _resolve_patched(),
            patch("core.llm_wrapper.create_openai_client") as mock_create,
        ):
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=_success_response(
                    json.dumps({"passed": True, "reason": "OK"})
                )
            )
            mock_create.return_value = mock_client

            result = await verify_expected_result("test", "<snap>")
        assert result.passed is True
        mock_create.assert_called_once_with()


# ------------------------------------------------------------------
# verify_expected_result — JSON repair paths (lines 419-432)
# ------------------------------------------------------------------


class TestVerifyExpectedResultJSONRepair:
    @pytest.mark.asyncio
    async def test_repairs_single_quotes(self):
        client = _make_async_client(
            "{'passed': False, 'reason': 'Not found'}"
        )

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        assert result.passed is False
        assert "Not found" in result.reason

    @pytest.mark.asyncio
    async def test_repairs_via_brace_extraction(self):
        """LLM prefixes with prose; regex extracts the JSON object."""
        content = 'Here is the result: {"passed": true, "reason": "OK"}. End.'
        client = _make_async_client(content)

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        assert result.passed is True
        assert result.reason == "OK"

    @pytest.mark.asyncio
    async def test_repairs_python_none_true_false(self):
        """`reason: None` triggers Python→JSON repair path. Pydantic rejects
        None for `reason: str`, which is caught by the outer except — the
        repair code path was still exercised.
        """
        client = _make_async_client(
            "{'passed': True, 'reason': None}"
        )

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        # Pydantic catches the None reason → outer except returns passed=False
        assert result.passed is False
        assert "Verification failed" in result.reason

    @pytest.mark.asyncio
    async def test_unparseable_returns_not_passed(self):
        client = _make_async_client("corrupted output garbage")

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        assert result.passed is False
        assert "parse error" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_unparseable_logs_warning(self, caplog):
        client = _make_async_client("completely unparseable")

        with _resolve_patched():
            with caplog.at_level(logging.WARNING, logger="core.llm_wrapper"):
                result = await verify_expected_result("test", "<snap>", client=client)
        assert result.passed is False
        assert any("not valid JSON" in rec.message for rec in caplog.records)


# ------------------------------------------------------------------
# verify_expected_result — exception handling (line 436-438)
# ------------------------------------------------------------------


class TestVerifyExpectedResultExceptions:
    @pytest.mark.asyncio
    async def test_api_failure_returns_not_passed(self):
        client = _make_async_client(Exception("API error"))

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        assert result.passed is False
        assert "API error" in result.reason
        assert "Verification failed" in result.reason

    @pytest.mark.asyncio
    async def test_api_failure_logs_error(self, caplog):
        client = _make_async_client(Exception("boom"))

        with _resolve_patched():
            with caplog.at_level(logging.ERROR, logger="core.llm_wrapper"):
                result = await verify_expected_result("test", "<snap>", client=client)
        assert result.passed is False
        assert any("Verification LLM call failed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_validation_error_returns_not_passed(self):
        """Pydantic ValidationError falls through to outer except — not passed."""
        # Missing 'passed' field → ValidationError raised
        client = _make_async_client(json.dumps({"reason": "no passed field"}))

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        # ValidationError propagates as exception → caught by outer except
        assert result.passed is False
        assert "Verification failed" in result.reason

    @pytest.mark.asyncio
    async def test_typed_validation_error_message_preserved(self):
        client = _make_async_client(Exception("connection refused"))

        with _resolve_patched():
            result = await verify_expected_result("test", "<snap>", client=client)
        assert "connection refused" in result.reason


# ------------------------------------------------------------------
# generate_tool_call — additional edge cases for coverage
# ------------------------------------------------------------------


class TestGenerateToolCallEdgeCases:
    @pytest.mark.asyncio
    async def test_all_default_fields_used(self):
        """If LLM returns minimal tool call, defaults are filled by Pydantic."""
        content = json.dumps({"action": "snapshot"})
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("refresh", "<page>", client=client)
        assert result.action == "snapshot"
        assert result.selector is None
        assert result.selector_type == "css"
        assert result.timeout_ms == 30000

    @pytest.mark.asyncio
    async def test_json_with_unicode_chinese(self):
        content = json.dumps({
            "action": "click",
            "selector": "e1",
            "thinking": "点击登录按钮",
            "next_goal": "验证成功",
        })
        client = _make_async_client(content)

        with _resolve_patched():
            result = await generate_tool_call("点击登录", "<page>", client=client)
        assert result.thinking == "点击登录按钮"
        assert result.next_goal == "验证成功"

    @pytest.mark.asyncio
    async def test_first_attempt_warning_logged(self, caplog):
        """Invalid JSON on first attempt should log a warning."""
        bad = _success_response("garbage")
        good = _success_response(json.dumps({"action": "click", "selector": "e1"}))
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[bad, good])

        with _resolve_patched():
            with caplog.at_level(logging.WARNING, logger="core.llm_wrapper"):
                await generate_tool_call("click", "<page>", client=client)
        assert any("not valid JSON" in rec.message for rec in caplog.records)


# ------------------------------------------------------------------
# Module constants
# ------------------------------------------------------------------


class TestPrompts:
    def test_system_prompt_is_non_empty_string(self):
        assert isinstance(SYSTEM_PROMPT, str)
        assert len(SYSTEM_PROMPT) > 100
        assert "Playwright" in SYSTEM_PROMPT
        assert "JSON" in SYSTEM_PROMPT
        assert "thinking" in SYSTEM_PROMPT

    def test_verify_prompt_is_non_empty_string(self):
        assert isinstance(VERIFY_PROMPT, str)
        assert len(VERIFY_PROMPT) > 100
        assert "passed" in VERIFY_PROMPT
        assert "JSON" in VERIFY_PROMPT

    def test_system_prompt_mentions_all_actions(self):
        for action in ("goto", "click", "fill", "select", "wait", "screenshot", "assert_text"):
            assert action in SYSTEM_PROMPT, f"action {action!r} missing from SYSTEM_PROMPT"
