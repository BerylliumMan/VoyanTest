# core/step_intent.py
"""Two-phase step resolution inspired by Midscene deepThink / Instant Action.

Phase 1: LLM emits Intent (action + target labels) — never ephemeral refs.
Phase 2: Deterministic AX snapshot match; on 0/>1 matches, optional vision disambiguation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
from typing import Any, Optional

from pydantic import BaseModel, Field

from core.locator_memory import parse_snapshot_elements

logger = logging.getLogger(__name__)

_BRACKET_RE = re.compile(r"【([^】]+)】")
_GUILLEMET_RE = re.compile(r"「([^」]+)」")

INTENT_SYSTEM_PROMPT = """You extract a single browser Intent from a test step. The step text is authoritative.

Output ONLY JSON (no markdown) matching:
{
  "action": "click|fill|select|wait|goto|hover|press_key|scroll|assert_text|error",
  "target_role": "button|link|textbox|combobox|option|menuitem|checkbox|radio|null",
  "target_name": "exact UI label from 【】/「」 or step text",
  "value": "fill/select/wait/goto value or null",
  "confidence": 0.0-1.0,
  "ambiguous": false,
  "thinking": "quote the label you chose and what you refuse to click"
}

Rules:
- NEVER invent refs (e15) or CSS selectors.
- Prefer 【】/「」 text as target_name. Control-type words (下拉框/输入框/按钮) are NOT target_name.
- 提交≠确定≠保存; 查询≠搜索; 取消≠关闭.
- One primary action only. If the step is wait/assert, action=wait or assert_text and value=text.
- If you cannot decide safely, action=error and explain in thinking; set ambiguous=true.
"""

VISION_DISAMBIGUATE_PROMPT = """You pick ONE candidate element for the step. Candidates are listed with refs from the accessibility tree.

Output ONLY JSON:
{"ref": "e12", "thinking": "why this candidate"}
or {"ref": null, "thinking": "why none"}

