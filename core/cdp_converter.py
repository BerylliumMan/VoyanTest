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

import asyncio
import json as _json
import logging
import re
from typing import Any, Optional

import openai
from openai import AsyncOpenAI

from core.llm_wrapper import _resolve_config, create_openai_client

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# LLM prompt for CDP events → test steps conversion
# ------------------------------------------------------------------

CDP_TO_STEPS_PROMPT = """你是 UI 自动化测试工程师。将 CDP 录制事件转为**浏览器可执行**的 StructuredStep。

输入：按时间排序的浏览器事件（navigate/click/input…），含 url、selector、value。

硬规则：
- 一步 = 一个浏览器动作；合并同一输入框的连续 input。
- **禁止**只输出自然语言散文；每步必须含 action / target_name（及 fill/select 的 value）。
- target_name 只写页面可见文案 / aria-label；禁止「按钮」「输入框」「下拉框」等类型词，禁止省略号。
- 下拉筛选：先 click(textbox，target_name 用完整 placeholder 如「请选择单位」) 展开 → fill 筛选词 → select/click 选项。
- selector 优先固化稳定属性（如 input[placeholder=\"请选择单位\"]）；禁止裸 input；禁止把 placeholder 截成过短的「单位」。
- 输出 ONLY JSON 数组，无 Markdown。

OUTPUT SCHEMA：
[
  {"action":"goto","target_name":"登录","value":"http://example.com/login","expected_result":"页面加载完成"},
  {"action":"click","target_name":"请选择单位","target_role":"textbox","selector":"input[placeholder=\\"请选择单位\\"]","expected_result":"下拉展开"},
  {"action":"fill","target_name":"请选择单位","target_role":"textbox","value":"京州市院","expected_result":"输入成功"},
  {"action":"select","target_name":"京州市院","target_role":"option","value":"京州市院","expected_result":"选项已选中"},
  {"action":"fill","target_name":"用户名","target_role":"textbox","value":"admin","expected_result":"输入成功"},
  {"action":"fill","target_name":"密码","target_role":"textbox","value":"***","expected_result":"输入成功"},
  {"action":"click","target_name":"登录","target_role":"button","expected_result":"登录成功或页面跳转"}
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
    """Normalize LLM/rule output into UI-executable step dicts.

    Each item preferably has StructuredStep fields (action/target_name/…) plus
    step_description / expected_result for display.
    """
    if parsed is None:
        return []

    from core.step_normalize import coerce_structured_step, render_structured_step

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
        expected = item.get('expected_result') or item.get('expected') or item.get('result')
        structured = coerce_structured_step(item)
        if structured is None and item.get("action"):
            structured = {
                k: item.get(k)
                for k in (
                    "action", "target_name", "target_role", "value",
                    "disambiguation", "icon_hint", "frame_hint", "note",
                    "selector",
                )
                if item.get(k) not in (None, "")
            }
        desc = item.get('step_description') or item.get('description') or item.get('step')
        if structured:
            desc = desc or render_structured_step(structured)
        if not desc and not structured:
            continue
        row = {
            'step_description': str(desc or "").strip(),
            'expected_result': str(expected).strip() if expected else '步骤执行成功',
        }
        if structured:
            row['structured_step'] = structured
            row['action'] = structured.get('action')
            row['target_name'] = structured.get('target_name')
            row['target_role'] = structured.get('target_role')
            row['value'] = structured.get('value')
            if structured.get('selector'):
                row['selector'] = structured.get('selector')
            if not row['step_description']:
                row['step_description'] = render_structured_step(structured) or ""
        result.append(row)
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
    agent_type: str = "recording",
    agent_id: int | None = None,
) -> list[dict]:
    """Convert raw CDP events into structured test step definitions.

    Uses the active *recording* AgentDefinition (llm_config + prompt) when
    available; falls back to global AI config and ``CDP_TO_STEPS_PROMPT``.
    """
    if not events:
        return []

    # Prefer deterministic StructuredStep mapping — recording must be UI-executable.
    try:
        from core.cdp_events_to_steps import events_to_structured_steps

        rule_steps = events_to_structured_steps(events)
        if rule_steps:
            logger.info(
                "CDP convert: using rule-based StructuredStep path (%d steps)",
                len(rule_steps),
            )
            return rule_steps
    except Exception:
        logger.exception("rule-based CDP convert failed; falling back to LLM")

    system_prompt = CDP_TO_STEPS_PROMPT
    resolved_model = model
    try:
        from app.database import AsyncSessionLocal
        from app.crud.agent_definition import get_active_by_type, get_agent_definition
        from app.crud.prompt_template import get_prompt_template_by_key

        async with AsyncSessionLocal() as db:
            if client is None:
                client = await create_openai_client(agent_type=agent_type)
            _, _, cfg_model = await _resolve_config(
                explicit_model=model, agent_type=agent_type,
            )
            if resolved_model is None:
                resolved_model = cfg_model

            agent_def = None
            if agent_id is not None:
                agent_def = await get_agent_definition(db, agent_id)
            if agent_def is None:
                agent_def = await get_active_by_type(db, agent_type)

            from app.runtime_config import resolve_prompt_for_agent

            resolved_body = None
            for prompt_key in ("cdp_convert", "recording_convert"):
                try:
                    body = await resolve_prompt_for_agent(
                        db, agent_type, prompt_key, agent_id=agent_id,
                    )
                    if body and str(body).strip():
                        resolved_body = str(body).strip()
                        break
                except Exception:
                    logger.debug("resolve %s failed", prompt_key, exc_info=True)
                    pt = await get_prompt_template_by_key(db, prompt_key)
                    if pt and (pt.content or "").strip():
                        role = (agent_def.system_prompt or "").strip() if agent_def else ""
                        body = pt.content.strip()
                        resolved_body = f"{role}\n\n{body}" if role else body
                        break

            if resolved_body:
                system_prompt = resolved_body
            elif agent_def and (agent_def.system_prompt or "").strip():
                system_prompt = (
                    f"{agent_def.system_prompt.strip()}\n\n{CDP_TO_STEPS_PROMPT}"
                )
    except Exception:
        logger.debug("recording Agent 解析失败，回退全局配置", exc_info=True)
        if client is None:
            client = await create_openai_client()
        if resolved_model is None:
            _, _, resolved_model = await _resolve_config(explicit_model=model)

    timeline = _format_timeline(events, page_title=page_title)

    user_message = (
        f"{timeline}\n\n"
        f"Group the events above into logical test steps. "
        f"Output ONLY the JSON array, no markdown fences."
    )

    messages: list[dict] = [
        {'role': 'system', 'content': system_prompt},
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
        except (openai.OpenAIError, asyncio.TimeoutError, OSError) as exc:
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
