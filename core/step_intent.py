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
# Prefer real selectable rows over tooltip/text mirrors of the same label.
_PREFERRED_CLICK_ROLES = (
    "option", "menuitem", "treeitem", "listitem", "row", "link", "button", "tab", "checkbox",
)
_WEAK_CLICK_ROLES = frozenset({
    "tooltip", "text", "generic", "label", "cell", "paragraph", "heading",
})

# Close / dismiss controls — Ant Design often exposes aria-label "Close" not「关闭」.
_CLOSE_NAME_ALIASES = frozenset({
    "关闭", "close", "关闭按钮", "关闭窗口", "关闭对话框", "关闭弹窗", "关闭提示",
    "×", "x", "✕", "✖", "✗", "dismiss", "关闭通知",
})
# Notification CTAs that open 消息列表 — never treat as dialog close.
_CLOSE_FORBIDDEN_NAMES = frozenset({
    "去查看", "立即查看", "查看消息", "查看详情", "消息列表", "查看全部",
    "了解更多", "打开消息", "进入消息", "查看",
})
_CLOSE_STEP_RE = re.compile(
    r"(?:"
    r"关闭.*(?:对话框|弹窗|提示框|消息弹|通知)"
    r"|(?:对话框|弹窗|提示框|消息弹|通知).*(?:关闭|关掉|【X】|关闭标志)"
    r"|点击【(?:关闭|X|×)】"
    r"|【X】形状的关闭"
    r"|关掉.*(?:对话框|弹窗|提示)"
    r")",
    re.IGNORECASE,
)


def _norm_close_label(name: str | None) -> str:
    return (name or "").strip().lower()


def is_close_control_name(name: str | None) -> bool:
    """True when AX accessible name is a dismiss/close control."""
    raw = (name or "").strip()
    if not raw:
        return False
    if raw in ("×", "✕", "✖", "✗", "X", "x"):
        return True
    return _norm_close_label(raw) in {_norm_close_label(a) for a in _CLOSE_NAME_ALIASES}


def is_forbidden_close_click_name(name: str | None) -> bool:
    """True for notification CTAs that must never be used to dismiss a dialog."""
    raw = (name or "").strip()
    if not raw:
        return False
    if raw in _CLOSE_FORBIDDEN_NAMES:
        return True
    low = raw.lower()
    return any(f in raw or f.lower() in low for f in _CLOSE_FORBIDDEN_NAMES)


def is_close_dialog_step(
    step_description: str | None = None,
    structured_step: dict | None = None,
    intent: StepIntent | None = None,
) -> bool:
    """True when the step intent is dismiss/close dialog(s), not open messages."""
    if intent is not None and is_close_control_name(intent.target_name):
        return True
    if isinstance(structured_step, dict):
        if is_close_control_name(structured_step.get("target_name")):
            return True
        icon = (structured_step.get("icon_hint") or "")
        note = (structured_step.get("note") or "")
        dis = (structured_step.get("disambiguation") or "")
        blob = f"{icon} {note} {dis}"
        if "关闭" in blob or "【X】" in blob or "对话框" in blob:
            return True
    s = (step_description or "").strip()
    return bool(s and _CLOSE_STEP_RE.search(s))


def close_control_candidates(snapshot: str, *, limit: int = 12) -> list[dict[str, str]]:
    """AX nodes that look like dialog/notification close controls.

    Prefers named Close/关闭/×; never returns「去查看」/消息列表 entries.
    """
    named: list[dict[str, str]] = []
    glyph: list[dict[str, str]] = []
    for el in parse_snapshot_elements(snapshot):
        role = (el.get("role") or "").lower()
        if role not in _ICON_CANDIDATE_ROLES:
            continue
        name = (el.get("name") or "").strip()
        if is_forbidden_close_click_name(name):
            continue
        if is_close_control_name(name):
            named.append(el)
        elif not name or len(name) <= 1 or name.lower() in _GENERIC_ICON_NAMES:
            # Unnamed / single-glyph buttons often are the X on Ant notifications
            if role in ("button", "img", "image", "link"):
                glyph.append(el)
    out = named + glyph
    return out[:limit]


