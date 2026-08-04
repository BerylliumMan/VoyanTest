# core/goal_agent_loop.py
"""Whole-case NL goal agent loop (Cursor-session style).

One continuous observe→act session over MCP snapshot/refs until the case goal
is done, failed, or hit turn/stagnation limits. Steps are a soft checklist only.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 40
STAGNATION_LIMIT = 5

_JSON_RE = re.compile(r"\{[\s\S]*\}")

GOAL_SYSTEM_PROMPT = """You are a browser automation agent (like Cursor IDE browser tools).
You receive a WHOLE-CASE natural-language GOAL plus an accessibility SNAPSHOT of the current page.
Decide ONE next browser action, or mark the goal done/failed.

Rules:
1. Prefer snapshot refs (e.g. e12) as selector for click/fill/hover/select — they are current-page refs.
2. For fill/type use action "fill" with selector=ref and value=text. Prefer slow typing for filter/search boxes when the UI filters on input events (set value and mention in thinking).
3. One primary action per turn. After dropdowns open, take another turn to type/filter then click the exact option.
4. Prefer exact text match for tree/list options (avoid clicking a longer similar label).
5. Closing dialogs/notifications/messages: MUST actually click Close/关闭/header X (or dismiss) for EACH visible message/dialog. Loop until none remain. Never assume they are gone. Never status="done" while any close/dismiss checklist item is still uncovered. action "evaluate" / "wait" NEVER counts as completing a close-message checklist item — only successful click does.
6. Use action "evaluate" only when refs fail for Element UI / Ant overlays — value must be a short JS function body returning a boolean success, e.g. click exact tree label. Do NOT use evaluate to "verify" or mark close/open checklist items done.
7. Use action "wait" with numeric seconds or text to wait for.
8. Use action "goto" only when navigation is still needed (value=url).
9. status="done" ONLY when EVERY checklist item has been advanced by a successful action (see UNCOVERED CHECKLIST in the user message). If any checklist item is still uncovered — especially close-message / close-dialog steps — you MUST status="continue" and perform the action. Premature done is forbidden.
10. If stuck after retries, status="fail" with reason.
11. structured hints in the goal are optional tips, NOT mandatory one-shot bindings.
12. Output ONLY one JSON object matching the schema — no markdown fences.

Schema:
{
  "status": "continue" | "done" | "fail",
  "thinking": "brief reasoning",
  "action": "click|fill|goto|wait|select|press_key|hover|evaluate|screenshot",
  "selector": "ref or css or empty",
  "value": "text/url/js/key or empty",
  "stable_hint": "durable locator hint e.g. placeholder=请选择单位 or role=button name=登录",
  "checklist_index": 3,
  "checklist_note": "which checklist item this advances"
}

