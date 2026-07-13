# tests/unit/core/test_cdp_converter.py
"""Comprehensive unit tests for core/cdp_converter.py.

Covers all non-LLM logic:
- Timeline formatting helpers: _format_event_line, _format_timeline
- JSON repair: _repair_and_parse_json
- Output normalization: _normalize_steps
- Public async LLM entry point with mocked AsyncOpenAI client:
  convert_events_to_steps

The LLM is never actually called; all responses are supplied through mocks.
"""

import ast
import json as _json
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.cdp_converter import (
    CDP_TO_STEPS_PROMPT,
    _format_event_line,
    _format_timeline,
    _repair_and_parse_json,
    _normalize_steps,
    convert_events_to_steps,
)


# ------------------------------------------------------------------
# _format_event_line
# ------------------------------------------------------------------


class TestFormatEventLine:
    """Tests for the single-event timeline formatter."""

    def test_basic(self):
        """All standard fields are rendered in the expected order."""
        event = {
            "event_type": "click",
            "url": "https://example.com/login",
            "page_title": "Login",
            "selector": "#submit",
            "value": "Sign in",
        }
        line = _format_event_line(1, event)

        assert line.startswith("[1] type=click")
        assert "url=https://example.com/login" in line
        assert "title=Login" in line
        assert "selector=#submit" in line
        assert "value=Sign in" in line
        # Parts are joined with ' | ' so the format must be coherent.
        assert " | " in line

    def test_missing_fields(self):
        """Missing keys are silently skipped; no exceptions are raised."""
        event = {"event_type": "click", "selector": "#btn"}
        line = _format_event_line(2, event)

        assert line.startswith("[2] type=click")
        assert "selector=#btn" in line
        # These keys are absent and must not appear.
        assert "url=" not in line
        assert "title=" not in line
        assert "value=" not in line

    def test_truncated_value(self):
        """Values longer than 80 characters are truncated to 77 chars + '...'."""
        long_value = "a" * 100
        event = {"event_type": "input", "value": long_value}
        line = _format_event_line(1, event)

        # Truncation marker must be present.
        assert "..." in line
        # Full value must NOT appear verbatim.
        assert long_value not in line
        # 77 'a's + '...' = exactly 80 chars in the rendered value fragment.
        assert "value=" + ("a" * 77) + "..." in line

    def test_unknown_event_type(self):
        """Missing event_type / type defaults to 'unknown'."""
        event = {"url": "https://example.com", "selector": "body"}
        line = _format_event_line(5, event)

        # Falls back to 'unknown' when neither key is present.
        assert "type=unknown" in line
        assert "url=https://example.com" in line
        assert "selector=body" in line

    def test_type_alias_fallback(self):
        """`type` is accepted as an alias for `event_type`."""
        event = {"type": "navigation", "url": "https://example.com"}
        line = _format_event_line(1, event)

        # 'type' key is used when 'event_type' is absent.
        assert "type=navigation" in line

    def test_empty_event(self):
        """An empty dict renders a minimal line with the unknown type."""
        line = _format_event_line(1, {})

        # No keys present → only the event type is rendered.
        assert line == "[1] type=unknown"

    def test_falsy_values_ignored(self):
        """Empty strings / falsy values are dropped from output."""
        event = {
            "event_type": "click",
            "url": "",
            "page_title": "",
            "selector": "",
            "value": "",
        }
        line = _format_event_line(1, event)

        assert line == "[1] type=click"


# ------------------------------------------------------------------
# _format_timeline
# ------------------------------------------------------------------


class TestFormatTimeline:
    """Tests for the multi-event timeline aggregator."""

    def test_basic(self):
        """Multiple events are joined with newlines, with chronological indices."""
        events = [
            {"event_type": "navigation", "url": "https://a.test"},
            {"event_type": "click", "selector": "#btn"},
            {"event_type": "input", "selector": "#q", "value": "x"},
        ]
        timeline = _format_timeline(events)

        # Header is always present.
        assert "# Event timeline (chronological):" in timeline
        # Indices are 1-based and monotonic.
        assert "[1] type=navigation" in timeline
        assert "[2] type=click" in timeline
        assert "[3] type=input" in timeline
        # Lines are newline-separated.
        lines = timeline.split("\n")
        assert len(lines) == 4  # header + 3 events

    def test_with_page_title(self):
        """When page_title is provided, it is prepended as a header line."""
        events = [{"event_type": "click", "selector": "#btn"}]
        timeline = _format_timeline(events, page_title="Welcome page")

        # Title line precedes the events header.
        title_idx = timeline.find("# Final page title: Welcome page")
        header_idx = timeline.find("# Event timeline (chronological):")
        assert title_idx == 0
        assert title_idx < header_idx

    def test_empty_events(self):
        """An empty event list still yields a valid header-only timeline."""
        timeline = _format_timeline([])

        # Header is present, no event lines.
        assert "# Event timeline (chronological):" in timeline
        # Only the header line is rendered.
        assert timeline == "# Event timeline (chronological):"

    def test_empty_events_with_page_title(self):
        """Empty events + page_title → only the title and header, no events."""
        timeline = _format_timeline([], page_title="Landing")

        assert "# Final page title: Landing" in timeline
        assert "# Event timeline (chronological):" in timeline
        # No event line markers.
        assert "[" not in timeline

    def test_single_event(self):
        """A single-event timeline is rendered with index 1."""
        events = [{"event_type": "click", "selector": "#x"}]
        timeline = _format_timeline(events)

        assert "[1] type=click" in timeline
        assert "selector=#x" in timeline