def prefer_actionable_candidates(
    candidates: list[dict[str, str]],
    *,
    allow_weak: bool = True,
) -> list[dict[str, str]]:
    """Rank click candidates: option/button first, then other strong roles.

    Weak roles (tooltip/text) are last resort only when ``allow_weak`` is True.
    Search-result clicks must pass ``allow_weak=False`` — tooltip click-through
    often selects a sibling (e.g. 山西省院) and wastes minutes in browser-use.
    """
    if not candidates:
        return []
    preferred = [
        c for c in candidates
        if (c.get("role") or "").lower() in _PREFERRED_CLICK_ROLES
    ]
    if preferred:
        return preferred
    strong = [
        c for c in candidates
        if (c.get("role") or "").lower() not in _WEAK_CLICK_ROLES
    ]
    if strong:
        return strong
    if not allow_weak:
        logger.info(
            "intent match: refusing weak-only roles %s for name=%r "
            "(allow_weak=False)",
            sorted({(c.get("role") or "") for c in candidates}),
            (candidates[0].get("name") if candidates else None),
        )
        return []
    logger.info(
        "intent match: using weak-only roles %s for name=%r (last resort)",
        sorted({(c.get("role") or "") for c in candidates}),
        (candidates[0].get("name") if candidates else None),
    )
    return candidates


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
- Close/dismiss steps (关闭对话框 / Close / X): ONLY pick Close/关闭/×/X or unnamed X glyph.
  NEVER pick「去查看」「查看」「消息列表」or notification body entries.
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
    # Disambiguation (搜索结果中的) stays in thinking so the matcher prefers
    # filter-panel roles; do NOT force target_role=option — many UIs only
    # expose the filtered row as tooltip/text, which previously clicked fine.
    return StepIntent(
        action=action,
        target_role=role,
        target_name=name,
        value=value if value is not None else None,
        confidence=0.95,
        ambiguous=False,
        thinking="; ".join(thinking_parts),
    )


# Interactive actions that can use a recorded CSS/Playwright selector as MCP target.
_SELECTOR_ACTIONS = frozenset({
    "click", "fill", "select", "hover", "check", "uncheck", "icon_click",
})
# Accessibility refs look like e15 / f4e11 — not CSS.
_AX_REF_RE = re.compile(r"^[a-zA-Z]*\d*e\d+$", re.IGNORECASE)
# Bare tags fail Playwright strict mode on real forms (e.g. locator('input') → 8 hits).
_BARE_TAG_RE = re.compile(
    r"^(html|body|div|span|p|a|button|input|select|textarea|label|img|"
    r"ul|ol|li|table|tr|td|th|form|section|nav|header|footer|main|"
    r"i|b|em|strong|svg|path|h[1-6])$",
    re.IGNORECASE,
)
# Element UI / Ant ephemeral popover ids change every session.
_EPHEMERAL_ID_RE = re.compile(
    r"#el-(?:popover|popper|tooltip|message|notification|dialog|drawer|select|dropdown)-\d+"
    r"|#el-[a-z]+-\d+",
    re.IGNORECASE,
)


def is_ax_ref(selector: str | None) -> bool:
    """True when selector is an ephemeral accessibility ref (e15), not CSS."""
    if not selector:
        return False
    return bool(_AX_REF_RE.fullmatch(str(selector).strip()))


def is_usable_solidified_selector(selector: str | None) -> bool:
    """False for over-generic / session-ephemeral selectors (strict-mode bombs)."""
    if not selector:
        return False
    s = str(selector).strip()
    if not s or is_ax_ref(s):
        return False
    if _BARE_TAG_RE.fullmatch(s):
        return False
    if _EPHEMERAL_ID_RE.search(s):
        return False
    # Need a specificity signal beyond a lone tag word
    if any(ch in s for ch in ("#", ".", "[", ">", "=", '"', "'")):
        return True
    low = s.lower()
    if "has-text" in low or "text=" in low or "nth-" in low or ">>" in s:
        return True
    if " " in s:
        return True
    return False


def solidified_selector(structured: dict | None) -> str | None:
    """Return recorded CSS/Playwright selector from StructuredStep, if usable."""
    if not isinstance(structured, dict):
        return None
    raw = structured.get("selector")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if not is_usable_solidified_selector(s):
        logger.info("skip weak/ephemeral solidified selector=%r", s)
        return None
    return s