Rules: Prefer exact 【】/「」 match. Never pick a similar wrong label. If unsure, ref=null.
"""


class StepIntent(BaseModel):
    action: str = Field(..., description="Browser action type")
    target_role: Optional[str] = None
    target_name: Optional[str] = None
    value: Optional[str] = None
    confidence: float = 0.0
    ambiguous: bool = False
    thinking: str = ""


class StepPreview(BaseModel):
    """Debug preview of what would be acted on (Stagehand observe-style)."""

    action: str
    target_role: Optional[str] = None
    target_name: Optional[str] = None
    value: Optional[str] = None
    ref: Optional[str] = None
    match_count: int = 0
    candidates: list[dict[str, str]] = Field(default_factory=list)
    thinking: str = ""
    needs_vision: bool = False


def extract_label_hints(step_description: str) -> list[str]:
    labels: list[str] = []
    for rx in (_BRACKET_RE, _GUILLEMET_RE):
        for m in rx.finditer(step_description or ""):
            t = (m.group(1) or "").strip()
            if t and t not in labels:
                labels.append(t)
    return labels


def match_intent_candidates(
    snapshot: str,
    intent: StepIntent,
) -> list[dict[str, str]]:
    """Return AX elements matching intent role/name (exact name preferred)."""
    role = (intent.target_role or "").strip().lower() or None
    name = (intent.target_name or "").strip()
    if not name and not role:
        return []

    exact: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    for el in parse_snapshot_elements(snapshot):
        if role and el["role"] != role:
            continue
        el_name = el.get("name") or ""
        if name:
            if el_name == name:
                exact.append(el)
            elif name in el_name or el_name in name:
                partial.append(el)
        else:
            exact.append(el)
    return exact if exact else partial


def intent_to_tool_call(
    intent: StepIntent,
    *,
    ref: str | None,
    timeout_ms: int = 30000,
):
    from core.llm_wrapper import PlaywrightMCPToolCall

    action = (intent.action or "click").strip().lower()
    thinking = intent.thinking or ""
    if action in ("wait", "assert_text"):
        return PlaywrightMCPToolCall(
            action=action if action != "assert_text" else "assert_text",
            selector=None,
            value=intent.value or intent.target_name,
            timeout_ms=timeout_ms,
            thinking=thinking,
            next_goal="verify this step only",
        )
    if action == "goto":
        return PlaywrightMCPToolCall(
            action="goto",
            selector=None,
            value=intent.value,
            timeout_ms=timeout_ms,
            thinking=thinking,
        )
    if action == "press_key":
        return PlaywrightMCPToolCall(
            action="press_key",
            selector=None,
            value=intent.value,
            timeout_ms=timeout_ms,
            thinking=thinking,
        )
    if action == "error" or not ref:
        return PlaywrightMCPToolCall(
            action="error",
            selector=None,
            value=intent.value or thinking or "unable to resolve target",
            timeout_ms=timeout_ms,
            thinking=thinking,
        )
    return PlaywrightMCPToolCall(
        action=action,
        selector=ref,
        value=intent.value,
        timeout_ms=timeout_ms,
        thinking=thinking + f" | bound ref={ref} name={intent.target_name}",
        next_goal="verify this step only",
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        data = json.loads(m.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError(f"cannot parse intent JSON: {text[:200]}")


async def generate_step_intent(
    step_description: str,
    snapshot: str,
    *,
    expected_result: str | None = None,
    client=None,
    model: str | None = None,
) -> StepIntent:
    """Phase 1: LLM intent without refs."""
    from core.llm_wrapper import create_openai_client, _resolve_config

    if client is None:
        client = await create_openai_client()
    _, _, resolved_model = await _resolve_config(explicit_model=model)

    labels = extract_label_hints(step_description)
    user = (
        f"STEP:\n{step_description}\n\n"
        f"LABEL HINTS FROM BRACKETS: {labels or '(none)'}\n\n"
        f"PAGE SNAPSHOT (for context only — do NOT output refs):\n"
        f"{(snapshot or '')[:6000]}\n\n"
    )
    if expected_result:
        user += f"EXPECTED (hint only):\n{expected_result}\n\n"
    user += "Emit Intent JSON now."

    resp = await client.chat.completions.create(
        model=resolved_model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_object(raw)
    return StepIntent.model_validate(data)


async def _screenshot_base64(mcp_manager) -> str | None:
    """Capture page screenshot as base64 for vision disambiguation."""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="vt_vision_")
    os.close(fd)
    try:
        saved = await mcp_manager.take_screenshot(path)
        if not saved or not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception as exc:
        logger.warning("vision screenshot failed: %s", exc)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def disambiguate_with_vision(
    step_description: str,
    intent: StepIntent,
    candidates: list[dict[str, str]],
    *,
    mcp_manager,
    client=None,
    model: str | None = None,
) -> str | None:
    """Phase 2b: pick one candidate ref using screenshot + candidate list."""
    from core.llm_wrapper import create_openai_client, _resolve_config

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]["ref"]

    if client is None:
        client = await create_openai_client()
    _, _, resolved_model = await _resolve_config(explicit_model=model)

    cand_lines = "\n".join(
        f"- ref={c['ref']} role={c.get('role')} name={c.get('name')!r}"
        for c in candidates[:12]
    )
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"STEP: {step_description}\n"
                f"INTENT: action={intent.action} role={intent.target_role} "
                f"name={intent.target_name!r} value={intent.value!r}\n"
                f"CANDIDATES:\n{cand_lines}\n"
                "Pick exactly one ref or null."
            ),
        }
    ]
    b64 = await _screenshot_base64(mcp_manager)
    if b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    try:
        resp = await client.chat.completions.create(
            model=resolved_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": VISION_DISAMBIGUATE_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = _parse_json_object(raw)
        ref = data.get("ref")
        allowed = {c["ref"] for c in candidates}
        if isinstance(ref, str) and ref in allowed:
            logger.info(
                "vision disambiguate chose ref=%s thinking=%s",
                ref, str(data.get("thinking") or "")[:120],
            )
            return ref
        logger.info("vision disambiguate declined: %s", str(data.get("thinking") or "")[:160])
    except Exception as exc:
        logger.warning("vision disambiguate failed: %s", exc, exc_info=True)
    return None


async def resolve_tool_call_from_step(
    step_description: str,
    snapshot: str,
    *,
    expected_result: str | None = None,
    mcp_manager=None,
    client=None,
    model: str | None = None,
    system_prompt: str | None = None,  # kept for API compat; unused in two-phase
    use_vision_fallback: bool = True,
    timeout_ms: int = 30000,
):
    """Resolve NL step → PlaywrightMCPToolCall via Intent + deterministic bind (+ vision)."""
    from core.llm_wrapper import PlaywrightMCPToolCall, generate_tool_call

    # Fast path without mcp: keep legacy one-shot for unit tests / callers without manager
    if mcp_manager is None:
        return await generate_tool_call(
            step_description,
            snapshot,
            expected_result=expected_result,
            client=client,
            model=model,
            system_prompt=system_prompt,
        )

    try:
        intent = await generate_step_intent(
            step_description,
            snapshot,
            expected_result=expected_result,
            client=client,
            model=model,
        )
    except Exception as exc:
        logger.warning("intent generation failed, fallback legacy: %s", exc)
        return await generate_tool_call(
            step_description,
            snapshot,
            expected_result=expected_result,
            client=client,
            model=model,
            system_prompt=system_prompt,
        )

    action = (intent.action or "").lower()
    if action in ("wait", "assert_text", "goto", "press_key", "scroll", "screenshot"):
        return intent_to_tool_call(intent, ref=None, timeout_ms=timeout_ms)
    if action == "error" or intent.ambiguous:
        return intent_to_tool_call(intent, ref=None, timeout_ms=timeout_ms)

    candidates = match_intent_candidates(snapshot, intent)
    ref: str | None = None
    if len(candidates) == 1:
        ref = candidates[0]["ref"]
    elif len(candidates) != 1 and use_vision_fallback:
        logger.info(
            "intent match count=%s for name=%r — vision fallback",
            len(candidates), intent.target_name,
        )
        # If zero matches, broaden to name-only across roles for vision list
        pool = candidates
        if not pool and intent.target_name:
            broadened = StepIntent(
                action=intent.action,
                target_role=None,
                target_name=intent.target_name,
                value=intent.value,
                thinking=intent.thinking,
            )
            pool = match_intent_candidates(snapshot, broadened)[:12]
        if not pool:
            # Last resort: include same-role elements (capped)
            role = (intent.target_role or "").lower()
            pool = [
                el for el in parse_snapshot_elements(snapshot)
                if not role or el["role"] == role
            ][:12]
        ref = await disambiguate_with_vision(
            step_description,
            intent,
            pool,
            mcp_manager=mcp_manager,
            client=client,
            model=model,
        )

    if not ref:
        return PlaywrightMCPToolCall(
            action="error",
            value=(
                f"ambiguous or missing target name={intent.target_name!r} "
                f"role={intent.target_role!r} matches={len(candidates)}"
            ),
            thinking=intent.thinking,
            timeout_ms=timeout_ms,
        )
    return intent_to_tool_call(intent, ref=ref, timeout_ms=timeout_ms)


async def preview_step_resolution(
    step_description: str,
    snapshot: str,
    *,
    expected_result: str | None = None,
    client=None,
    model: str | None = None,
) -> StepPreview:
    """Observe-only: Intent + match candidates without executing (Stagehand-style)."""
    try:
        intent = await generate_step_intent(
            step_description,
            snapshot,
            expected_result=expected_result,
            client=client,
            model=model,
        )
    except Exception as exc:
        return StepPreview(
            action="error",
            thinking=f"intent failed: {exc}",
            match_count=0,
        )
    candidates = match_intent_candidates(snapshot, intent)
    ref = candidates[0]["ref"] if len(candidates) == 1 else None
    return StepPreview(
        action=intent.action,
        target_role=intent.target_role,
        target_name=intent.target_name,
        value=intent.value,
        ref=ref,
        match_count=len(candidates),
        candidates=[{"ref": c["ref"], "role": c["role"], "name": c["name"]} for c in candidates[:12]],
        thinking=intent.thinking,
        needs_vision=len(candidates) != 1 and (intent.action or "") not in (
            "wait", "goto", "error", "assert_text", "press_key",
        ),
    )