# ------------------------------------------------------------------
# _repair_and_parse_json
# ------------------------------------------------------------------


class TestRepairAndParseJson:
    """Tests for the LLM JSON repair utility."""

    def test_valid_json(self):
        """A well-formed JSON array parses on the first try."""
        content = '[{"a": 1}, {"a": 2}]'
        result = _repair_and_parse_json(content)

        assert result == [{"a": 1}, {"a": 2}]

    def test_valid_json_object(self):
        """Top-level objects parse on the first try (no repair needed)."""
        content = '{"steps": [{"x": 1}]}'
        result = _repair_and_parse_json(content)

        assert result == {"steps": [{"x": 1}]}

    def test_markdown_fenced(self):
        """```json ... ``` fences are stripped before parsing."""
        content = "```json\n[{\"a\": 1}]\n```"
        result = _repair_and_parse_json(content)

        assert result == [{"a": 1}]

    def test_markdown_fenced_no_lang(self):
        """Plain ``` fences (no language tag) are also stripped."""
        content = "```\n[{\"a\": 2}]\n```"
        result = _repair_and_parse_json(content)

        assert result == [{"a": 2}]

    def test_single_quotes(self):
        """Single-quoted JSON-like text is repaired to valid JSON."""
        content = "[{'a': 1, 'b': 'two'}]"
        result = _repair_and_parse_json(content)

        assert result == [{"a": 1, "b": "two"}]

    def test_none_true_false(self):
        """Python-style None/True/False are normalized to JSON equivalents."""
        content = "[{'x': None, 'y': True, 'z': False}]"
        result = _repair_and_parse_json(content)

        assert result == [{"x": None, "y": True, "z": False}]

    def test_trailing_commas(self):
        """Trailing commas in arrays/objects are removed."""
        content = '[{"a": 1,}, {"b": 2,},]'
        result = _repair_and_parse_json(content)

        assert result == [{"a": 1}, {"b": 2}]

    def test_array_extraction(self):
        """When prose surrounds the JSON, only the [...] region is used."""
        content = "Here you go:\n[{\"a\": 1}, {\"a\": 2}]\nThanks!"
        result = _repair_and_parse_json(content)

        assert result == [{"a": 1}, {"a": 2}]

    def test_object_extraction(self):
        """When no [...] is present, a top-level {...} region is extracted."""
        content = "Result: {\"x\": 1, \"y\": 2} done"
        result = _repair_and_parse_json(content)

        assert result == {"x": 1, "y": 2}

    def test_invalid_content_raises(self):
        """Content that cannot be repaired raises a JSON decode error."""
        content = "this is not json at all !!!"

        with pytest.raises((_json.JSONDecodeError, ValueError, SyntaxError)):
            _repair_and_parse_json(content)

    def test_ast_fallback_for_tuple(self):
        """Single-quoted Python-literal structures fall back to ast.literal_eval."""
        content = "[('a', 1)]"
        result = _repair_and_parse_json(content)

        # ast.literal_eval preserves Python tuple types.
        assert result == [("a", 1)]
        assert isinstance(result, list)
        assert isinstance(result[0], tuple)


# ------------------------------------------------------------------
# _normalize_steps
# ------------------------------------------------------------------