def _css_attr_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _placeholder_lookup_names(name: str) -> list[str]:
    """Expand short field labels to full placeholder phrases for CSS fallbacks.

    Recording/convert may store target_name as「单位」while the DOM only has
    ``placeholder="请选择单位"``. Prefer the full phrase; never emit CSS from a
    truncated field word alone (no ``placeholder=\"单位\"`` / ``*=\"单位\"``).
    """
    n = (name or "").strip()
    if not n:
        return []
    out: list[str] = []

    def _add(x: str) -> None:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)

    if n.startswith("请选择") or n.startswith("请输入"):
        _add(n)
    else:
        _add(f"请选择{n}")
        _add(f"请输入{n}")
    return out


def derived_selector_candidates(structured: dict | None) -> list[str]:
    """CSS fallbacks from target_name when recorded selector is weak/missing.

    Element UI / Ant Design unit pickers often use placeholder「请选择单位」while
    AX role=combobox matching finds nothing — placeholder CSS still works.
    Prefer exact ``placeholder=\"…\"`` with the full visible phrase; avoid
    deriving ``input[placeholder*=\"单位\"]`` from a truncated field label.
    """
    if not isinstance(structured, dict):
        return []
    name = (structured.get("target_name") or "").strip()
    if len(name) < 1:
        return []
    action = (structured.get("action") or "click").strip().lower()
    role = (structured.get("target_role") or "").strip().lower()
    if action not in _SELECTOR_ACTIONS and action != "select":
        return []
    out: list[str] = []
    names = _placeholder_lookup_names(name)
    # Placeholder-driven inputs / select triggers — exact first, then *=
    if role in ("combobox", "textbox", "searchbox", "listbox", "") or action in (
        "click", "fill", "select",
    ):
        for cand in names:
            safe = _css_attr_escape(cand)
            out.append(f'input[placeholder="{safe}"]')
            out.append(f'[placeholder="{safe}"]')
        for cand in names:
            # Only *= with full placeholder phrases (请选择… / 请输入…),
            # never with a lone short field word like「单位」.
            if not (cand.startswith("请选择") or cand.startswith("请输入")):
                continue
            safe = _css_attr_escape(cand)
            out.append(f'input[placeholder*="{safe}"]')
            out.append(f'[placeholder*="{safe}"]')
    # Labeled buttons / links — keep original target_name (not 请选择…)
    if action in ("click", "icon_click") and role in ("button", "link", ""):
        safe = _css_attr_escape(name)
        out.append(f'button:has-text("{safe}")')
        out.append(f'a:has-text("{safe}")')
    if action in ("click", "select") and role in ("option", "menuitem", "treeitem", ""):
        safe = _css_attr_escape(name)
        out.append(f':text("{safe}")')
        out.append(f'div:has-text("{safe}")')
    seen: set[str] = set()
    result: list[str] = []
    for sel in out:
        if sel not in seen and is_usable_solidified_selector(sel):
            seen.add(sel)
            result.append(sel)
    return result


def _action_value_for_selector_tc(structured: dict | None) -> tuple[str, str | None]:
    action = "click"
    value = None
    if isinstance(structured, dict):
        action = (structured.get("action") or "click").strip().lower()
        if structured.get("value") is not None:
            value = str(structured.get("value"))
    if action == "icon_click":
        action = "click"
    elif action in ("check", "uncheck"):
        action = "click"
    return action, value


def tool_call_from_solidified_selector(
    structured: dict | None,
    *,
    timeout_ms: int = 30000,
):
    """Build a PlaywrightMCPToolCall from recorded selector (skip AX bind).

    Playwright MCP accepts ``target`` as either an AX ref or a unique element
    selector — recording solidification stores the latter. Weak selectors like
    bare ``input`` are rejected (see ``is_usable_solidified_selector``).
    """
    from core.llm_wrapper import PlaywrightMCPToolCall

    sel = solidified_selector(structured)
    if not sel:
        return None
    action, value = _action_value_for_selector_tc(structured)
    if action not in _SELECTOR_ACTIONS:
        return None
    return PlaywrightMCPToolCall(
        action=action,
        selector=sel,
        value=value,
        timeout_ms=timeout_ms,
        thinking=f"solidified selector={sel!r}",
        next_goal="verify this step only",
    )


