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
5. Closing dialogs/notifications: click visible Close/关闭 or header X; may need multiple turns.
6. Use action "evaluate" only when refs fail for Element UI / Ant overlays — value must be a short JS function body returning a boolean success, e.g. click exact tree label.
7. Use action "wait" with numeric seconds or text to wait for.
8. Use action "goto" only when navigation is still needed (value=url).
9. When the GOAL and checklist expectations are satisfied, status="done".
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
    """Assemble whole-case NL goal; steps are soft checklist."""
    lines: list[str] = [
        f"CASE: {case_name or 'UI case'}",
    ]
    if description:
        lines.append(f"DESCRIPTION:\n{(description or '').strip()}")
    lines.append("CHECKLIST (soft — complete the intent, not necessarily 1 MCP call per line):")
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
        "Complete the checklist intents on the live page. "
        "You may use multiple turns for one checklist item."
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


async def decide_next_goal_action(
    *,
    client: AsyncOpenAI,
    model: str,
    goal_text: str,
    snapshot: str,
    journal_tail: list[dict[str, Any]],
    temperature: float = 0.15,
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
    user = (
        f"{goal_text}\n\n"
        f"RECENT ACTIONS (oldest→newest):\n"
        f"{json.dumps(recent, ensure_ascii=False, indent=2)}\n"
        f"{last_fail}\n"
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

    - Steps before the fail cursor (or at/before max successful progress) → passed
    - Earliest unrecovered failed checklist item → failed (+ screenshot)
    - If no explicit fail (e.g. max_turns): fail the first step after max progress
    - Remaining later steps → skipped
    """
    orders = [
        int(s.get("step_order") or s.get("step_number") or i + 1)
        for i, s in enumerate(steps or [])
    ]
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
        if e.get("success"):
            covered.add(idx)
            if fail_order == idx:
                fail_order = None
                fail_error = None
                fail_shot = None
        else:
            if fail_order is None or idx < fail_order:
                fail_order = idx
                fail_error = e.get("error") or error
                fail_shot = e.get("screenshot_path")

    max_progress = max(covered) if covered else 0

    if success:
        fail_order = None
    elif fail_order is None:
        nxt = next((o for o in orders if o > max_progress), None)
        fail_order = nxt if nxt is not None else (orders[-1] if orders else None)
        fail_error = error or "nl_goal failed"

    if not success and fail_shot is None:
        for e in reversed(journal or []):
            if e.get("screenshot_path"):
                fail_shot = e["screenshot_path"]
                break

    results: list[dict[str, Any]] = []
    for o in orders:
        s = by_order.get(o) or {}
        desc = s.get("description") or s.get("original_description") or ""
        if success or (fail_order is not None and o < fail_order) or (
            fail_order is None and o <= max_progress
        ):
            st, ok, err, shot = "passed", True, None, None
        elif fail_order is not None and o == fail_order:
            st, ok, err, shot = (
                "failed",
                False,
                fail_error or error or "nl_goal failed",
                fail_shot,
            )
        else:
            st, ok, err, shot = (
                "skipped",
                False,
                f"Skipped due to step {fail_order} failure" if fail_order else (error or "skipped"),
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