class TestNormalizeSteps:
    """Tests for the LLM-output normalizer."""

    def test_valid_list(self):
        """A list of well-formed dicts is returned with trimmed strings."""
        parsed = [
            {"step_description": "  打开页面  ", "expected_result": "  页面加载  "},
            {"step_description": "点击登录", "expected_result": "跳转"},
        ]
        result = _normalize_steps(parsed)

        assert result == [
            {"step_description": "打开页面", "expected_result": "页面加载"},
            {"step_description": "点击登录", "expected_result": "跳转"},
        ]

    def test_steps_envelope(self):
        """A dict with a 'steps' key is unwrapped into the list form."""
        parsed = {"steps": [{"step_description": "A", "expected_result": "a"}]}
        result = _normalize_steps(parsed)

        assert result == [{"step_description": "A", "expected_result": "a"}]

    def test_steps_envelope_non_list_treated_as_single_dict(self):
        """A 'steps' key whose value is not a list is treated as a single dict."""
        parsed = {"step_description": "A", "expected_result": "a", "extra": "x"}
        result = _normalize_steps(parsed)

        # The whole dict is wrapped into a list (no 'steps' unwrapping path).
        assert result == [{"step_description": "A", "expected_result": "a"}]

    def test_single_dict(self):
        """A single dict is wrapped into a one-element list."""
        parsed = {"step_description": "点击", "expected_result": "成功"}
        result = _normalize_steps(parsed)

        assert result == [{"step_description": "点击", "expected_result": "成功"}]

    def test_missing_keys(self):
        """Entries without a step_description are dropped."""
        parsed = [
            {"step_description": "A", "expected_result": "a"},
            {"expected_result": "no description"},  # skipped: no step_description
            {"step_description": "C", "expected_result": "c"},
            {"foo": "bar"},  # skipped: no step_description
        ]
        result = _normalize_steps(parsed)

        assert result == [
            {"step_description": "A", "expected_result": "a"},
            {"step_description": "C", "expected_result": "c"},
        ]

    def test_alternative_keys(self):
        """`description`/`step` and `expected`/`result` aliases are accepted."""
        parsed = [
            {"description": "X", "expected": "x"},
            {"step": "Y", "result": "y"},
            {"step_description": "Z", "expected_result": "z"},
        ]
        result = _normalize_steps(parsed)

        assert result == [
            {"step_description": "X", "expected_result": "x"},
            {"step_description": "Y", "expected_result": "y"},
            {"step_description": "Z", "expected_result": "z"},
        ]

    def test_empty_list(self):
        """An empty list returns an empty list."""
        assert _normalize_steps([]) == []

    def test_none(self):
        """None returns an empty list (not an error)."""
        assert _normalize_steps(None) == []

    def test_non_list_non_dict(self):
        """Non-list, non-dict inputs return an empty list."""
        assert _normalize_steps("string") == []
        assert _normalize_steps(42) == []
        assert _normalize_steps(3.14) == []

    def test_partial_entry(self):
        """Missing expected_result gets the default placeholder."""
        parsed = [{"step_description": "Only description"}]
        result = _normalize_steps(parsed)

        assert result == [
            {"step_description": "Only description", "expected_result": "步骤执行成功"},
        ]

    def test_extra_keys_ignored(self):
        """Extra keys in entries are dropped from the output."""
        parsed = [
            {
                "step_description": "A",
                "expected_result": "a",
                "extra": "ignored",
                "screenshot": "base64-blob",
            }
        ]
        result = _normalize_steps(parsed)

        assert result == [{"step_description": "A", "expected_result": "a"}]

    def test_non_dict_items_skipped(self):
        """Non-dict items inside the list are skipped."""
        parsed = [
            "not a dict",
            42,
            None,
            {"step_description": "OK", "expected_result": "ok"},
        ]
        result = _normalize_steps(parsed)

        assert result == [{"step_description": "OK", "expected_result": "ok"}]


# ------------------------------------------------------------------
# convert_events_to_steps (mocked LLM)
# ------------------------------------------------------------------


def _build_mock_client(content=None, side_effect=None):
    """Construct a mock AsyncOpenAI client that returns a given content string.

    Either `content` (str) is wrapped into a normal response object, or
    `side_effect` is forwarded to the underlying AsyncMock so callers can
    raise exceptions or return multiple values.
    """
    client = MagicMock()

    if side_effect is not None:
        client.chat.completions.create = AsyncMock(side_effect=side_effect)
        return client

    response = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    response.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