def selector_tool_call_candidates(
    structured: dict | None,
    *,
    timeout_ms: int = 30000,
) -> list:
    """Ordered MCP tool-calls: usable recorded selector, then derived placeholders."""
    from core.llm_wrapper import PlaywrightMCPToolCall

    action, value = _action_value_for_selector_tc(structured)
    if action not in _SELECTOR_ACTIONS:
        return []
    out: list = []
    seen: set[str] = set()
    primary = tool_call_from_solidified_selector(structured, timeout_ms=timeout_ms)
    if primary is not None and primary.selector:
        seen.add(primary.selector)
        out.append(primary)
    for sel in derived_selector_candidates(structured):
        if sel in seen:
            continue
        seen.add(sel)
        out.append(
            PlaywrightMCPToolCall(
                action=action,
                selector=sel,
                value=value,
                timeout_ms=timeout_ms,
                thinking=f"derived selector from target_name={sel!r}",
                next_goal="verify this step only",
            )
        )
    return out


def goto_is_redundant(
    current_url: str | None,
    goto_url: str | None,
    base_url: str | None = None,
) -> bool:
    """True when goto would leave a deeper BASE path for a shallower same-origin URL.

    Example: BASE/current is ``http://host/xtmh`` and step goto is ``http://host/``
    — navigating away breaks subsequent login steps.
    """
    from urllib.parse import urlparse

    def _parts(u: str | None) -> tuple[str, str] | None:
        raw = (u or "").strip()
        if not raw:
            return None
        try:
            p = urlparse(raw)
        except Exception:
            return None
        if not p.scheme or not p.netloc:
            return None
        origin = f"{p.scheme}://{p.netloc}".lower()
        path = (p.path or "/").rstrip("/") or "/"
        return origin, path

    goto = _parts(goto_url)
    if not goto:
        return False
    g_origin, g_path = goto

    for cand in (current_url, base_url):
        cur = _parts(cand)
        if not cur:
            continue
        c_origin, c_path = cur
        if c_origin != g_origin:
            continue
        if c_path == g_path:
            return True
        # Already under a non-root app path; goto to origin root is harmful.
        if c_path != "/" and g_path == "/":
            return True
        # Current is strictly deeper than goto on the same prefix.
        g_prefix = g_path.rstrip("/")
        if g_prefix and c_path.startswith(g_prefix + "/"):
            return True
    return False


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


def _names_compatible(el_name: str, target_name: str) -> bool:
    """True when AX name is an acceptable match for the intended label.

    Exact match always wins. Containment is allowed for placeholders
    (单位 ⊂ 请选择单位) but NOT for peer org labels of similar length
    (京州市院 ≉ 山西省院) even if they share a one-char suffix like 院.
    """
    a = (el_name or "").strip()
    b = (target_name or "").strip()
    if not b:
        return True
    if not a:
        return False
    if a == b:
        return True
    # Peer labels: same length (±1), share only a short suffix → never fuzzy-match
    if (
        abs(len(a) - len(b)) <= 1
        and len(a) >= 4
        and len(b) >= 4
        and a != b
        and a[-1] == b[-1]
        and a not in b
        and b not in a
    ):
        return False
    # Prefer containment only when both sides are meaningful (≥2 chars)
    if len(a) >= 2 and len(b) >= 2 and (b in a or a in b):
        return True
    return False


def _exact_name_only(el_name: str, target_name: str) -> bool:
    return (el_name or "").strip() == (target_name or "").strip()


def _element_by_ref(snapshot: str, ref: str) -> dict[str, str] | None:
    if not ref:
        return None
    for el in parse_snapshot_elements(snapshot):
        if el.get("ref") == ref:
            return el
    return None


