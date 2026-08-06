# core/precondition.py
"""Precondition verify-then-execute for nl_goal runs.

Case ``description`` often stores text like ``前置条件：已打开…页面``.
That is an *assumption* in Chinese, not a hard step. This module:
1. extracts the precondition text;
2. asks the LLM whether the current snapshot already satisfies it;
3. if not, drives a short action loop until it does (or fails).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_PRECOND_MAX_TURNS = 12

_PRECOND_LINE_RE = re.compile(
    r"^\s*前置条件\s*[:：]\s*(.+?)\s*$",
    re.I | re.M,
)
_PRECOND_BLOCK_RE = re.compile(
    r"前置条件\s*[:：]\s*(.+)",
    re.I | re.S,
)
_JSON_RE = re.compile(r"\{[\s\S]*\}")

PRECOND_CHECK_SYSTEM = """You judge whether a UI test PRECONDITION is already satisfied
on the current page from an accessibility SNAPSHOT only.

Rules:
1. Treat Chinese "已打开/已进入/已登录/弹窗已打开" as REQUIRED CURRENT STATE, not history.
2. met=true ONLY if the snapshot clearly shows that state (menu selected, dialog visible,
   page title/section present, etc.).
3. If the snapshot looks like a different module/home/login page, met=false.
4. If unsure, met=false.
5. Output ONLY one JSON object: {"met": true|false, "reason": "brief"}.
"""

PRECOND_EXEC_SYSTEM = """You are a browser agent establishing a UI test PRECONDITION
before the real checklist steps run.

The precondition text may say "已打开/已进入…" — rewrite that mentally as an instruction:
actively navigate/open until that state is true on the live page.

IMPORTANT: An external verifier already said the precondition is NOT met.
You MUST status="continue" and perform a real UI action (usually click a sidebar/
menu/button to enter the required page or open the required dialog).
Do NOT status="done" — the verifier decides that. Do NOT only "wait" unless the
page is clearly loading.

Rules:
1. status="continue" + one click/fill/goto/select/evaluate toward the precondition.
2. Prefer snapshot refs for menus like 合规检查 / 新建检查任务.
   selector MUST be the bare ref only, e.g. "e12" or "f4e135" — NEVER "[ref=e12]".
3. Do NOT work on the main test steps — only establish the precondition.
4. status="fail" only if the precondition is impossible from this site after real attempts.
   If the snapshot is nearly empty (splash/image/loading) or menus are missing, status="continue"
   with action="wait" (value="2" or "3") — do NOT fail early.
5. If a previous action failed with the same selector, pick a DIFFERENT ref or evaluate.
6. Output ONLY one JSON object:
{"status":"continue|fail","thinking":"...","action":"click|fill|goto|wait|select|press_key|hover|evaluate",
 "selector":"e12","value":"...","stable_hint":"...","checklist_index":null,"checklist_note":"PRECONDITION"}
