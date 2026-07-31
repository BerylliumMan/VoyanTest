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

# 纯图标 / 视觉描述步骤（旁无文字时靠截图消歧）
_ICON_ONLY_STEP_RE = re.compile(
    r"(?:"
    r"图标|icon\b|无文字按钮|图形按钮|形状图标"
    r"|(?:书本|齿轮|铃铛|放大镜|垃圾桶|铅笔|头像|汉堡|三点|省略号).{0,8}(?:图标|形状|按钮)"
    r"|(?:图标|形状).{0,8}(?:书本|齿轮|铃铛|放大镜|设置|帮助|手册)"
    r")",
    re.IGNORECASE,
)
_GENERIC_ICON_NAMES = frozenset({
    "图标", "图片", "图像", "icon", "image", "img", "按钮", "控件",
})
_ICON_CANDIDATE_ROLES = frozenset({
    "button", "link", "img", "image", "menuitem", "tab", "checkbox",
})


def is_icon_only_click_step(text: str | None) -> bool:
    """True when the step targets an icon / glyph rather than plain labeled text."""
    s = (text or "").strip()
    if not s:
        return False
    if not _ICON_ONLY_STEP_RE.search(s):
        return False
    # 「点击【帮助】」with word 图标 elsewhere is still label-first; still allow vision
    labels = extract_label_hints(s)
    if labels and all(lb.lower() not in _GENERIC_ICON_NAMES for lb in labels):
        # Has a real bracket label — may still be icon button with aria-name
        return "图标" in s or "icon" in s.lower() or "形状" in s
    return True


def icon_click_candidates(snapshot: str, *, limit: int = 16) -> list[dict[str, str]]:
    """AX nodes likely to be icon controls (prefer empty/short names)."""
    from core.locator_memory import parse_snapshot_elements

    unnamed: list[dict[str, str]] = []
    named: list[dict[str, str]] = []
    for el in parse_snapshot_elements(snapshot):
        role = (el.get("role") or "").lower()
        if role not in _ICON_CANDIDATE_ROLES:
            continue
        name = (el.get("name") or "").strip()
        # Empty / single-glyph / generic control words → icon-like; keep normal labels
        if (
            not name
            or len(name) <= 1
            or name.lower() in _GENERIC_ICON_NAMES
        ):
            unnamed.append(el)
        else:
            named.append(el)
    # Prefer unnamed icon-like controls; keep a few named buttons as fallback
    out = unnamed + named
    return out[:limit]

INTENT_SYSTEM_PROMPT = """You extract a single browser Intent from a test step. The step text is authoritative.

Output ONLY JSON (no markdown) matching:
{
  "action": "click|fill|select|wait|goto|hover|press_key|scroll|assert_text|click_blank|error",
  "target_role": "button|link|textbox|combobox|option|menuitem|treeitem|listitem|img|checkbox|radio|null",
  "target_name": "exact UI label from 【】/「」 or step text",
  "value": "fill/select/wait/goto value or null",
  "confidence": 0.0-1.0,
  "ambiguous": false,
  "thinking": "quote the label you chose and what you refuse to click"
}

Rules:
- NEVER invent refs (e15) or CSS selectors.
- Prefer 【】/「」 text as target_name. Control-type words (下拉框/输入框/按钮/图标) are NOT target_name.
- 提交≠确定≠保存; 查询≠搜索; 取消≠关闭.
- One primary action only. If the step is wait/assert, action=wait or assert_text and value=text.
- Dropdown open-only steps (展开/打开某某下拉): target_role=combobox (or button), target_name=字段名如「单位」.
- Dropdown option steps (选择/点击【汉东省院】): target_role=option|menuitem|treeitem|listitem,
  target_name=选项文案「汉东省院」。Options may appear under role=tooltip / listbox / menu — still match by option name.
- Icon-only steps (…图标 / 书本形状 / 齿轮形状 / 无文字按钮, often with 位置+用途):
  target_role=button (or img/link), target_name=null unless 【】 contains a real accessible name
  (not 图标/图片). Put shape+position+purpose into thinking. Do NOT set target_name to 图标.
- Click blank/outside area (点击空白处/页面空白/外侧/遮罩) → action=click_blank
  (runtime performs a real viewport mouse click; do not invent a body/html target).
- If you cannot decide safely, action=error and explain in thinking; set ambiguous=true.
"""

VISION_DISAMBIGUATE_PROMPT = """You pick ONE candidate element for the step. Candidates are listed with refs from the accessibility tree.

Output ONLY JSON:
{"ref": "e12", "thinking": "why this candidate"}
or {"ref": null, "thinking": "why none"}

Rules:
- Prefer exact 【】/「」 text match when present.
- Never pick a similar wrong label.
- Icon-only / visual steps: MUST use the screenshot. Match by region (右上角/工具栏…), glyph shape
  (书本=翻开书页/双页；齿轮=齿牙圆盘；铃铛；放大镜…), color, and stated purpose.
  Among several empty-name buttons in the same toolbar, pick the one whose glyph matches the
  described shape — do NOT return null solely because names are empty.
- If the screenshot is missing and names are empty, ref=null.
"""