def ref_matches_intent(snapshot: str, ref: str | None, intent: StepIntent) -> bool:
    """Reject binds where the chosen AX node name clearly contradicts target_name.

    Prevents false-success clicks on a stale ref (e.g. reusing the previous step's
    button after a help-center dropdown has already closed).
    """
    if not ref:
        return False
    name = (intent.target_name or "").strip()
    el = _element_by_ref(snapshot, ref)
    if not el:
        return False
    el_name = (el.get("name") or "").strip()
    # Never accept notification CTAs as a close/dismiss bind
    if is_close_control_name(name) and is_forbidden_close_click_name(el_name):
        return False
    if not name:
        # Icon / unnamed: allow empty/short/generic names only
        if not el_name or len(el_name) <= 2 or el_name.lower() in _GENERIC_ICON_NAMES:
            return True
        # Named content button is almost never the toolbar icon
        return False
    if is_close_control_name(name):
        if is_close_control_name(el_name):
            return True
        # X glyph / empty aria often used for Ant Design notification close
        if not el_name or len(el_name) <= 1 or el_name.lower() in _GENERIC_ICON_NAMES:
            return True
        return False
    return _names_compatible(el_name, name)


def match_intent_candidates(
    snapshot: str,
    intent: StepIntent,
    *,
    frame_hint: str | None = None,
) -> list[dict[str, str]]:
    """Return AX elements matching intent role/name (exact name preferred).

    ``frame_hint``: when set, keep only elements whose nearest preceding
    ``iframe`` / ``frame`` snapshot line mentions the hint.

    When ``target_name`` is empty, returns [] — callers that need glyph/icon
    controls must use ``icon_click_candidates`` (do NOT dump every button).
    """
    role = (intent.target_role or "").strip().lower() or None
    name = (intent.target_name or "").strip()
    if not name:
        return []

    elements = parse_snapshot_elements(snapshot)
    if frame_hint:
        fh = frame_hint.strip()
        if fh:
            lines = (snapshot or "").splitlines()
            ref_frame: dict[str, str] = {}
            last_frame = ""
            last_frame_indent = -1
            for line in lines:
                stripped = line.lstrip(" ")
                indent = len(line) - len(stripped)
                low = stripped.lower()
                if low.startswith("- iframe") or re.match(r"- frame\b", low):
                    last_frame = line
                    last_frame_indent = indent
                elif last_frame_indent >= 0 and indent <= last_frame_indent and stripped.startswith("-"):
                    # Left the iframe subtree (sibling or ancestor level)
                    last_frame = ""
                    last_frame_indent = -1
                m = re.search(r"\[ref=([a-zA-Z]*\d*e\d+)\]", line)
                if m:
                    ref_frame[m.group(1)] = last_frame
            preferred = [
                el for el in elements
                if fh in (ref_frame.get(el["ref"]) or "")
                or fh.lower() in (ref_frame.get(el["ref"]) or "").lower()
            ]
            if preferred:
                elements = preferred

    close_target = is_close_control_name(name)
    exact: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    for el in elements:
        if role and el["role"] != role:
            continue
        el_name = el.get("name") or ""
        if is_forbidden_close_click_name(el_name) and (
            close_target or is_close_control_name(name)
        ):
            continue
        if el_name == name or (close_target and is_close_control_name(el_name)):
            exact.append(el)
        elif _names_compatible(el_name, name) and el_name != name:
            if close_target and is_forbidden_close_click_name(el_name):
                continue
            partial.append(el)
    # Exact weak labels (e.g. "单位") must not hide the real control
    # (textbox/combobox "请选择单位") that only matches partially.
    if exact:
        exact_strong = [
            e for e in exact
            if (e.get("role") or "").lower() not in _WEAK_CLICK_ROLES
        ]
        if exact_strong:
            # Keep weak siblings (tooltip) so search-result callers can
            # drop treeitem and still click the filter-panel mirror.
            return exact
        partial_strong = [
            e for e in partial
            if (e.get("role") or "").lower() not in _WEAK_CLICK_ROLES
        ]
        if partial_strong:
            interactive = [
                e for e in partial_strong
                if (e.get("role") or "").lower() in (
                    *_PREFERRED_CLICK_ROLES,
                    "textbox", "combobox", "searchbox",
                )
            ]
            chosen = interactive or partial_strong
            logger.info(
                "intent match: prefer strong partial over weak exact name=%r "
                "exact_roles=%s → partial_roles=%s",
                name,
                sorted({(e.get("role") or "") for e in exact}),
                sorted({(e.get("role") or "") for e in chosen}),
            )
            return chosen
        return exact
    return partial