"""


def split_case_description(
    description: str | None,
) -> tuple[Optional[str], Optional[str]]:
    """Split ``description`` into ``(precondition, remaining)``.

    Supports:
    - whole field: ``前置条件：xxx``
    - leading line then other notes
    """
    raw = (description or "").strip()
    if not raw:
        return None, None

    m_line = _PRECOND_LINE_RE.search(raw)
    if m_line and m_line.start() == 0:
        pre = (m_line.group(1) or "").strip()
        rest = (raw[m_line.end() :] or "").strip()
        return (pre or None), (rest or None)

    if raw.startswith("前置条件") or raw.lower().startswith("precondition"):
        m = _PRECOND_BLOCK_RE.search(raw)
        if m:
            pre = (m.group(1) or "").strip()
            return (pre or None), None

    # No explicit marker — do not treat free-form description as precondition
    return None, raw


def build_precondition_goal_text(precondition: str) -> str:
    pre = (precondition or "").strip()
    return (
        "PRECONDITION PHASE (before main checklist):\n"
        f"{pre}\n\n"
        "Interpret '已打开/已进入/弹窗已打开' as actions you must achieve NOW "
        "if the snapshot does not already show that state.\n"
        "When the live page matches the precondition, status=done.\n"
    )


def _parse_met_json(raw: str) -> tuple[bool, str]:
    text = (raw or "").strip()
    m = _JSON_RE.search(text)
    blob = m.group(0) if m else text
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise ValueError("precondition check not a JSON object")
    met = bool(data.get("met"))
    reason = str(data.get("reason") or "").strip() or ("met" if met else "not met")
    return met, reason


async def verify_precondition_met(
    *,
    client: AsyncOpenAI,
    model: str,
    snapshot: str,
    precondition: str,
    temperature: float = 0.0,
) -> tuple[bool, str]:
    """Return ``(met, reason)`` from snapshot + precondition text."""
    pre = (precondition or "").strip()
    if not pre:
        return True, "empty precondition"
    user = (
        f"PRECONDITION:\n{pre}\n\n"
        f"CURRENT ACCESSIBILITY SNAPSHOT:\n"
        f"{(snapshot or '(empty)')[:100000]}\n\n"
        "Is this precondition already satisfied? JSON only."
    )
    messages = [
        {"role": "system", "content": PRECOND_CHECK_SYSTEM},
        {"role": "user", "content": user},
    ]
    last_err: Optional[str] = None
    for attempt in range(3):
        try:
            if attempt and last_err:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Previous output invalid: {last_err}. "
                            'Output ONLY {"met":bool,"reason":"..."}.'
                        ),
                    }
                )
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=256,
            )
            content = (resp.choices[0].message.content or "").strip()
            return _parse_met_json(content)
        except Exception as exc:
            last_err = str(exc)
            logger.warning("verify_precondition_met attempt %s failed: %s", attempt + 1, exc)
    logger.warning("verify_precondition_met giving up: %s", last_err)
    return False, f"precondition check failed: {last_err}"


async def decide_precondition_action(
    *,
    client: AsyncOpenAI,
    model: str,
    precondition: str,
    snapshot: str,
    journal_tail: list[dict[str, Any]],
    temperature: float = 0.15,
) -> Any:
    """Ask LLM for one action to establish the precondition (or done/fail)."""
    from core.goal_agent_loop import GoalAction, _parse_goal_action

    goal = build_precondition_goal_text(precondition)
    recent = journal_tail[-6:] if journal_tail else []
    user = (
        f"{goal}\n"
        "VERIFIER RESULT: precondition NOT met on the current page.\n"
        "Take ONE real UI action now (click menu/button to enter the required page "
        "or open the required dialog). status must be continue or fail — not done.\n\n"
        f"RECENT PRECONDITION ACTIONS:\n"
        f"{json.dumps(recent, ensure_ascii=False, indent=2)}\n\n"
        f"CURRENT ACCESSIBILITY SNAPSHOT:\n"
        f"{(snapshot or '(empty)')[:100000]}\n\n"
        "Decide the next single action JSON now."
    )
    messages = [
        {"role": "system", "content": PRECOND_EXEC_SYSTEM},
        {"role": "user", "content": user},
    ]
    last_err: Optional[str] = None
    for attempt in range(3):
        try:
            if attempt and last_err:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Previous output invalid: {last_err}. Output ONLY valid JSON.",
                    }
                )
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=1024,
            )
            content = (resp.choices[0].message.content or "").strip()
            return _parse_goal_action(content)
        except Exception as exc:
            last_err = str(exc)
            logger.warning(
                "decide_precondition_action attempt %s failed: %s", attempt + 1, exc
            )
    raise ValueError(f"decide_precondition_action failed: {last_err}")


_DIALOG_PRECOND_RE = re.compile(r"弹窗|对话框|dialog|modal", re.I)
_UNDO_DIALOG_RE = re.compile(
    r"关闭(?:此)?对话|关闭弹窗|close\s+(?:the\s+)?dialog|关闭此对话框|"
    r"headerbtn|el-dialog__headerbtn|dismiss\s+(?:the\s+)?dialog|"
    r"关闭新建|关掉弹窗",
    re.I,
)


def precondition_requires_dialog(precondition: str | None) -> bool:
    return bool(precondition and _DIALOG_PRECOND_RE.search(precondition))


def decision_undoes_precondition_dialog(
    decision: Any,
    *,
    precondition: str | None,
    steps: list[dict[str, Any]] | None = None,
) -> bool:
    """True when the decision would dismiss a dialog that precondition requires."""
    if not precondition_requires_dialog(precondition):
        return False
    # Allow close if checklist itself asks to close
    for s in steps or []:
        desc = (s.get("description") or s.get("original_description") or "")
        if re.search(r"关闭|dismiss|close\s+dialog", desc, re.I):
            return False
    blob = " ".join(
        str(x or "")
        for x in (
            getattr(decision, "thinking", None),
            getattr(decision, "stable_hint", None),
            getattr(decision, "checklist_note", None),
            getattr(decision, "value", None),
            getattr(decision, "action", None),
        )
    )
    if _UNDO_DIALOG_RE.search(blob):
        return True
    # Bare close-looking accessible names in stable_hint / note
    try:
        from core.step_intent import is_close_control_name

        for part in (
            getattr(decision, "stable_hint", None),
            getattr(decision, "checklist_note", None),
        ):
            # stable_hint often "button '关闭此对话框'" — extract quoted name
            if not part:
                continue
            m = re.search(r"['\"]([^'\"]+)['\"]", str(part))
            name = m.group(1) if m else str(part)
            if is_close_control_name(name) or "关闭此对话框" in str(part):
                return True
    except Exception:
        pass
    return False


def overlay_intercept_hint(error: str | None) -> Optional[str]:
    """Hint for next decide round when dialog overlay blocked a background click."""
    err = (error or "").lower()
    if not err:
        return None
    if any(
        k in err
        for k in (
            "intercepts pointer",
            "pointer events",
            "subtree intercepts",
            "element is outside of the viewport",
            "not visible",
        )
    ):
        return (
            "SYSTEM HINT: previous click was blocked by an overlay/dialog. "
            "Re-pick a control INSIDE the open dialog that matches the checklist "
            "(e.g. placeholder/label 请选择检查单位). Do NOT close the dialog."
        )
    return None


_AX_DIALOG_RE = re.compile(
    r"^\s*-\s*(?:dialog|alertdialog)\b|"
    r"\[(?:role=)?(?:dialog|alertdialog)\]|"
    r"el-dialog|新建检查任务",
    re.I | re.M,
)


def snapshot_still_has_precondition_dialog(
    snapshot: str | None,
    precondition: str | None,
) -> bool:
    """Heuristic: when precondition requires a dialog, snapshot should still show one."""
    if not precondition_requires_dialog(precondition):
        return True
    text = snapshot or ""
    if not text.strip():
        return False
    return bool(_AX_DIALOG_RE.search(text))


# Click Element UI / tree-select "unit" control inside the visible dialog.
# Used when AX snapshot truncates dialog body and LLM cannot see the ref.
CLICK_DIALOG_UNIT_SELECT_JS = """() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const wrappers = [
    ...document.querySelectorAll('.el-dialog__wrapper'),
    ...document.querySelectorAll('[role="dialog"]'),
  ].filter(visible);
  const root =
    wrappers.find((r) => /新建|检查任务|被检|检查单位/.test(r.innerText || '')) ||
    wrappers[0];
  if (!root) return false;
  const needles = ['请选择检查单位', '检查单位', '请选择单位', '请选择'];
  const inputs = [...root.querySelectorAll('input')].filter(visible);
  for (const inp of inputs) {
    if (inp.disabled) continue;
    const ph = inp.getAttribute('placeholder') || '';
    if (needles.some((n) => ph.includes(n))) {
      inp.click();
      return true;
    }
  }
  for (const lab of root.querySelectorAll('.el-form-item__label, label')) {
    const t = (lab.textContent || '').trim();
    if (!/(检查单位|被检单位|单位)/.test(t)) continue;
    const item = lab.closest('.el-form-item') || lab.parentElement;
    if (!item) continue;
    const ctrl = item.querySelector(
      'input:not([disabled]), .el-select, .treeSelect_div, [class*="treeSelect"], .el-input'
    );
    if (!ctrl) continue;
    const clickable = ctrl.matches('input')
      ? ctrl
      : ctrl.querySelector('input:not([disabled])') || ctrl;
    if (clickable && visible(clickable)) {
      clickable.click();
      return true;
    }
  }
  for (const inp of inputs) {
    if (inp.disabled) continue;
    const ph = inp.getAttribute('placeholder') || '';
    if (ph.includes('请选择')) {
      inp.click();
      return true;
    }
  }
  return false;
}"""


_UNIT_DROPDOWN_STEP_RE = re.compile(
    r"请选择检查单位|检查单位.*下拉|下拉框.*单位|点击.*检查单位",
    re.I,
)


def is_unit_dropdown_checklist_step(description: str | None) -> bool:
    return bool(description and _UNIT_DROPDOWN_STEP_RE.search(description))