class TestConvertEventsToSteps:
    """Tests for the async public API (with a mocked AsyncOpenAI client)."""

    @pytest.fixture(autouse=True)
    def stub_resolve_config(self, monkeypatch):
        """Skip database access by stubbing the LLM config resolver."""
        monkeypatch.setattr(
            "core.cdp_converter._resolve_config",
            AsyncMock(return_value=(
                "fake-key",
                "https://fake.example.com",
                "fake-model",
            )),
        )

    @pytest.mark.asyncio
    async def test_empty_events(self):
        """Empty events return [] without invoking any LLM client."""
        # The client should NOT be called at all.
        client = MagicMock()
        client.chat.completions.create = AsyncMock()

        result = await convert_events_to_steps([], client=client)

        assert result == []
        client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_with_mocked_client(self):
        """A successful LLM response is parsed and normalized into steps."""
        client = _build_mock_client(
            content='[{"step_description": "打开页面", "expected_result": "页面加载"}]'
        )

        events = [{"event_type": "navigation", "url": "https://example.com"}]
        steps = await convert_events_to_steps(
            events, page_title="Test", client=client
        )

        assert len(steps) == 1
        assert steps[0]["step_description"] == "打开页面"
        assert steps[0]["expected_result"] == "页面加载"
        # The LLM was called exactly once on a clean response.
        assert client.chat.completions.create.await_count == 1

    @pytest.mark.asyncio
    async def test_api_failure_returns_empty(self):
        """When the API keeps failing, retries happen and [] is returned."""
        client = _build_mock_client(
            side_effect=OSError("network down")
        )

        events = [{"event_type": "click", "selector": "#btn"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == []
        # Up to 3 attempts are made (the loop range is 3).
        assert client.chat.completions.create.await_count == 3

    @pytest.mark.asyncio
    async def test_api_failure_then_success(self):
        """A first-attempt API failure followed by a successful response."""
        success_response = MagicMock()
        success_choice = MagicMock()
        success_choice.message.content = (
            '[{"step_description": "ok", "expected_result": "done"}]'
        )
        success_response.choices = [success_choice]

        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[OSError("transient"), success_response]
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == [{"step_description": "ok", "expected_result": "done"}]
        # Two attempts total: one failure, one success.
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_unparseable_response_retries(self):
        """Unparseable LLM output triggers a retry; success on attempt 2."""
        success_response = MagicMock()
        success_choice = MagicMock()
        success_choice.message.content = (
            '[{"step_description": "retry-ok", "expected_result": "done"}]'
        )
        success_response.choices = [success_choice]

        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                # First attempt: garbage output that JSON repair cannot fix.
                MagicMock(
                    choices=[
                        MagicMock(message=MagicMock(content="garbage not json !!!"))
                    ]
                ),
                success_response,
            ]
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == [
            {"step_description": "retry-ok", "expected_result": "done"}
        ]
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_normalized_output_retries(self):
        """A response that parses but normalizes to [] is retried."""
        success_response = MagicMock()
        success_choice = MagicMock()
        success_choice.message.content = (
            '[{"step_description": "ok", "expected_result": "done"}]'
        )
        success_response.choices = [success_choice]

        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                # First attempt: valid JSON, but the only entry lacks a description
                # so _normalize_steps drops it → empty list → retry.
                MagicMock(
                    choices=[
                        MagicMock(
                            message=MagicMock(
                                content='[{"expected_result": "no description"}]'
                            )
                        )
                    ]
                ),
                success_response,
            ]
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == [{"step_description": "ok", "expected_result": "done"}]
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_fenced_markdown_response(self):
        """A ```json-fenced response is repaired and normalized correctly."""
        client = _build_mock_client(
            content=(
                "```json\n"
                '[{"step_description": "fenced", "expected_result": "ok"}]\n'
                "```"
            )
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == [{"step_description": "fenced", "expected_result": "ok"}]

    @pytest.mark.asyncio
    async def test_steps_envelope_response(self):
        """A {"steps": [...]} envelope is unwrapped by the normalizer."""
        client = _build_mock_client(
            content=(
                '{"steps": [{"step_description": "env", "expected_result": "ok"}]}'
            )
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == [{"step_description": "env", "expected_result": "ok"}]

    @pytest.mark.asyncio
    async def test_three_consecutive_parse_failures(self):
        """Three unparseable responses in a row still return [] after retries."""
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                MagicMock(
                    choices=[
                        MagicMock(message=MagicMock(content="not json 1"))
                    ]
                ),
                MagicMock(
                    choices=[
                        MagicMock(message=MagicMock(content="not json 2"))
                    ]
                ),
                MagicMock(
                    choices=[
                        MagicMock(message=MagicMock(content="not json 3"))
                    ]
                ),
            ]
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events, client=client)

        assert result == []
        assert client.chat.completions.create.await_count == 3

    @pytest.mark.asyncio
    async def test_default_client_created_when_not_provided(
        self, monkeypatch
    ):
        """Without a client, create_openai_client is invoked to build one."""
        mock_client = _build_mock_client(
            content='[{"step_description": "auto", "expected_result": "ok"}]'
        )
        monkeypatch.setattr(
            "core.cdp_converter.create_openai_client",
            AsyncMock(return_value=mock_client),
        )

        events = [{"event_type": "click"}]
        result = await convert_events_to_steps(events)

        assert result == [{"step_description": "auto", "expected_result": "ok"}]
        assert mock_client.chat.completions.create.await_count == 1
