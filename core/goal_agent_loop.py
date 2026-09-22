# core/goal_agent_loop.py
"""Checklist coverage helpers shared by OTA / script solidification.

Journal coverage rules used by agent_bridge, script_synthesize, replay_resolve,
and script_templates. The nl_goal decide loop has been removed.
"""
from __future__ import annotations

import re
from typing import Any

REPAIR_SYSTEM_PROMPT = """You repair a Playwright Python script that failed on dry-run.
Keep the same overall flow. Fix selectors/strict-mode issues:
- use .first and :visible / :not([disabled]):visible filters for duplicate/hidden twins
- for unit/tree selects: open the combobox (.treeSelect_div or the visible placeholder),
  type into the enabled+visible filter input with press_sequentially (never .fill alone),
  then click the EXACT .el-tree-node__label full-text match — never a parent/prefix node
- for Element UI overlays: loop .el-dialog__wrapper:visible → footer /关\\s*闭/ else
  .el-dialog__headerbtn; .el-notification → .el-notification__closeBtn
- the CHECKLIST in the prompt is the source of truth: NEVER introduce intermediate or
  failed journal values (wrong username, parent node) into the script
Output ONLY the full Python script, no markdown fences. The script MUST define
async def test_case_{case_id}(page).
"""


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


_CLOSE_MESSAGES_STEP_RE = re.compile(
    r"关闭.*(?:消息|通知|弹窗|对话框|按钮)|消息的关闭|关闭按钮|所有出现的消息",
    re.I,
)
_OPEN_NAV_STEP_RE = re.compile(r"^打开【|^打开\s|跳转到|导航到|访问https?://|打开页面", re.I)
_CLICK_STEP_RE = re.compile(r"^(?:点击|单击|click\b)", re.I)
_FILL_STEP_RE = re.compile(r"输入|填写|填入|\bfill\b|\btype\b", re.I)
_SELECT_STEP_RE = re.compile(r"选择|选中|\bselect\b", re.I)
_PRESS_KEY_STEP_RE = re.compile(
    r"按键|快捷键|\bESC\b|\bEnter\b|回车键?|空格键|按下.+键"
    r"|按\s*(?:ESC|Enter|回车|空格|Tab|删除|退格|方向|上下|F\d+)",
    re.I,
)
_WAIT_ASSERT_STEP_RE = re.compile(
    r"等待|断言|出现文本|文本出现|包含文本|可见文本|"
    r"\bassert(?:_text)?\b|\bwait(?:_for)?\b|expect.*text",
    re.I,
)


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


def is_press_key_checklist_step(description: str | None) -> bool:
    """True for checklist items whose primary intent is pressing a key."""
    if not description:
        return False
    return bool(_PRESS_KEY_STEP_RE.search(description.strip()))


def is_wait_assert_checklist_step(description: str | None) -> bool:
    """True for checklist items whose primary intent is wait/assert text."""
    if not description:
        return False
    desc = description.strip()
    if is_click_checklist_step(desc) or is_fill_checklist_step(desc):
        return False
    if is_close_messages_checklist_step(desc) or is_press_key_checklist_step(desc):
        return False
    return bool(_WAIT_ASSERT_STEP_RE.search(desc))


def _wait_value_covers_assert_step(entry: dict[str, Any], desc: str) -> bool:
    """wait/assert 的 value 必须对应步骤里的期望文本；纯秒数等待不得冒充文本断言。"""
    value = str(entry.get("value") or "").strip()
    if not value:
        return False
    # 纯时间等待：仅覆盖「等待 N 秒」且步骤不含期望文本
    if value.replace(".", "", 1).isdigit():
        if re.search(r"出现|文本|包含|断言|assert|\btext\b", desc, re.I):
            return False
        return bool(re.search(r"等待|wait", desc, re.I))
    # 文本等待：value 须出现在步骤描述中（或步骤摘出的引号/【】片段等于 value）
    if value in desc:
        return True
    for m in re.finditer(r"[「【\"']([^」】\"']+)[」】\"']", desc):
        if value == m.group(1).strip() or m.group(1).strip() in value:
            return True
    return False


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
    """True when evaluate result_snippet clearly indicates a falsy JS return.

    Snippets often embed the JS source (which may contain ``return true``);
    only the ``### Result`` payload (or a bare return) counts.
    """
    snippet = str(entry.get("result_snippet") or "").strip()
    if not snippet:
        # No result text — cannot prove the click happened
        return False
    # Prefer explicit Result block from Playwright MCP
    m = re.search(
        r"###\s*Result\s*\n([^\n#]+)",
        snippet,
        re.I,
    )
    result_text = (m.group(1) if m else snippet).strip().lower()
    # Strip code fences / quotes
    result_text = result_text.strip("`\"' ")
    if result_text in ("false", "null", "undefined", "0", "none", ""):
        return True
    if result_text.startswith("false"):
        return True
    # Bare false token at start of result (avoid matching source ``return true``)
    if re.match(r"false\b", result_text):
        return True
    return False


def _evaluate_is_real_click_action(entry: dict[str, Any]) -> bool:
    """True when evaluate actually performs a DOM click (not a no-op / verify)."""
    if _evaluate_result_looks_falsy(entry):
        return False
    # Prefer JS source in value; do not trust thinking alone
    blob = " ".join(
        str(x or "")
        for x in (
            entry.get("value"),
            entry.get("checklist_note"),
            entry.get("stable_hint"),
        )
    )
    if not re.search(r"\.click\s*\(|dispatchEvent\s*\(", blob, re.I):
        return False
    # Must have a truthy result when a Result block exists
    snippet = str(entry.get("result_snippet") or "")
    if re.search(r"###\s*Result\s*\n", snippet, re.I):
        m = re.search(r"###\s*Result\s*\n([^\n#]+)", snippet, re.I)
        result_text = (m.group(1) if m else "").strip().lower().strip("`\"' ")
        if result_text in ("true", "1"):
            return True
        # Non-boolean object/string returns are ok if click ran and not falsy
        if result_text and result_text not in ("false", "null", "undefined", "0", "none"):
            return True
        return False
    return True


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

    # Explicit key-press intent is the most specific: a successful ESC/Enter
    # press covers it even when the text also mentions a dialog/message.
    if is_press_key_checklist_step(desc):
        return action in ("press_key", "browser_press_key")
    # A key press must not fake-cover click/fill/assert steps.
    if action in ("press_key", "browser_press_key"):
        return False

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

    # wait/assert 只能覆盖等待/断言类步骤，且 value 必须对齐期望文本
    if action in ("wait", "assert_text", "browser_wait_for"):
        if not desc or not is_wait_assert_checklist_step(desc):
            return False
        return _wait_value_covers_assert_step(entry, desc)

    if action == "screenshot":
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

    # 等待/断言步骤只接受 wait/assert 动作（上文已处理）；其它动作不得冒充
    if is_wait_assert_checklist_step(desc):
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
