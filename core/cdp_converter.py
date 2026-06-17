# core/cdp_converter.py
"""
CDP events to test steps converter.

Converts raw Chrome DevTools Protocol (CDP) RecordedEvent objects captured
by core/cdp_session.py into structured test step definitions
(step_description + expected_result pairs) using an LLM.

Architecture:
  RecordedEvent[] ──→ format_timeline() ──→ LLM (OpenAI-compatible) ──→ TestStep[]
"""

from __future__ import annotations

import json as _json
import logging
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from core.llm_wrapper import _resolve_config, create_openai_client

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# LLM prompt for CDP events → test steps conversion
# ------------------------------------------------------------------

CDP_TO_STEPS_PROMPT = """You are a test case designer analyzing raw browser interaction events captured via Chrome DevTools Protocol (CDP).

INPUT: A chronological timeline of browser events from a real user session. Each event describes what the user did (navigation, click, typing, selection, etc.) with the page URL, page title, target selector, and input value.

YOUR JOB: Group consecutive related events into logical test steps. For each step, produce:
1. "step_description": A concise natural language description in Chinese (e.g. "打开登录页面", "输入用户名 admin", "点击登录按钮").
2. "expected_result": What should happen after this step completes, in Chinese (e.g. "登录页面加载完成", "用户名输入成功", "成功跳转到首页").

EVENT TYPES you may encounter:
- "navigate" / "navigation": User navigated to a new URL. step: "打开 <url 或页面名>", expected: "页面加载完成"
- "click": User clicked an element. step: "点击 <按钮/链接名称>", expected: "点击生效 / 页面响应"
- "input" / "type" / "fill": User typed into a field. step: "在 <字段名> 输入 <值>", expected: "<字段名> 输入成功"
- "change" / "select": User selected a dropdown option. step: "选择 <下拉框> 为 <选项>", expected: "选项已选中"
- "submit" / "form_submit": User submitted a form. step: "提交表单", expected: "表单提交成功"
- "keypress" / "key": Keyboard event. fold into the nearest input/click step.
- "scroll" / "hover" / "focus": Usually auxiliary — only emit a step if standalone meaningful.

RULES:
- Group rapid input events (multiple characters typed into same field) into a single "fill" step.
- A navigation event always starts a new step.
- A click on a submit/login/search button typically expects a result like "页面跳转" or "搜索结果展示".
- Use Chinese for ALL descriptions and expected results. Keep descriptions short and imperative (verb-first).
- Infer expected results from context (e.g. clicking "登录" → "成功登录" or "跳转到首页" if you can tell from the URL/title).
- Do not invent steps that are not represented in the events.
- Output ONLY the JSON array, no markdown fences, no extra text.

OUTPUT SCHEMA (exact JSON):
[
  {"step_description": "打开登录页面", "expected_result": "登录页面加载完成"},
  {"step_description": "输入用户名 admin", "expected_result": "用户名输入成功"},
  {"step_description": "输入密码 123456", "expected_result": "密码输入成功"},
  {"step_description": "点击登录按钮", "expected_result": "成功跳转到首页"}
]"""


# ------------------------------------------------------------------
# Timeline formatting
# ------------------------------------------------------------------


def _format_event_line(idx: int, event: dict) -> str:
    """Format a single event dict into a human-readable timeline line.

    Accepts the dict form of RecordedEvent (from to_dict() or __dict__).
    Tolerates missing keys gracefully.
    """
    event_type = str(event.get('event_type') or event.get('type') or 'unknown')
    page_title = event.get('page_title') or ''
    url = event.get('url') or ''
    selector = event.get('selector') or ''
    value = event.get('value') or ''

    parts: list[str] = [f"[{idx}] type={event_type}"]
    if url:
        parts.append(f"url={url}")
    if page_title:
        parts.append(f"title={page_title}")
    if selector:
        parts.append(f"selector={selector}")
    if value:
        # Truncate long values to keep the timeline readable
        v = str(value)
        if len(v) > 80:
            v = v[:77] + '...'
        parts.append(f"value={v}")
    return ' | '.join(parts)


def _format_timeline(events: list[dict], page_title: str = '') -> str:
    """Format a list of event dicts into a human-readable timeline string."""
    lines: list[str] = []
    if page_title:
        lines.append(f"# Final page title: {page_title}")
    lines.append('# Event timeline (chronological):')
    for idx, ev in enumerate(events, start=1):
        lines.append(_format_event_line(idx, ev))
    return '\n'.join(lines)


# ------------------------------------------------------------------
# JSON repair (mirrors core/llm_wrapper.py)
# ------------------------------------------------------------------


