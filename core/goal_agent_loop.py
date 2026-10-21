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


def _step_wants_absence(desc: str) -> bool:
    """步骤语义是否为「不存在/不包含/消失」类负向断言（034）。

    先剔除引号/【】中的期望文本——文本内容里的「不存在」是断言对象，
    不是断言极性（例：断言页面包含【这段文字绝对不存在】是**正向**断言）。
    """
    stripped = re.sub(r"[「【\"'][^」】\"']*[」】\"']", " ", desc or "")
    return bool(re.search(
        r"消失|不包含|不存在|不可见|不显示|不出现|不应(?:包含|存在|可见|出现)"
        r"|not\s*(?:contain|exist|be\s*visible|appear)",
        stripped, re.I,
    ))


def _wait_value_covers_assert_step(entry: dict[str, Any], desc: str) -> bool:
    """wait/assert 的 value 必须对应步骤里的期望文本；纯秒数等待不得冒充文本断言。

    034 极性守卫：`wait(wait_mode=gone)` 不得覆盖正向断言（「断言页面包含【x】」），
    反之「等待【x】消失」也不得被正向等待覆盖——否则模型可反转断言语义骗过完成判定
    （实测：case 670 第二次运行时 wait(gone) 冒充「断言包含」导致假通过）。
    """
    value = str(entry.get("value") or "").strip()
    if not value:
        return False
    # 纯时间等待：仅覆盖「等待 N 秒」且步骤不含期望文本
    if value.replace(".", "", 1).isdigit():
        # 断言动作的纯数字期望文本（如 断言徽标数字为【1】）与描述中的
        # 【】/「」片段全等时按文本断言处理；秒数等待的防护保持在后
        if (entry.get("action") or "").strip().lower() == "assert_text":
            for m in re.finditer(r"[「【\"']([^」】\"']+)[」】\"']", desc):
                if value == m.group(1).strip():
                    return True
        if re.search(r"出现|文本|包含|断言|assert|\btext\b", desc, re.I):
            return False
        return bool(re.search(r"等待|wait", desc, re.I))
    # 文本等待：value 须出现在步骤描述中（或步骤摘出的引号/【】片段等于 value）
    if value in desc or any(
        value == m.group(1).strip() or m.group(1).strip() in value
        for m in re.finditer(r"[「【\"']([^」】\"']+)[」】\"']", desc)
    ):
        action = (entry.get("action") or "").strip().lower()
        if action in ("wait", "browser_wait_for"):
            wait_mode = str(entry.get("wait_mode") or "").strip().lower()
            gone = wait_mode in ("gone", "textgone", "disappear", "hidden")
            wants_absence = _step_wants_absence(desc)
            if gone != wants_absence:
                return False
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

    # 文件/弹窗类动作（描述优先编辑）：纯文字描述里的「选择框/文件名」等词
    # 会被粗略意图词表误判为 select/click，这里先按文件/弹窗语义判定
    if action in (
        "upload",
        "file_upload",
        "browser_file_upload",
        "drop",
        "browser_drop",
    ):
        if is_click_checklist_step(desc):
            return False  # 明确以「点击」开头 → 属于点击步骤
        if not desc or re.search(
            r"上传|附件|拖拽|拖入|拖放|拖到|拖进|选择框|上传框", desc
        ):
            return True
        if re.search(r"文件|路径", desc) and not is_select_checklist_step(desc):
            return True
        if (
            is_click_checklist_step(desc)
            or is_fill_checklist_step(desc)
            or is_select_checklist_step(desc)
            or _OPEN_NAV_STEP_RE.search(desc)
        ):
            return False
        return True

    if action in ("dialog", "browser_handle_dialog"):
        if not desc or re.search(
            r"弹窗|对话框|提示框|确认框|\balert\b|\bconfirm\b", desc, re.I
        ):
            return True
        if (
            is_click_checklist_step(desc)
            or is_fill_checklist_step(desc)
            or is_select_checklist_step(desc)
            or _OPEN_NAV_STEP_RE.search(desc)
        ):
            return False
        return True

    if desc and _OPEN_NAV_STEP_RE.search(desc):
        return action in (
            "goto",
            "navigate",
            "browser_navigate",
            "click",
            "browser_click",
        )

    # wait/assert 覆盖语义：词表识别为等待/断言时按期望文本对齐；自由文字描述
    # （未被识别为明确交互意图）仅当断言值在描述中有字面依据才可覆盖
    if action in ("wait", "assert_text", "browser_wait_for"):
        if not desc:
            return False
        if is_wait_assert_checklist_step(desc):
            return _wait_value_covers_assert_step(entry, desc)
        if (
            is_click_checklist_step(desc)
            or is_fill_checklist_step(desc)
            or is_select_checklist_step(desc)
            or _OPEN_NAV_STEP_RE.search(desc)
        ):
            return False
        return _wait_value_covers_assert_step(entry, desc)

    if action == "screenshot":
        return False

    # 036：fetch（以 API 方式完成）只覆盖「导出/下载/导入/提交/接口」类交互步骤；
    # 038 收紧：无关业务描述（如「整理交付材料」）不得被一次任意 fetch 冒充覆盖。
    if action in ("fetch", "browser_fetch"):
        if is_wait_assert_checklist_step(desc):
            return False
        if not desc:
            return False
        if re.search(
            r"导出|下载|导入|提交|上传|接口|API|报表|文件|附件|数据同步|拉取|获取",
            desc, re.I,
        ):
            return True
        return _literal_overlap(desc, entry)

    # 036+：诊断脚本只覆盖「脚本/查询/核对」类步骤；交互/断言类不得被冒充
    if action in ("run_script", "browser_run_script"):
        if not desc:
            return False
        return bool(re.search(r"脚本|代码|查询|查库|核对|诊断|数据库", desc))

    if is_click_checklist_step(desc):
        if action in ("click", "browser_click"):
            return True
        if action in ("evaluate", "browser_evaluate", "js", "eval"):
            return _evaluate_is_real_click_action(entry)
        # 038：坐标点击只有在回读命中元素与描述字面一致时才可覆盖点击步骤
        if action in (
            "click_xy", "browser_mouse_click_xy",
            "drag_xy", "browser_mouse_drag_xy",
        ):
            return _literal_overlap(desc, entry)
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

    # 038：坐标/鼠标动作可覆盖「点选/拖动」类未分类步骤（动作类型与意图一致；
    # 是否真的点中目标由后续断言/页面状态兜底）。
    if action in (
        "click_xy", "browser_mouse_click_xy", "drag_xy", "browser_mouse_drag_xy",
        "mouse_down", "browser_mouse_down", "mouse_up", "browser_mouse_up",
    ) and re.search(r"点击|点选|单击|双击|右键|拖动|拖拽|滑动|图形|验证码", desc):
        return True

    # 038：未分类的业务描述不再被"任意成功动作"兜底覆盖。
    # 只有显式声明 checklist_index，或动作目标/文案/URL 与描述存在字面依据时才可覆盖；
    # 否则保持未覆盖（宁可继续尝试或如实失败，也不假通过）。
    # 注意：journal 里的 checklist_index 是覆盖后回填的推断值，不能当作"显式声明"；
    # 仅识别调用方显式标注的 _explicit_checklist（模型明确指定步骤号时由调用方写入）。
    if entry.get("_explicit_checklist") is not None:
        return True
    if not desc:
        return False
    return _literal_overlap(desc, entry)