checklist_index = 1-based checklist step number this action advances (required when status=continue).
"""

REPAIR_SYSTEM_PROMPT = """You repair a Playwright Python script that failed on dry-run.
Keep the same overall flow. Fix selectors/strict-mode issues (use .first, visible filters,
exact text, press_sequentially for filter inputs). Output ONLY the full Python script,
no markdown fences. The script MUST define async def test_case_{case_id}(page).
"""


class GoalAction(BaseModel):
    status: str = Field(default="continue")  # continue | done | fail
    thinking: str = ""
    action: str = ""
    selector: Optional[str] = None
    value: Optional[str] = None
    stable_hint: Optional[str] = None
    checklist_index: Optional[int] = None
    checklist_note: Optional[str] = None
    reason: Optional[str] = None


_CHECKLIST_IDX_RE = re.compile(
    r"(?:checklist\s*item|item|step|步骤|第)\s*#?\s*(\d+)",
    re.I,
)


def parse_checklist_index(
    note: str | None = None,
    *,
    explicit: int | None = None,
) -> int | None:
    if explicit is not None:
        try:
            n = int(explicit)
            return n if n > 0 else None
        except (TypeError, ValueError):
            pass
    text = (note or "").strip()
    if not text:
        return None
    m = _CHECKLIST_IDX_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def build_goal_text(
    *,
    case_name: str,
    description: str | None,
    steps: list[dict[str, Any]],
) -> str:
    """Assemble whole-case NL goal; steps are soft checklist.

    Leads with a Cursor-style natural goal (intent), then a soft checklist.
    Rigid per-step wording is guidance — complete the intent on the live page.
    """
    step_descs = [
        (s.get("description") or s.get("original_description") or "").strip()
        for s in (steps or [])
        if (s.get("description") or s.get("original_description") or "").strip()
    ]
    # Soft NL goal similar to a Cursor user prompt
    nl_bits = "；".join(step_descs[:12]) if step_descs else ""
    lines: list[str] = [
        f"CASE: {case_name or 'UI case'}",
        "NATURAL GOAL (primary — execute like Cursor browser agent):",
        (
            (description or "").strip()
            or nl_bits
            or "Complete the UI workflow on the current site."
        ),
    ]
    if description and nl_bits:
        lines.append(f"STEP HINTS (merged): {nl_bits}")
    lines.append(
        "CHECKLIST (soft — complete the intent; you may merge/skip redundant lines "
        "like re-opening a URL already navigated, or intermediate username typos):"
    )
    for s in steps or []:
        order = s.get("step_order") or s.get("step_number") or "?"
        desc = (s.get("description") or s.get("original_description") or "").strip()
        exp = (s.get("expected_result") or s.get("parsed_result") or "").strip()
        hint = ""
        st = s.get("structured_step") if isinstance(s.get("structured_step"), dict) else {}
        if st:
            bits = []
            if st.get("action"):
                bits.append(f"action={st.get('action')}")
            if st.get("selector"):
                bits.append(f"selector_hint={st.get('selector')}")
            if st.get("value") is not None and str(st.get("value")):
                bits.append(f"value_hint={st.get('value')}")
            if st.get("target_name"):
                bits.append(f"name={st.get('target_name')}")
            if bits:
                hint = " [" + "; ".join(bits) + "]"
        line = f"{order}. {desc}{hint}"
        if exp:
            line += f"\n   EXPECT: {exp}"
        lines.append(line)
    lines.append(
        "STRATEGY (from successful Cursor sessions on Element UI):\n"
        "- Prefer snapshot refs for click/fill; use slow type for filter inputs.\n"
        "- For tree select: open → filter → click exact .el-tree-node__label "
        "(evaluate JS click is OK when refs fail).\n"
        "- After login home: close ALL prompts via evaluate that clicks "
        ".el-dialog__wrapper footer 关闭 / .el-dialog__headerbtn and "
        ".el-notification__closeBtn in a loop — do NOT click 消息铃铛/去查看.\n"
        "- Do not status=done until overlays are gone. "
        "evaluate that only 'verifies' without .click() does not count."
    )
    return "\n".join(lines)

def _parse_goal_action(raw: str) -> GoalAction:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise ValueError(f"LLM did not return JSON: {text[:200]}")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM JSON root must be object")
    status = str(data.get("status") or "continue").strip().lower()
    if status not in ("continue", "done", "fail"):
        # allow implicit done via action
        if str(data.get("action") or "").lower() == "done":
            status = "done"
        else:
            status = "continue"
    return GoalAction(
        status=status,
        thinking=str(data.get("thinking") or data.get("reason") or ""),
        action=str(data.get("action") or "").strip(),
        selector=(str(data["selector"]) if data.get("selector") is not None else None),
        value=(str(data["value"]) if data.get("value") is not None else None),
        stable_hint=(str(data["stable_hint"]) if data.get("stable_hint") else None),
        checklist_index=parse_checklist_index(
            data.get("checklist_note"),
            explicit=data.get("checklist_index"),
        ),
        checklist_note=(str(data["checklist_note"]) if data.get("checklist_note") else None),
        reason=(str(data["reason"]) if data.get("reason") else None),
    )


_CLOSE_MESSAGES_STEP_RE = re.compile(
    r"关闭.*(?:消息|通知|弹窗|对话框|按钮)|消息的关闭|关闭按钮|所有出现的消息",
    re.I,
)
_OPEN_NAV_STEP_RE = re.compile(r"^打开【|^打开\s|跳转到|导航到|访问https?://|打开页面", re.I)
_CLICK_STEP_RE = re.compile(r"^(?:点击|单击|click\b)", re.I)
_FILL_STEP_RE = re.compile(r"输入|填写|填入|\bfill\b|\btype\b", re.I)
_SELECT_STEP_RE = re.compile(r"选择|选中|\bselect\b", re.I)


def is_close_messages_checklist_step(description: str | None) -> bool:
    """True for checklist items that dismiss page messages/notifications."""
    return bool(description and _CLOSE_MESSAGES_STEP_RE.search(description))


def is_click_checklist_step(description: str | None) -> bool:
    """True for checklist items whose primary intent is a click."""
    if not description or is_close_messages_checklist_step(description):
        return False
    return bool(_CLICK_STEP_RE.search(description.strip()))


def is_fill_checklist_step(description: str | None) -> bool:
    """True for checklist items whose primary intent is typing/filling."""
    if not description:
        return False
    desc = description.strip()
    if is_click_checklist_step(desc) or is_close_messages_checklist_step(desc):
        return False
    return bool(_FILL_STEP_RE.search(desc))


def is_select_checklist_step(description: str | None) -> bool:
    """True for checklist items whose primary intent is selecting an option."""
    if not description:
        return False
    desc = description.strip()
    if is_click_checklist_step(desc) or is_fill_checklist_step(desc):
        return False
    return bool(_SELECT_STEP_RE.search(desc))


# Proven against Element UI home overlays (from a successful Cursor browser session):
# close visible .el-dialog__wrapper (footer 关闭 / header X) + .el-notification.
CLOSE_ALL_PAGE_PROMPTS_JS = r"""() => {
  const clicked = [];
  for (const w of Array.from(document.querySelectorAll('.el-dialog__wrapper'))) {
    if (getComputedStyle(w).display === 'none') continue;
    const footerClose = Array.from(w.querySelectorAll('button')).find((b) =>
      /关\s*闭/.test(b.innerText || '')
    );
    const headerClose = w.querySelector('.el-dialog__headerbtn');
    const btn = footerClose || headerClose;
    if (btn) {
      btn.click();
      clicked.push('dialog:' + (footerClose ? 'footer' : 'header'));
    }
  }
  for (const n of Array.from(document.querySelectorAll('.el-notification'))) {
    if (getComputedStyle(n).display === 'none') continue;
    const btn = n.querySelector(
      '.el-notification__closeBtn, .el-icon-close, [class*="close"]'
    );
    if (btn) {
      btn.click();
      clicked.push('notification');
    } else {
      try {
        n.remove();
        clicked.push('notification-remove');
      } catch (e) {}
    }
  }
  for (const w of Array.from(document.querySelectorAll('.el-message-box__wrapper'))) {
    if (getComputedStyle(w).display === 'none') continue;
    const btn = Array.from(w.querySelectorAll('button')).find((b) =>
      /关\s*闭|取消|确定/.test(b.innerText || '')
    );
    if (btn) {
      btn.click();
      clicked.push('msgbox');
    }
  }
  const remainingDialogs = Array.from(
    document.querySelectorAll('.el-dialog__wrapper')
  ).filter((w) => getComputedStyle(w).display !== 'none').length;
  const remainingNotes = Array.from(
    document.querySelectorAll('.el-notification')
  ).filter((n) => getComputedStyle(n).display !== 'none').length;
  return {
    ok: true,
    clicked,
    remainingDialogs,
    remainingNotes,
  };
}"""


def _evaluate_is_real_close_action(entry: dict[str, Any]) -> bool:
    """True when evaluate actually dismisses overlays (Cursor-style), not a no-op verify."""
    blob = " ".join(
        str(x or "")
        for x in (
            entry.get("value"),
            entry.get("checklist_note"),
            entry.get("thinking"),
            entry.get("result_snippet"),
            entry.get("stable_hint"),
        )
    )
    if "CLOSE_ALL_PAGE_PROMPTS" in blob or "close_all_page_prompts" in blob.lower():
        return True
    if re.search(
        r"el-dialog__headerbtn|el-notification__closeBtn|关\\s\*闭|关\s*闭",
        blob,
    ):
        return True
    if re.search(r"\.click\s*\(|clicked\s*[:=]", blob, re.I):
        if re.search(r"dialog|notification|关闭|closeBtn|headerbtn", blob, re.I):
            return True
    return False


def _evaluate_result_looks_falsy(entry: dict[str, Any]) -> bool:
    """True when evaluate result_snippet clearly indicates a falsy JS return."""
    snippet = str(entry.get("result_snippet") or "").strip().lower()
    if not snippet:
        return False
    # Ignore generic agent chatter like "Executing: evaluate"
    if "executing:" in snippet and "false" not in snippet and "null" not in snippet:
        return False
    if snippet in ("false", "null", "undefined", "0", "none"):
        return True
    if re.search(r"(?:^|[\s:=])false(?:[\s,}\]]|$)", snippet) and "true" not in snippet:
        return True
    return False


def _evaluate_is_real_click_action(entry: dict[str, Any]) -> bool:
    """True when evaluate actually performs a DOM click (not a no-op / verify)."""
    if _evaluate_result_looks_falsy(entry):
        return False
    blob = " ".join(
        str(x or "")
        for x in (
            entry.get("value"),
            entry.get("checklist_note"),
            entry.get("thinking"),
            entry.get("stable_hint"),
        )
    )
    return bool(re.search(r"\.click\s*\(", blob))


def journal_entry_covers_checklist(
    entry: dict[str, Any],
    *,
    step_description: str | None = None,
) -> bool:
    """Whether a journal entry may count as covering a checklist step.

    Close-message steps need a real dismiss: click, or evaluate that actually
    clicks Element UI dialog/notification close controls (Cursor pattern).
    Bare evaluate "verify no dialogs" must NOT fake-cover.

    Click / fill / select steps require action-type alignment: a successful
    fill/search must NOT mark a click checklist item as covered.
    """
    if not entry.get("success"):
        return False
    action = (entry.get("action") or "").strip().lower()
    desc = (step_description or "").strip()

    if is_close_messages_checklist_step(desc):
        if action in ("click", "browser_click"):
            return True
        if action in ("evaluate", "browser_evaluate", "js", "eval"):
            return _evaluate_is_real_close_action(entry)
        return False

    if desc and _OPEN_NAV_STEP_RE.search(desc):
        return action in (
            "goto",
            "navigate",
            "browser_navigate",
            "click",
            "browser_click",
        )

    if action in ("wait", "screenshot", "press_key", "browser_press_key"):
        return False

    if is_click_checklist_step(desc):
        if action in ("click", "browser_click"):
            return True
        if action in ("evaluate", "browser_evaluate", "js", "eval"):
            return _evaluate_is_real_click_action(entry)
        return False

    if is_fill_checklist_step(desc):
        return action in (
            "fill",
            "type",
            "browser_type",
            "select",
            "browser_select_option",
        )

    if is_select_checklist_step(desc):
        if action in (
            "click",
            "browser_click",
            "select",
            "browser_select_option",
        ):
            return True
        if action in ("evaluate", "browser_evaluate", "js", "eval"):
            return _evaluate_is_real_click_action(entry)
        return False

    return True


def close_messages_step_orders(steps: list[dict[str, Any]] | None) -> list[int]:
    """Checklist orders whose description is a close-all-messages intent."""
    out: list[int] = []
    for i, s in enumerate(steps or []):
        o = int(s.get("step_order") or s.get("step_number") or i + 1)
        desc = str(s.get("description") or s.get("original_description") or "")
        if is_close_messages_checklist_step(desc):
            out.append(o)
    return out


def covered_checklist_indices(
    journal: list[dict[str, Any]] | None,
    steps: list[dict[str, Any]] | None = None,
) -> set[int]:
    """Checklist indices successfully advanced in the journal (action-aware)."""
    by_order: dict[int, str] = {}
    for i, s in enumerate(steps or []):
        o = int(s.get("step_order") or s.get("step_number") or i + 1)
        by_order[o] = str(s.get("description") or s.get("original_description") or "")

    covered: set[int] = set()
    for e in journal or []:
        idx = parse_checklist_index(
            e.get("checklist_note"),
            explicit=e.get("checklist_index"),
        )
        if idx is None:
            continue
        desc = by_order.get(idx) or e.get("checklist_note")
        if journal_entry_covers_checklist(e, step_description=desc):
            covered.add(idx)
    return covered


def checklist_orders(steps: list[dict[str, Any]] | None) -> list[int]:
    return [
        int(s.get("step_order") or s.get("step_number") or i + 1)
        for i, s in enumerate(steps or [])
    ]


def uncovered_checklist_orders(
    steps: list[dict[str, Any]] | None,
    journal: list[dict[str, Any]] | None,
) -> list[int]:
    """1-based checklist orders not yet successfully covered by journal."""
    covered = covered_checklist_indices(journal, steps)
    return [o for o in checklist_orders(steps) if o not in covered]


def seed_open_steps_after_navigation(
    steps: list[dict[str, Any]] | None,
    base_url: str | None,
) -> list[dict[str, Any]]:
    """Mark open/navigate checklist items covered when BASE URL already matches."""
    if not base_url or not steps:
        return []
    base = str(base_url).lower()
    seeded: list[dict[str, Any]] = []
    for i, s in enumerate(steps):
        o = int(s.get("step_order") or s.get("step_number") or i + 1)
        desc = str(s.get("description") or s.get("original_description") or "")
        if not _OPEN_NAV_STEP_RE.search(desc):
            continue
        m = re.search(r"【([^】]+)】", desc)
        token = (m.group(1) if m else "").strip().lower()
        if not token or token not in base:
            continue
        seeded.append(
            {
                "turn": 0,
                "status": "continue",
                "thinking": "base_url navigation",
                "action": "goto",
                "selector": None,
                "value": base_url,
                "stable_hint": None,
                "checklist_index": o,
                "checklist_note": f"BASE URL already opened 【{token}】",
                "success": True,
                "error": None,
                "duration_ms": 0,
                "result_snippet": None,
                "screenshot_on_fail": False,
                "screenshot_path": None,
            }
        )
    return seeded


async def decide_next_goal_action(
    *,
    client: AsyncOpenAI,
    model: str,
    goal_text: str,
    snapshot: str,
    journal_tail: list[dict[str, Any]],
    temperature: float = 0.15,
    steps: list[dict[str, Any]] | None = None,
) -> GoalAction:
    """Ask LLM for the next GoalAction given goal + snapshot + recent journal."""
    recent = journal_tail[-8:] if journal_tail else []
    last_fail = ""
    if recent and not recent[-1].get("success"):
        last_fail = (
            f"\nLAST ACTION FAILED — recover with a different approach "
            f"(other ref, evaluate for exact tree label, or close overlay first):\n"
            f"{json.dumps(recent[-1], ensure_ascii=False)}\n"
        )
    uncovered = uncovered_checklist_orders(steps, journal_tail) if steps is not None else []
    uncovered_block = ""
    if steps is not None:
        if uncovered:
            uncovered_block = (
                f"\nUNCOVERED CHECKLIST (must execute before status=done): {uncovered}\n"
                "Do NOT status=done while this list is non-empty. "
                "Close/dismiss message or dialog items require real click actions.\n"
            )
        else:
            uncovered_block = (
                "\nUNCOVERED CHECKLIST: [] — all checklist items covered; "
                "status=done is allowed if the page goal is satisfied.\n"
            )
    user = (
        f"{goal_text}\n\n"
        f"RECENT ACTIONS (oldest→newest):\n"
        f"{json.dumps(recent, ensure_ascii=False, indent=2)}\n"
        f"{last_fail}"
        f"{uncovered_block}\n"
        f"CURRENT ACCESSIBILITY SNAPSHOT:\n"
        f"{(snapshot or '(empty)')[:120000]}\n\n"
        "Decide the next single action JSON now."
    )
    messages = [
        {"role": "system", "content": GOAL_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    last_err: Optional[str] = None
    for attempt in range(3):
        if attempt and last_err:
            messages.append(
                {
                    "role": "user",
                    "content": f"Previous output invalid: {last_err}. Output ONLY valid JSON.",
                }
            )
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=2048,
            )
            content = (resp.choices[0].message.content or "").strip()
            return _parse_goal_action(content)
        except Exception as exc:
            last_err = str(exc)
            logger.warning("decide_next_goal_action attempt %s failed: %s", attempt + 1, exc)
    raise ValueError(f"decide_next_goal_action failed: {last_err}")


def journal_entry(
    *,
    turn: int,
    decision: GoalAction,
    success: bool,
    error: str | None = None,
    duration_ms: float = 0,
    result_snippet: str | None = None,
    screenshot_on_fail: bool = False,
    screenshot_path: str | None = None,
) -> dict[str, Any]:
    idx = parse_checklist_index(
        decision.checklist_note,
        explicit=decision.checklist_index,
    )
    return {
        "turn": turn,
        "status": decision.status,
        "thinking": decision.thinking,
        "action": decision.action,
        "selector": decision.selector,
        "value": decision.value,
        "stable_hint": decision.stable_hint,
        "checklist_index": idx,
        "checklist_note": decision.checklist_note,
        "success": success,
        "error": error,
        "duration_ms": duration_ms,
        "result_snippet": (result_snippet or "")[:500] or None,
        "screenshot_on_fail": bool(screenshot_on_fail),
        "screenshot_path": screenshot_path,
    }


def detect_stagnation(journal: list[dict[str, Any]], limit: int = STAGNATION_LIMIT) -> bool:
    """True when the last N actions are identical failures or identical no-ops."""
    if len(journal) < limit:
        return False
    tail = journal[-limit:]
    if all(not e.get("success") for e in tail):
        keys = [
            (e.get("action"), e.get("selector"), e.get("value"))
            for e in tail
        ]
        if len(set(keys)) <= 2:
            return True
    # same successful action repeated (stuck loop)
    keys_ok = [
        (e.get("action"), e.get("selector"), e.get("value"))
        for e in tail
        if e.get("success")
    ]
    if len(keys_ok) >= limit and len(set(keys_ok)) == 1:
        return True
    return False


def tool_call_from_decision(decision: GoalAction) -> dict[str, Any]:
    """Map GoalAction → STEP_EXECUTE tool_call dict."""
    action = (decision.action or "").strip().lower()
    if action in ("browser_click",):
        action = "click"
    elif action in ("browser_type", "type"):
        action = "fill"
    elif action in ("browser_navigate", "navigate"):
        action = "goto"
    elif action in ("browser_evaluate", "js", "eval"):
        action = "evaluate"
    elif action in ("browser_press_key",):
        action = "press_key"
    elif action in ("browser_hover",):
        action = "hover"
    elif action in ("browser_select_option",):
        action = "select"
    return {
        "action": action,
        "selector": decision.selector or "",
        "value": decision.value,
        "selector_type": "css",
        "thinking": decision.thinking,
        "timeout_ms": 30000,
    }


def steps_results_from_goal(
    steps: list[dict[str, Any]],
    *,
    success: bool,
    journal: list[dict[str, Any]],
    error: str | None = None,
    backend: str = "nl_goal",
) -> list[dict[str, Any]]:
    """Map journal checklist progress onto per-step report rows.

    When success=True (goal marked done):
      - Only checklist indices successfully covered in journal → passed
      - Never-covered checklist steps → failed
        (error: "nl_goal marked done but step N was not executed")
      Never treat DONE as "all steps passed".

    When success=False:
      - Only journal-covered checklist steps → passed (never fake-pass
        earlier uncovered steps just because fail_order is later)
      - Earliest unrecovered failed checklist item → failed (+ screenshot)
      - Uncovered steps before the fail cursor → failed ("was not executed")
      - If no explicit fail (e.g. max_turns): fail the first step after max progress
      - Remaining later steps → skipped
    """
    orders = checklist_orders(steps)
    by_order = {
        int(s.get("step_order") or s.get("step_number") or i + 1): s
        for i, s in enumerate(steps or [])
    }
    if not orders:
        return []

    covered: set[int] = set()
    fail_order: int | None = None
    fail_error: str | None = None
    fail_shot: str | None = None

    for e in journal or []:
        idx = parse_checklist_index(
            e.get("checklist_note"),
            explicit=e.get("checklist_index"),
        )
        if idx is None:
            continue
        step_rec = by_order.get(idx) or {}
        desc = str(
            step_rec.get("description")
            or step_rec.get("original_description")
            or ""
        )
        if journal_entry_covers_checklist(e, step_description=desc):
            covered.add(idx)
            if fail_order == idx:
                fail_order = None
                fail_error = None
                fail_shot = None
        elif e.get("success"):
            # successful but not cover-eligible (e.g. evaluate on close step)
            continue
        else:
            if fail_order is None or idx < fail_order:
                fail_order = idx
                fail_error = e.get("error") or error
                fail_shot = e.get("screenshot_path")

    max_progress = max(covered) if covered else 0

    # DONE / success=True: journal-truthful — never fake-pass uncovered steps
    if success:
        results: list[dict[str, Any]] = []
        for o in orders:
            s = by_order.get(o) or {}
            desc = s.get("description") or s.get("original_description") or ""
            if o in covered:
                st, ok, err, shot = "passed", True, None, None
            else:
                st, ok, err, shot = (
                    "failed",
                    False,
                    f"nl_goal marked done but step {o} was not executed",
                    None,
                )
            results.append(
                {
                    "step_number": o,
                    "original_description": desc,
                    "success": ok,
                    "status": st,
                    "thinking": "nl_goal",
                    "action": "nl_goal",
                    "next_goal": "",
                    "error": err,
                    "screenshot_path": shot,
                    "duration_ms": 0,
                    "backend": backend,
                }
            )
        if results and journal:
            results[0]["action_journal"] = journal
            results[0]["duration_ms"] = sum(
                float(e.get("duration_ms") or 0) for e in journal
            )
        return results

    if fail_order is None:
        nxt = next((o for o in orders if o > max_progress), None)
        fail_order = nxt if nxt is not None else (orders[-1] if orders else None)
        fail_error = error or "nl_goal failed"

    if fail_shot is None:
        for e in reversed(journal or []):
            if e.get("screenshot_path"):
                fail_shot = e["screenshot_path"]
                break

    results = []
    for o in orders:
        s = by_order.get(o) or {}
        desc = s.get("description") or s.get("original_description") or ""
        if fail_order is not None and o > fail_order:
            st, ok, err, shot = (
                "skipped",
                False,
                f"Skipped due to step {fail_order} failure",
                None,
            )
        elif o in covered:
            # Journal-covered only — never assume earlier steps passed
            st, ok, err, shot = "passed", True, None, None
        elif fail_order is not None and o == fail_order:
            st, ok, err, shot = (
                "failed",
                False,
                fail_error or error or "nl_goal failed",
                fail_shot,
            )
        else:
            # Uncovered and at/before fail cursor → not executed
            st, ok, err, shot = (
                "failed",
                False,
                f"nl_goal step {o} was not executed",
                None,
            )
        results.append(
            {
                "step_number": o,
                "original_description": desc,
                "success": ok,
                "status": st,
                "thinking": "nl_goal",
                "action": "nl_goal",
                "next_goal": "",
                "error": err,
                "screenshot_path": shot,
                "duration_ms": 0,
                "backend": backend,
            }
        )

    if results and journal:
        results[0]["action_journal"] = journal
        results[0]["duration_ms"] = sum(float(e.get("duration_ms") or 0) for e in journal)
    return results