def match_intent_candidates_with_role_fallback(
    snapshot: str,
    intent: StepIntent,
    *,
    frame_hint: str | None = None,
) -> list[dict[str, str]]:
    """Match by name; widen roles; prefer option/menuitem over tooltip mirrors."""
    name = (intent.target_name or "").strip()
    thinking = intent.thinking or ""

    # Search-result / dropdown option clicks: try selectable roles first
    search_like = bool(
        re.search(r"搜索结果|筛选结果|下拉|列表|选项", thinking)
        or (intent.target_role or "").lower() in (
            "option", "menuitem", "treeitem", "listitem",
        )
    )
    if search_like and name:
        # 「搜索结果」: after filter, exact treeitem/option is OK; never tooltip.
        search_panel = bool(re.search(r"搜索结果|筛选结果", thinking))
        if search_panel:
            role_order: tuple[str | None, ...] = (
                "option", "menuitem", "listitem", "row", "treeitem", None,
            )
        else:
            role_order = (
                "option", "menuitem", "treeitem", "listitem", "row", None,
            )
        for alt in role_order:
            widened = intent.model_copy(update={"target_role": alt})
            raw = match_intent_candidates(snapshot, widened, frame_hint=frame_hint)
            if search_panel:
                # Exact name only — never fuzzy 京州市院 ↔ 山西省院
                raw = [
                    c for c in raw
                    if (c.get("name") or "").strip() == name
                ]
                # Tooltip click-through mis-selects siblings; filtered tree rows
                # often appear as text/generic — those are OK. Drop tooltip only.
                raw = [
                    c for c in raw
                    if (c.get("role") or "").lower() != "tooltip"
                ]
            found = prefer_actionable_candidates(
                raw,
                # search: allow exact text/generic after tooltip stripped
                allow_weak=True if search_panel else True,
            )
            if search_panel and found:
                # Still never return a pure-tooltip list (already stripped)
                found = [
                    c for c in found
                    if (c.get("role") or "").lower() != "tooltip"
                ]
            if found:
                logger.info(
                    "intent match search/option path: role=%r name=%r count=%s",
                    alt, name, len(found),
                )
                return found
        if search_panel:
            # No non-tooltip exact hit — fail fast (hybrid will pick up)
            logger.info(
                "intent match: search panel has no non-tooltip hit for %r; "
                "fail fast to hybrid",
                name,
            )
            return []

    # Close/dismiss: match Close/× aliases first; never return 去查看
    if is_close_dialog_step(intent=intent) or is_close_control_name(name):
        close_hits = prefer_actionable_candidates(
            match_intent_candidates(snapshot, intent, frame_hint=frame_hint)
        )
        close_hits = [
            c for c in close_hits
            if not is_forbidden_close_click_name(c.get("name"))
        ]
        if close_hits:
            return close_hits
        # Widen role — Close is often button/img without Chinese「关闭」
        for alt in ("button", "img", "image", "link", None):
            widened = intent.model_copy(update={"target_role": alt})
            raw = match_intent_candidates(snapshot, widened, frame_hint=frame_hint)
            raw = [
                c for c in raw
                if is_close_control_name(c.get("name"))
                and not is_forbidden_close_click_name(c.get("name"))
            ]
            if raw:
                return prefer_actionable_candidates(raw)
        pool = close_control_candidates(snapshot)
        if pool:
            logger.info(
                "intent match close path: using close_control_candidates count=%s",
                len(pool),
            )
            return pool
        return []

    found = prefer_actionable_candidates(
        match_intent_candidates(snapshot, intent, frame_hint=frame_hint)
    )
    if found or not name:
        return found
    role = (intent.target_role or "").strip().lower()
    # Dropdown / help-center entries are often menuitem or link, not button
    alt_roles: list[str | None]
    if role in ("", "button"):
        alt_roles = ["option", "menuitem", "treeitem", "listitem", "link", "tab", "combobox", "textbox", None]
    elif role in ("combobox", "listbox"):
        # Ant Design Select trigger is often textbox with placeholder 请选择…
        alt_roles = ["textbox", "searchbox", "button", "link", None]
    elif role:
        alt_roles = ["option", "menuitem", "button", "link", "textbox", "combobox", None]
    else:
        return found
    for alt in alt_roles:
        widened = intent.model_copy(update={"target_role": alt})
        found = prefer_actionable_candidates(
            match_intent_candidates(snapshot, widened, frame_hint=frame_hint)
        )
        if found:
            logger.info(
                "intent match role fallback: %r → %r name=%r count=%s",
                role or None, alt, intent.target_name, len(found),
            )
            return found
    return []


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
    prefer_selector: bool = True,
):
    """Resolve NL step → PlaywrightMCPToolCall via Intent + deterministic bind (+ vision).

    When ``structured_step`` is complete, skip Intent LLM and bind from the structure.
    When ``prefer_selector`` and ``structured_step.selector`` is set, return that
    CSS/Playwright selector as the MCP target (recording solidification) instead of
    AX name/role bind. Callers that already tried the selector and failed should
    pass ``prefer_selector=False`` for semantic fallback.
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

    # Prefer recorded / derived CSS before AX semantic bind.
    # Execute-and-retry callers should use selector_tool_call_candidates instead.
    if prefer_selector:
        for sel_tc in selector_tool_call_candidates(
            structured_step, timeout_ms=timeout_ms,
        ):
            logger.info(
                "prefer solidified selector action=%s selector=%r",
                sel_tc.action, sel_tc.selector,
            )
            return sel_tc

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
        close_step = is_close_dialog_step(
            step_description, structured_step, intent,
        )
        if close_step and isinstance(structured_step, dict) and structured_step.get("icon_hint"):
            # Prefer X/Close glyph path when Instant kept icon_hint
            icon_step = True
        frame_hint = None
        if isinstance(structured_step, dict):
            frame_hint = structured_step.get("frame_hint")

        # Scroll + re-snapshot when target label missing from truncated AX tree
        if mcp_manager is not None and hasattr(mcp_manager, "refresh_snapshot_for_hints"):
            hints = [intent.target_name] if intent.target_name else []
            if close_step:
                hints = list(dict.fromkeys(
                    (hints or []) + ["关闭", "Close", "×", "X"]
                ))
            if not hints:
                hints = extract_label_hints(step_description)
            try:
                snapshot = await mcp_manager.refresh_snapshot_for_hints(hints, current=snapshot)
            except Exception as exc:
                logger.debug("snapshot hint refresh failed: %s", exc)

        candidates = match_intent_candidates_with_role_fallback(
            snapshot, intent, frame_hint=frame_hint,
        )
        if close_step:
            candidates = [
                c for c in candidates
                if not is_forbidden_close_click_name(c.get("name"))
            ]
        ref: str | None = None
        if len(candidates) == 1 and not icon_step:
            ref = candidates[0]["ref"]
        elif icon_step or close_step or (use_vision_fallback and mcp_manager is not None and len(candidates) != 1):
            if close_step:
                pool = close_control_candidates(snapshot, limit=16)
                if not pool:
                    pool = list(candidates)[:16]
            elif icon_step:
                # Prefer unnamed/short glyph controls FIRST — never truncate a
                # dump of every button (that reused the previous step's ref).
                pool = icon_click_candidates(snapshot, limit=16)
            else:
                pool = list(candidates)[:16]
            if not pool and intent.target_name:
                pool = match_intent_candidates_with_role_fallback(
                    snapshot,
                    StepIntent(
                        action=intent.action,
                        target_role=None,
                        target_name=intent.target_name,
                        value=intent.value,
                    ),
                )[:12]
            if close_step:
                pool = [
                    c for c in pool
                    if not is_forbidden_close_click_name(c.get("name"))
                ]
            if not pool and not icon_step and not close_step:
                # Do not fall back to arbitrary page nodes when a label is required
                pool = []
            if pool and mcp_manager is not None and use_vision_fallback:
                ref = await disambiguate_with_vision(
                    step_description,
                    intent,
                    pool,
                    mcp_manager=mcp_manager,
                    client=client,
                    model=model,
                )
            elif len(pool) == 1:
                ref = pool[0]["ref"]
            elif close_step and pool:
                # Prefer first named Close/关闭/× without waiting on vision miss
                named = [c for c in pool if is_close_control_name(c.get("name"))]
                if len(named) == 1:
                    ref = named[0]["ref"]
                elif named:
                    ref = named[0]["ref"]
        elif len(candidates) == 0 and not use_vision_fallback:
            return intent_to_tool_call(
                intent.model_copy(update={"action": "error", "thinking": "structured bind: no AX match"}),
                ref=None,
                timeout_ms=timeout_ms,
            )
        if ref and not ref_matches_intent(snapshot, ref, intent):
            logger.warning(
                "reject bind: ref=%s incompatible with name=%r (icon_step=%s)",
                ref, intent.target_name, icon_step,
            )
            ref = None
        if not ref:
            from core.llm_wrapper import PlaywrightMCPToolCall as _TC
            return _TC(
                action="error",
                value=(
                    f"structured bind failed name={intent.target_name!r} "
                    f"role={intent.target_role!r} matches={len(candidates)}"
                    + (" (icon: no compatible glyph control)" if icon_step else "")
                    + (
                        " — target label not in snapshot (dropdown may be closed)"
                        if intent.target_name and not candidates and not icon_step
                        else ""
                    )
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
    close_step = is_close_dialog_step(step_description, intent=intent)
    if close_step:
        icon_step = True
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
        if close_step:
            hints = list(dict.fromkeys((hints or []) + ["关闭", "Close", "×", "X"]))
        try:
            snapshot = await mcp_manager.refresh_snapshot_for_hints(hints, current=snapshot)
        except Exception as exc:
            logger.debug("snapshot hint refresh failed: %s", exc)

    candidates = match_intent_candidates_with_role_fallback(snapshot, intent)
    if close_step:
        candidates = [
            c for c in candidates
            if not is_forbidden_close_click_name(c.get("name"))
        ]
    ref = None
    if len(candidates) == 1 and not icon_step:
        ref = candidates[0]["ref"]
    elif use_vision_fallback and (len(candidates) != 1 or icon_step or close_step):
        logger.info(
            "intent match count=%s for name=%r icon_step=%s close_step=%s — vision fallback",
            len(candidates), intent.target_name, icon_step, close_step,
        )
        if close_step:
            pool = close_control_candidates(snapshot, limit=16) or list(candidates)[:16]
        elif icon_step:
            pool = icon_click_candidates(snapshot, limit=16)
        else:
            pool = list(candidates)[:16]
        if not pool and intent.target_name:
            broadened = StepIntent(
                action=intent.action,
                target_role=None,
                target_name=intent.target_name,
                value=intent.value,
                thinking=intent.thinking,
            )
            pool = match_intent_candidates_with_role_fallback(snapshot, broadened)[:12]
        if close_step:
            pool = [
                c for c in pool
                if not is_forbidden_close_click_name(c.get("name"))
            ]
        if pool:
            ref = await disambiguate_with_vision(
                step_description,
                intent,
                pool,
                mcp_manager=mcp_manager,
                client=client,
                model=model,
            )
        if not ref and close_step and pool:
            named = [c for c in pool if is_close_control_name(c.get("name"))]
            if named:
                ref = named[0]["ref"]

    if ref and not ref_matches_intent(snapshot, ref, intent):
        logger.warning(
            "reject bind: ref=%s incompatible with name=%r (icon_step=%s)",
            ref, intent.target_name, icon_step,
        )
        ref = None

    if not ref:
        return PlaywrightMCPToolCall(
            action="error",
            value=(
                f"ambiguous or missing target name={intent.target_name!r} "
                f"role={intent.target_role!r} matches={len(candidates)}"
                + (" (icon-only; vision found no ref)" if icon_step else "")
                + (
                    " — target label not in snapshot (dropdown may be closed)"
                    if intent.target_name and not candidates and not icon_step
                    else ""
                )
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
    candidates = match_intent_candidates_with_role_fallback(snapshot, intent)
    if icon_step and len(candidates) != 1:
        candidates = icon_click_candidates(snapshot) or candidates
    ref = candidates[0]["ref"] if len(candidates) == 1 else None
    if ref and not ref_matches_intent(snapshot, ref, intent):
        ref = None
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