_GENERIC_STEP_TOKENS = {
    "查看", "检查", "确认", "验证", "页面", "功能", "主要", "进行", "操作",
    "显示", "正确", "数据", "状态", "成功", "列表", "信息", "系统", "当前",
    "是否", "以及", "相关", "所有", "一个", "一下", "内容", "测试", "用例",
    "步骤", "结果", "完成", "正常", "可以", "需要", "支持", "包含", "出现",
}


def _entry_haystack(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("element_desc", "value", "selector", "target", "url"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    resolved = entry.get("resolved")
    if isinstance(resolved, dict):
        for key in ("name", "text", "selector", "role", "tag"):
            v = resolved.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
    return "\n".join(parts)


def _longest_common_substring(a: str, b: str) -> str:
    """最长公共子串（短文本 DP；用于中文描述与目标文案的字面比对）。"""
    a, b = a[:200], b[:400]
    prev = [0] * (len(b) + 1)
    best_len = 0
    best_end = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len = cur[j]
                    best_end = i
        prev = cur
    return a[best_end - best_len:best_end]


def _literal_overlap(description: str, entry: dict[str, Any]) -> bool:
    """描述与动作目标是否存在可核对的重合（中文≥3 字公共子串 / 英文整词）。

    用于未分类步骤与坐标动作的覆盖判定：没有重合 = 无法证明做了这件事。
    纯泛词（验证/列表/数据…）不算依据。
    """
    haystack = _entry_haystack(entry)
    if not haystack:
        return False
    hay = haystack.lower()
    desc = str(description or "").lower()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", desc):
        if run in _GENERIC_STEP_TOKENS:
            continue
        if len(run) <= 2:
            if run in hay:
                return True
            continue
        common = _longest_common_substring(run, hay)
        if len(common) >= 3 and common not in _GENERIC_STEP_TOKENS:
            return True
    for word in re.findall(r"[a-z][a-z0-9_-]{2,}", desc):
        if word in _GENERIC_STEP_TOKENS:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", hay):
            return True
    return False


def coverage_reason(entry: dict[str, Any], *, step_description: str | None = None) -> str:
    """覆盖被拒绝的粗粒度原因（诊断用，不改变判定结果）。"""
    if not entry.get("success"):
        return "action_failed"
    desc = (step_description or "").strip()
    if entry.get("_explicit_checklist") is not None:
        return "explicit_checklist"
    if not desc:
        return "no_description"
    if is_click_checklist_step(desc):
        return "click_mismatch"
    if is_fill_checklist_step(desc):
        return "fill_mismatch"
    if is_select_checklist_step(desc):
        return "select_mismatch"
    if is_close_messages_checklist_step(desc):
        return "close_mismatch"
    if is_press_key_checklist_step(desc):
        return "press_mismatch"
    if is_wait_assert_checklist_step(desc):
        return "assert_mismatch"
    if _OPEN_NAV_STEP_RE.search(desc):
        return "nav_mismatch"
    return "literal_match" if _literal_overlap(desc, entry) else "unclassified_no_evidence"


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