class StepIntent(BaseModel):
    action: str = Field(..., description="Browser action type")
    target_role: Optional[str] = None
    target_name: Optional[str] = None
    value: Optional[str] = None
    confidence: float = 0.0
    ambiguous: bool = False
    thinking: str = ""


def structured_to_intent(structured: dict | None) -> StepIntent | None:
    """Build StepIntent from UI StructuredStep, skipping Intent LLM when complete."""
    from core.step_normalize import coerce_structured_step, structured_step_is_complete

    step = coerce_structured_step(structured)
    if not step or not structured_step_is_complete(step):
        return None
    action = (step.get("action") or "click").strip().lower()
    # Map UI-only actions onto executor actions
    if action == "assert_visible":
        action = "assert_text"
    elif action == "icon_click":
        action = "click"
    elif action in ("check", "uncheck"):
        action = "click"
    name = step.get("target_name")
    value = step.get("value")
    role = step.get("target_role")
    thinking_parts = ["from structured_step"]
    if step.get("disambiguation"):
        thinking_parts.append(f"disambiguation={step['disambiguation']}")
    if step.get("icon_hint"):
        thinking_parts.append(f"icon_hint={step['icon_hint']}")
        if not name:
            name = None
            role = role or "button"
    return StepIntent(
        action=action,
        target_role=role,
        target_name=name,
        value=value if value is not None else None,
        confidence=0.95,
        ambiguous=False,
        thinking="; ".join(thinking_parts),
    )


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
    *,
    frame_hint: str | None = None,
) -> list[dict[str, str]]:
    """Return AX elements matching intent role/name (exact name preferred).

    ``frame_hint``: when set, keep only elements whose nearest preceding
    ``iframe`` / ``frame`` snapshot line mentions the hint.
    """
    role = (intent.target_role or "").strip().lower() or None
    name = (intent.target_name or "").strip()
    if not name and not role:
        return []

    elements = parse_snapshot_elements(snapshot)
    if frame_hint:
        fh = frame_hint.strip()
        if fh:
            lines = (snapshot or "").splitlines()
            # Map ref → nearest preceding iframe/frame line text
            ref_frame: dict[str, str] = {}
            last_frame = ""
            for line in lines:
                low = line.lower()
                if "iframe" in low or re.search(r"^\s*-\s+frame\b", line, re.I):
                    last_frame = line
                m = re.search(r"\[ref=(e\d+)\]", line)
                if m:
                    ref_frame[m.group(1)] = last_frame
            preferred = [
                el for el in elements
                if fh in (ref_frame.get(el["ref"]) or "")
                or fh.lower() in (ref_frame.get(el["ref"]) or "").lower()
            ]
            if preferred:
                elements = preferred

    exact: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    for el in elements:
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
    if action in ("click_blank", "click_outside", "click_page"):
        return PlaywrightMCPToolCall(
            action="click_blank",
            selector=None,
            value=None,
            timeout_ms=timeout_ms,
            thinking=thinking or "blank/outside area click",
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
    structured_step: dict | None = None,
):
    """Resolve NL step → PlaywrightMCPToolCall via Intent + deterministic bind (+ vision).

    When ``structured_step`` is complete, skip Intent LLM and bind from the structure.
    """
    from core.blank_click import BLANK_CLICK_ACTION, is_blank_area_click_step
    from core.llm_wrapper import PlaywrightMCPToolCall, generate_tool_call

    # Blank / outside click: no a11y ref — skip LLM bind and use viewport mouse click.
    if is_blank_area_click_step(step_description):
        return PlaywrightMCPToolCall(
            action=BLANK_CLICK_ACTION,
            selector=None,
            value=None,
            timeout_ms=timeout_ms,
            thinking="Step asks to click blank/outside area; use viewport mouse click",
            next_goal="verify this step only",
        )

    # StructuredStep fast path (skip Intent LLM)
    prebuilt = structured_to_intent(structured_step)
    if prebuilt is not None:
        intent = prebuilt
        action = (intent.action or "").lower()
        if action in ("wait", "assert_text", "goto", "press_key", "scroll", "screenshot", "click_blank", "click_outside"):
            return intent_to_tool_call(intent, ref=None, timeout_ms=timeout_ms)
        if action == "click_blank" or action == BLANK_CLICK_ACTION:
            return intent_to_tool_call(intent, ref=None, timeout_ms=timeout_ms)

        icon_step = is_icon_only_click_step(step_description) or (
            isinstance(structured_step, dict)
            and (structured_step.get("action") or "").lower() == "icon_click"
        )
        frame_hint = None
        if isinstance(structured_step, dict):
            frame_hint = structured_step.get("frame_hint")

        # Scroll + re-snapshot when target label missing from truncated AX tree
        if mcp_manager is not None and hasattr(mcp_manager, "refresh_snapshot_for_hints"):
            hints = [intent.target_name] if intent.target_name else []
            if not hints:
                hints = extract_label_hints(step_description)
            try:
                snapshot = await mcp_manager.refresh_snapshot_for_hints(hints, current=snapshot)
            except Exception as exc:
                logger.debug("snapshot hint refresh failed: %s", exc)

        candidates = match_intent_candidates(snapshot, intent, frame_hint=frame_hint)
        ref: str | None = None
        if len(candidates) == 1 and not icon_step:
            ref = candidates[0]["ref"]
        elif len(candidates) == 1 and icon_step:
            ref = candidates[0]["ref"]
        elif use_vision_fallback and mcp_manager is not None and (len(candidates) != 1 or icon_step):
            pool = list(candidates)
            if icon_step:
                icon_pool = icon_click_candidates(snapshot)
                seen = {c["ref"] for c in pool}
                for c in icon_pool:
                    if c["ref"] not in seen:
                        pool.append(c)
                        seen.add(c["ref"])
                pool = pool[:16]
            if not pool and intent.target_name:
                pool = match_intent_candidates(
                    snapshot,
                    StepIntent(
                        action=intent.action,
                        target_role=None,
                        target_name=intent.target_name,
                        value=intent.value,
                    ),
                )[:12]
            if not pool:
                pool = icon_click_candidates(snapshot) if icon_step else parse_snapshot_elements(snapshot)[:12]
            ref = await disambiguate_with_vision(
                step_description,
                intent,
                pool,
                mcp_manager=mcp_manager,
                client=client,
                model=model,
            )
        elif len(candidates) == 0 and not use_vision_fallback:
            return intent_to_tool_call(
                intent.model_copy(update={"action": "error", "thinking": "structured bind: no AX match"}),
                ref=None,
                timeout_ms=timeout_ms,
            )
        if not ref:
            from core.llm_wrapper import PlaywrightMCPToolCall as _TC
            return _TC(
                action="error",
                value=(
                    f"structured bind failed name={intent.target_name!r} "
                    f"role={intent.target_role!r} matches={len(candidates)}"
                ),
                thinking=intent.thinking,
                timeout_ms=timeout_ms,
            )
        return intent_to_tool_call(intent, ref=ref, timeout_ms=timeout_ms)

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
    if action in ("wait", "assert_text", "goto", "press_key", "scroll", "screenshot", "click_blank", "click_outside"):
        return intent_to_tool_call(intent, ref=None, timeout_ms=timeout_ms)

    # Icon-only: ignore bogus target_name=图标; LLM often marks these ambiguous —
    # still force vision rather than short-circuiting on action=error.
    icon_step = is_icon_only_click_step(step_description)
    if icon_step and (intent.target_name or "").strip().lower() in _GENERIC_ICON_NAMES:
        intent = intent.model_copy(update={"target_name": None, "target_role": intent.target_role or "button"})
    if (action == "error" or intent.ambiguous) and not (icon_step and use_vision_fallback):
        return intent_to_tool_call(intent, ref=None, timeout_ms=timeout_ms)
    if icon_step and (action == "error" or intent.ambiguous):
        intent = intent.model_copy(
            update={"action": "click", "ambiguous": False, "target_role": intent.target_role or "button"}
        )
        action = "click"

    if mcp_manager is not None and hasattr(mcp_manager, "refresh_snapshot_for_hints"):
        hints = [intent.target_name] if intent.target_name else extract_label_hints(step_description)
        try:
            snapshot = await mcp_manager.refresh_snapshot_for_hints(hints, current=snapshot)
        except Exception as exc:
            logger.debug("snapshot hint refresh failed: %s", exc)

    candidates = match_intent_candidates(snapshot, intent)
    ref = None
    if len(candidates) == 1 and not icon_step:
        ref = candidates[0]["ref"]
    elif len(candidates) == 1 and icon_step:
        # Unique AX name match for an icon button (aria-label) — accept
        ref = candidates[0]["ref"]
    elif use_vision_fallback and (len(candidates) != 1 or icon_step):
        logger.info(
            "intent match count=%s for name=%r icon_step=%s — vision fallback",
            len(candidates), intent.target_name, icon_step,
        )
        pool = candidates
        if icon_step:
            # Always include glyph-like controls for visual pick
            icon_pool = icon_click_candidates(snapshot)
            seen = {c["ref"] for c in pool}
            for c in icon_pool:
                if c["ref"] not in seen:
                    pool.append(c)
                    seen.add(c["ref"])
            pool = pool[:16]
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
            role = (intent.target_role or "").lower()
            if icon_step:
                pool = icon_click_candidates(snapshot)
            else:
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
                + (" (icon-only; vision found no ref)" if icon_step else "")
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
    icon_step = is_icon_only_click_step(step_description)
    if icon_step and (intent.target_name or "").strip().lower() in _GENERIC_ICON_NAMES:
        intent = intent.model_copy(update={"target_name": None, "target_role": intent.target_role or "button"})
    candidates = match_intent_candidates(snapshot, intent)
    if icon_step and len(candidates) != 1:
        candidates = icon_click_candidates(snapshot) or candidates
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
            "wait", "goto", "error", "assert_text", "press_key", "click_blank",
        ),
    )
