"""Cursor-style helpers for OTA Observe → Think → Act.

Aligns OTA with IDE browser workflows:
1. Prefer snapshot refs for click/fill
2. On hard pages / failures, attach a viewport screenshot to Think
3. When ref click fails, resolve element center and retry via click_xy
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
from typing import Any

from core.locator_candidates import LocatorCandidate, is_snapshot_ref, snapshot_has_visible_overlay

logger = logging.getLogger(__name__)

# Shared Cursor-style system prompt for server AgentRunner (and Bridge override tip).
OTA_CURSOR_SYSTEM_PROMPT = """You are a goal-driven browser automation agent (Cursor IDE browser style).
Accomplish the user GOAL by observing the page and taking ONE action per turn.

LOOP: Observe → Think → Act → Repeat until done OR error.

INPUT:
1. GOAL — what to accomplish
2. HISTORY — prior actions and results
3. CURRENT PAGE — accessibility snapshot with refs (e.g. e12)
4. Optional SCREENSHOT — viewport image when the page is hard (overlay/canvas/failure)

ACTION PRIORITY (follow this order):
1. Prefer snapshot refs from CANDIDATE ELEMENTS for click/fill/hover/select.
2. If snapshot is truncated, overlay/canvas/iframe, or a screenshot is attached and refs fail —
   use click_xy / drag_xy with viewport coordinates from the screenshot (value="x,y").
3. Use short evaluate ONLY for Element UI / Ant overlays where refs exist but clicks miss —
   value must be a short JS function body returning boolean success. Do NOT use evaluate to "verify".
4. Never invent CSS selectors; never guess coordinates without a screenshot.

ACTIONS (maps to Playwright MCP):
- "click": Click by snapshot ref. selector=ref, value=null.
- "double_click" / "right_click": selector=ref.
- "fill": Type into input. selector=ref, value=text.
- "select": Dropdown. selector=ref, value=option text/value.
- "hover" / "check": selector=ref.
- "goto": Navigate. selector=null, value=URL.
- "wait": Wait for text or seconds. selector=null, value=text or number.
- "press_key": Key or chord. selector=null, value=Enter|Escape|Control+A…
- "scroll": Page or element scroll. selector=null|ref, value=px|"up"|"down".
- "drag": Drag element to element. selector=startRef, value=endRef.
- "click_xy": Click viewport coords (canvas / no reliable ref). selector=null, value="x,y".
- "move_mouse" / "drag_xy": Coordinates; drag_xy value="x1,y1,x2,y2".
- "dialog": Native alert/confirm/prompt. value=accept|dismiss|accept:text.
- "upload": File input. selector=ref, value=absolute path(s).
- "drop": Drop file(s) onto target. selector=ref, value=absolute path(s).
- "tabs": list|new|select:N|close[:N].
- "navigate_back": Browser back.
- "evaluate": Short JS only when refs fail (see priority 3).
- "screenshot" / "snapshot": Capture / refresh observation.
- "done": Goal achieved. value=summary.
- "error": Cannot proceed. value=reason.

RULES:
- Output ONLY one JSON object — no markdown fences.
- Always include "thinking" with brief reasoning and the chosen label/ref.
- status done ONLY when the GOAL is fully satisfied; otherwise continue.
- After dropdowns open, take another turn to pick the option.
- Closing overlays: click Close/关闭/X for EACH visible dialog; never assume gone.