def _repair_and_parse_json(content: str) -> Any:
    """Parse JSON from an LLM response with repair fallbacks.

    Mirrors the JSON repair approach in core/llm_wrapper.py:
    - Strip markdown fences
    - Find first { ... } or [ ... ] region
    - Replace single quotes with double quotes
    - Fix Python None/True/False
    - Fix trailing commas
    - Last resort: ast.literal_eval
    """
    content = content.strip()

    # Strip markdown fences if present
    content = re.sub(r'^```(?:json)?\s*', '', content)
    content = re.sub(r'\s*```$', '', content)

    # Try direct parse
    try:
        return _json.loads(content)
    except _json.JSONDecodeError:
        pass

    # Attempt repair: find first [ and last ] (we expect an array)
    match = re.search(r'\[.*\]', content, re.DOTALL)
    if not match:
        # Fallback to object extraction
        match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        content = match.group(0)

    # Replace all single quotes with double quotes
    content = content.replace("'", '"')

    # Fix Python-style None/True/False
    content = re.sub(r':\s*None\b', ': null', content)
    content = re.sub(r':\s*True\b', ': true', content)
    content = re.sub(r':\s*False\b', ': false', content)

    # Fix trailing commas
    content = re.sub(r',\s*}', '}', content)
    content = re.sub(r',\s*]', ']', content)

    try:
        return _json.loads(content)
    except _json.JSONDecodeError:
        # Last resort: try ast.literal_eval
        import ast
        try:
            return ast.literal_eval(content)
        except (ValueError, SyntaxError):
            raise


# ------------------------------------------------------------------
# Output normalization
# ------------------------------------------------------------------


def _normalize_steps(parsed: Any) -> list[dict]:
    """Normalize LLM output into a list of {step_description, expected_result} dicts.

    Accepts:
    - A list of dicts with the two required keys
    - A dict with a "steps" key wrapping such a list
    - A single dict (wrapped into a one-element list)

    Skips entries missing required keys. Fills in defaults for partial entries.
    """
    if parsed is None:
        return []

    # Unwrap {"steps": [...]} envelope if present
    if isinstance(parsed, dict) and 'steps' in parsed and isinstance(parsed['steps'], list):
        parsed = parsed['steps']

    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        return []

    result: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        desc = item.get('step_description') or item.get('description') or item.get('step')
        expected = item.get('expected_result') or item.get('expected') or item.get('result')
        if not desc:
            # Skip entries that have no description at all
            continue
        # Coerce to strings, default missing expected_result
        result.append({
            'step_description': str(desc).strip(),
            'expected_result': str(expected).strip() if expected else '步骤执行成功',
        })
    return result


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


async def convert_events_to_steps(
    events: list[dict],
    page_title: str = '',
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
    temperature: float = 0.1,
) -> list[dict]:
    """Convert raw CDP events into structured test step definitions.

    Args:
        events: List of event dicts. Each dict represents a RecordedEvent
                (from core/cdp_session.py) and may contain keys:
                event_type, url, selector, value, page_title, screenshot.
                The same shape as RecordedEvent.to_dict() or
                RecordedEvent.__dict__ is accepted.
        page_title: Optional final page title for additional LLM context.
        model: Override the default LLM model (resolved from DB config).
        temperature: LLM temperature (lower = more deterministic).
        client: Pre-configured AsyncOpenAI client. Created from DB config
                via core.llm_wrapper.create_openai_client() if not provided.

    Returns:
        A list of {"step_description": str, "expected_result": str} dicts.
        Returns an empty list if `events` is empty, or if the LLM output
        cannot be parsed after the retry budget is exhausted.
    """
    if not events:
        return []

    if client is None:
        client = create_openai_client()

    _, _, resolved_model = _resolve_config(explicit_model=model)

    timeline = _format_timeline(events, page_title=page_title)

    user_message = (
        f"{timeline}\n\n"
        f"Group the events above into logical test steps. "
        f"Output ONLY the JSON array, no markdown fences."
    )

    messages: list[dict] = [
        {'role': 'system', 'content': CDP_TO_STEPS_PROMPT},
        {'role': 'user', 'content': user_message},
    ]

    # Up to 2 retries on parse/validation failures (3 total attempts)
    last_error: Optional[str] = None
    for attempt in range(3):
        if attempt > 0 and last_error:
            messages.append({
                'role': 'user',
                'content': (
                    f"Previous response was invalid: {last_error}\n"
                    f"Please output ONLY a valid JSON array matching the schema."
                ),
            })

        try:
            response = await client.chat.completions.create(
                model=resolved_model,
                messages=messages,
                temperature=temperature,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.exception("CDP converter LLM call failed (attempt %s)", attempt + 1)
            if attempt >= 2:
                logger.error("CDP converter giving up after 3 API failures")
                return []
            last_error = f"API error: {exc}"
            continue

        content = response.choices[0].message.content or ''
        content = content.strip()

        try:
            parsed = _repair_and_parse_json(content)
        except (_json.JSONDecodeError, ValueError, SyntaxError) as exc:
            last_error = f"JSON parse error: {exc}"
            logger.warning(
                f"CDP converter LLM output not valid JSON (attempt {attempt + 1}): "
                f"{content[:200]}"
            )
            continue

        steps = _normalize_steps(parsed)
        if not steps:
            last_error = "Output contained no valid step entries"
            logger.warning(
                f"CDP converter LLM output produced no steps (attempt {attempt + 1}): "
                f"{content[:200]}"
            )
            continue

        return steps

    logger.error(
        f"CDP converter failed to produce valid steps after 3 attempts. Last error: {last_error}"
    )
    return []