OUTPUT SCHEMA:
{
  "action": "click",
  "selector": "e15",
  "value": null,
  "timeout_ms": 30000,
  "thinking": "…",
  "next_goal": "…"
}
"""


def should_capture_screenshot(
    snapshot: str | None,
    *,
    consecutive_failures: int = 0,
    force: bool = False,
    pending_hint: str | None = None,
) -> bool:
    """Decide whether this Observe turn should include a viewport screenshot."""
    if force:
        return True
    if consecutive_failures > 0:
        return True
    if pending_hint:
        return True
    snap = snapshot or ""
    if snapshot_has_visible_overlay(snap):
        return True
    # Canvas / truncated snapshot markers often need vision
    if re.search(r"\bcanvas\b|truncated|snapshot truncated", snap, re.I):
        return True
    return False


async def capture_screenshot_b64(mcp_manager) -> str | None:
    """Take a PNG screenshot via PlaywrightMCPManager and return base64."""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="vt_ota_")
    os.close(fd)
    try:
        saved = await mcp_manager.take_screenshot(path)
        if not saved or not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception as exc:
        logger.debug("OTA screenshot capture failed: %s", exc, exc_info=True)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


_BBOX_BY_ROLE_NAME_JS = """() => {
  const role = %s;
  const name = %s;
  const roleSel = {
    button: 'button, [role="button"], input[type="button"], input[type="submit"]',
    link: 'a, [role="link"]',
    textbox: 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]), textarea, [role="textbox"]',
    combobox: 'select, [role="combobox"], [aria-haspopup="listbox"]',
    checkbox: 'input[type="checkbox"], [role="checkbox"]',
    menuitem: '[role="menuitem"]',
    option: '[role="option"], option',
    tab: '[role="tab"]',
    treeitem: '[role="treeitem"]',
  };
  const sel = roleSel[role] || '*';
  const nodes = Array.from(document.querySelectorAll(sel));
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const target = norm(name);
  let best = null;
  for (const el of nodes) {
    const label = norm(
      el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.innerText
      || el.textContent
      || el.value
      || ''
    );
    if (!target) continue;
    if (label === target || label.includes(target) || target.includes(label)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        best = {
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          w: Math.round(r.width),
          h: Math.round(r.height),
        };
        if (label === target) break;
      }
    }
  }
  return best;
}"""


def bbox_evaluate_js(*, role: str | None, name: str | None) -> str | None:
    """JS function body/string for evaluate to return {x,y,w,h} or null."""
    if not (name or "").strip():
        return None
    return _BBOX_BY_ROLE_NAME_JS % (
        json.dumps((role or "button").strip().lower()),
        json.dumps((name or "").strip()),
    )


def parse_bbox_center_from_text(text: str | None) -> tuple[int, int] | None:
    """Parse ``{x,y,...}`` JSON from evaluate tool result text."""
    raw = (text or "").strip()
    if not raw or raw in ("null", "undefined"):
        return None
    # MCP may wrap JSON in prose — find first object
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    x, y = data.get("x"), data.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return int(x), int(y)
    return None


def bridge_evaluate_act(role: str | None, name: str | None) -> dict[str, Any] | None:
    """Build AgentBridge send_act payload for bbox evaluate."""
    fn = bbox_evaluate_js(role=role, name=name)
    if not fn:
        return None
    return {
        "name": "evaluate",
        "tool": "evaluate",
        "args": {
            "selector": None,
            "element_desc": None,
            "value": fn,
            "timeout_ms": 15000,
        },
    }


def bridge_click_xy_act(x: int, y: int, *, thinking: str = "") -> dict[str, Any]:
    """Build AgentBridge send_act payload for click_xy."""
    return {
        "name": "click_xy",
        "tool": "click_xy",
        "args": {
            "selector": None,
            "element_desc": None,
            "value": f"{x},{y}",
            "timeout_ms": 30000,
        },
        "thinking": thinking,
    }


async def resolve_bbox_center(
    mcp_manager,
    *,
    role: str | None,
    name: str | None,
) -> tuple[int, int] | None:
    """Find element center by role+accessible name via page evaluate."""
    fn = bbox_evaluate_js(role=role, name=name)
    if not fn:
        return None
    try:
        result = await mcp_manager.call_tool("browser_evaluate", {"function": fn})
        if not result.get("success"):
            return None
        return parse_bbox_center_from_text(result.get("text"))
    except Exception as exc:
        logger.debug("bbox resolve failed: %s", exc, exc_info=True)
    return None


def find_candidate(
    candidates: tuple[LocatorCandidate, ...] | list | None,
    ref: str | None,
) -> LocatorCandidate | None:
    if not ref or not candidates:
        return None
    for c in candidates:
        if getattr(c, "ref", None) == ref:
            return c
    return None


async def maybe_rewrite_click_xy_from_ref(
    action: dict[str, Any],
    *,
    mcp_manager,
    candidates: tuple[LocatorCandidate, ...] | list | None,
) -> dict[str, Any]:
    """If model chose click_xy with a snapshot ref as selector, resolve to x,y."""
    act = (action.get("action") or "").strip().lower()
    if act not in ("click_xy", "browser_mouse_click_xy"):
        return action
    sel = action.get("selector")
    if not is_snapshot_ref(sel):
        return action
    # Already has coords in value?
    nums = [int(n) for n in re.findall(r"-?\d+", action.get("value") or "")]
    if len(nums) >= 2:
        action["selector"] = None
        return action
    cand = find_candidate(candidates, sel)
    center = await resolve_bbox_center(
        mcp_manager,
        role=getattr(cand, "role", None) if cand else None,
        name=getattr(cand, "name", None) if cand else None,
    )
    if center is None:
        return action
    x, y = center
    out = dict(action)
    out["selector"] = None
    out["value"] = f"{x},{y}"
    out["thinking"] = (
        (out.get("thinking") or "")
        + f" [resolved click_xy from ref {sel} → {x},{y}]"
    ).strip()
    return out


async def click_xy_fallback_after_ref_fail(
    failed_action: dict[str, Any],
    *,
    mcp_manager,
    candidates: tuple[LocatorCandidate, ...] | list | None,
) -> dict[str, Any] | None:
    """Build a click_xy retry when a ref-based click/fill opener failed."""
    act = (failed_action.get("action") or "").strip().lower()
    if act not in ("click", "double_click", "right_click", "check", "hover"):
        return None
    sel = failed_action.get("selector")
    if not is_snapshot_ref(sel):
        return None
    cand = find_candidate(candidates, sel)
    center = await resolve_bbox_center(
        mcp_manager,
        role=getattr(cand, "role", None) if cand else "button",
        name=getattr(cand, "name", None) if cand else failed_action.get("element_desc"),
    )
    if center is None:
        return None
    x, y = center
    return {
        "action": "click_xy",
        "selector": None,
        "value": f"{x},{y}",
        "timeout_ms": failed_action.get("timeout_ms") or 30000,
        "thinking": f"ref click failed; fallback click_xy at {x},{y} for {sel}",
        "element_desc": failed_action.get("element_desc")
        or (getattr(cand, "name", None) if cand else None),
    }
