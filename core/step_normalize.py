# core/step_normalize.py
"""Shared UI step normalize / StructuredStep helpers (gen + execution).

Safe for Agent offline packaging — stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Optional

UI_ACTIONS = frozenset({
    "goto",
    "click",
    "fill",
    "select",
    "check",
    "uncheck",
    "wait",
    "assert_text",
    "assert_visible",
    "hover",
    "press_key",
    "click_blank",
    "icon_click",
})

# Map legacy / alias action names → canonical
_ACTION_ALIASES = {
    "assert": "assert_text",
    "press": "press_key",
    "screenshot": "wait",  # not executable as assert; keep benign
    "scroll": "hover",
    "input": "fill",
    "type": "fill",
    "navigate": "goto",
    "open": "goto",
}

_CTRL_TYPE_SUFFIX = (
    r"(?:下拉框|下拉菜单|下拉列表|选择器|输入框|文本框|文本域|编辑框|"
    r"组合框|按钮|控件|弹窗|对话框|提示框|"
    r"图标|图片|图像|箭头|符号|徽标|logo|icon|image|img)"
)

_GENERIC_BRACKET_ONLY = frozenset({
    "图标", "图片", "图像", "箭头", "符号", "徽标", "logo", "icon", "image", "img",
    "按钮", "控件", "链接", "菜单",
})

_CTRL_TYPE_IN_NAME_RE = re.compile(
    rf"(?:^{_CTRL_TYPE_SUFFIX}$|{_CTRL_TYPE_SUFFIX}$)",
    re.IGNORECASE,
)

_ELLIPSIS_IN_TEXT_RE = re.compile(r"(?:…+|\.{2,}|。{2,})")


def strip_ellipsis_in_label(inner: str) -> str:
    """Remove truncated tails from a label so execution can longest-match."""
    s = (inner or "").strip()
    if not s:
        return s
    s2 = re.sub(
        r"[（(][^）)]*(?:…+|\.{2,}|。{2,})[^）)]*[）)]\s*$",
        "",
        s,
    ).strip()
    s2 = re.sub(r"(?:…+|\.{2,}|。{2,})\s*$", "", s2).strip()
    s2 = re.sub(r"[（(]\s*$", "", s2).strip()
    return s2 or s


def sanitize_brackets_ellipsis(text: str) -> str:
    """Rewrite every 【…】 so inner labels do not contain ellipsis truncation."""

    def _repl(m: re.Match) -> str:
        return f"【{strip_ellipsis_in_label(m.group(1))}】"

    return re.sub(r"【([^】]*)】", _repl, text or "")


def label_has_ellipsis(text: str | None) -> bool:
    return bool(text and _ELLIPSIS_IN_TEXT_RE.search(text))


def label_has_control_type_word(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if t.lower() in _GENERIC_BRACKET_ONLY:
        return True
    return bool(re.search(_CTRL_TYPE_SUFFIX + r"$", t, re.IGNORECASE))


def expand_compound_ui_step(step: str) -> list[str]:
    """Split known multi-action phrases into single-action steps."""
    s = (step or "").strip()
    if not s:
        return []
    close_all = (
        re.search(r"(?:把|将)?所有(?:的)?(?:对话框|弹窗|提示框)", s)
        or re.search(r"(?:关闭|关掉)所有(?:的)?(?:对话框|弹窗|提示框)", s)
    )
    mentions_close = re.search(
        r"点击【(?:关闭|X|×)】|点击.*(?:关闭|【X】)|关闭标志|关闭按钮",
        s,
    )
    if close_all and (mentions_close or "关闭" in s or "【X】" in s):
        return [
            "等待弹窗或对话框出现",
            "点击【关闭】",
        ]
    return [s]


def sanitize_ui_step(step: str) -> str:
    """Rewrite common non-executable step phrasings into【visible-label】form."""
    s = (step or "").strip()
    if not s:
        return s

    s = sanitize_brackets_ellipsis(s)

    if re.fullmatch(
        r"等待(?:页面)?(?:加载)?完成|等待加载完成|等待页面稳定",
        s,
    ):
        return "等待页面稳定"

    m = re.match(rf"^(.+?){_CTRL_TYPE_SUFFIX}\s*选择\s*【([^】]+)】\s*$", s)
    if m:
        field, opt = m.group(1).strip(), m.group(2).strip()
        if field:
            return f"在【{field}】中选择【{opt}】"

    m = re.match(rf"^点击【(.+?){_CTRL_TYPE_SUFFIX}】\s*$", s)
    if m:
        label = m.group(1).strip()
        if label and label.lower() not in _GENERIC_BRACKET_ONLY:
            return f"点击【{label}】"

    m = re.match(rf"^点击\s*(.+?){_CTRL_TYPE_SUFFIX}\s*$", s)
    if m and "【" not in s:
        label = m.group(1).strip(" ：:的")
        if label and label.lower() not in _GENERIC_BRACKET_ONLY:
            return f"点击【{label}】"

    m = re.match(
        r"^点击【(?P<br>[^】]+)】\s*[（(](?P<hint>[^）)]+)[）)]\s*$",
        s,
    )
    if m and m.group("br").strip().lower() in _GENERIC_BRACKET_ONLY:
        hint = m.group("hint").strip()
        if hint:
            return f"点击【{hint}】"
    m = re.match(
        r"^点击\s*(?P<head>.+?)【(?P<br>[^】]+)】\s*$",
        s,
    )
    if m and m.group("br").strip().lower() in _GENERIC_BRACKET_ONLY:
        head = re.sub(rf"{_CTRL_TYPE_SUFFIX}$", "", m.group("head")).strip(" ：:的")
        if head and head.lower() not in _GENERIC_BRACKET_ONLY:
            return f"点击【{head}】"

    m = re.match(
        rf"^(?:在\s*)?(.+?){_CTRL_TYPE_SUFFIX}\s*(?:中)?\s*(?:输入|填写|填入)\s*(.+)$",
        s,
    )
    if m and "【" not in s:
        field, value = m.group(1).strip(" ：:的"), m.group(2).strip()
        if field and value:
            return f"在【{field}】输入 {value}"

    m = re.match(
        rf"^(?:在\s*)?(.+?)(?:{_CTRL_TYPE_SUFFIX})?\s*(?:输入|填写|填入)\s*【([^】]+)】\s*$",
        s,
    )
    if m:
        field, value = m.group(1).strip(" ：:的"), m.group(2).strip()
        if field and value and "【" not in field:
            field = re.sub(rf"{_CTRL_TYPE_SUFFIX}$", "", field).strip(" ：:的")
            if field:
                return f"在【{field}】输入 {value}"

    m = re.match(
        r"^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*(?:输入|填写|填入)\s*【([^】]+)】\s*$",
        s,
    )
    if m:
        return f"在【{m.group(1).strip()}】输入 {m.group(2).strip()}"

    def _strip_ctrl_in_brackets(match: re.Match) -> str:
        inner = match.group(1)
        cleaned = re.sub(rf"{_CTRL_TYPE_SUFFIX}$", "", inner).strip()
        if not cleaned or cleaned.lower() in _GENERIC_BRACKET_ONLY:
            return f"【{inner}】"
        return f"【{cleaned}】"

    s2 = re.sub(r"【([^】]+)】", _strip_ctrl_in_brackets, s)
    return sanitize_brackets_ellipsis(s2)


def _clean_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    s = strip_ellipsis_in_label(str(name).strip())
    if not s:
        return None
    cleaned = re.sub(rf"{_CTRL_TYPE_SUFFIX}$", "", s, flags=re.IGNORECASE).strip()
    if cleaned and cleaned.lower() not in _GENERIC_BRACKET_ONLY:
        s = cleaned
    return s or None


def coerce_structured_step(raw: Any) -> Optional[dict[str, Any]]:
    """Normalize a dict (or Instant string) into a StructuredStep dict, or None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return parse_instant_to_structured(sanitize_ui_step(raw))
    if not isinstance(raw, dict):
        return None

    action = str(raw.get("action") or "").strip().lower()
    action = _ACTION_ALIASES.get(action, action)
    if action and action not in UI_ACTIONS:
        # Unknown action — try description fallback
        desc = raw.get("description") or raw.get("desc") or ""
        if desc:
            return parse_instant_to_structured(sanitize_ui_step(str(desc)))
        return None

    target_name = _clean_name(raw.get("target_name") or raw.get("target") or raw.get("name"))
    value = raw.get("value")
    if value is not None:
        value = str(value).strip()
        value = strip_ellipsis_in_label(value) if value else value
        if value == "":
            value = None

    target_role = raw.get("target_role") or raw.get("role")
    if target_role is not None:
        target_role = str(target_role).strip().lower() or None

    disambiguation = raw.get("disambiguation")
    if disambiguation is not None:
        disambiguation = str(disambiguation).strip() or None

    icon_hint = raw.get("icon_hint")
    if icon_hint is not None:
        icon_hint = str(icon_hint).strip() or None

    frame_hint = raw.get("frame_hint")
    if frame_hint is not None:
        frame_hint = str(frame_hint).strip() or None

    note = raw.get("note")
    if note is not None:
        note = str(note).strip() or None

    if not action:
        desc = raw.get("description") or raw.get("desc") or ""
        if desc:
            return parse_instant_to_structured(sanitize_ui_step(str(desc)))
        return None

    # select with field+option in one object may use target_name=field, value=option
    out: dict[str, Any] = {"action": action}
    if target_name is not None:
        out["target_name"] = target_name
    if target_role is not None:
        out["target_role"] = target_role
    if value is not None:
        out["value"] = value
    if disambiguation is not None:
        out["disambiguation"] = disambiguation
    if icon_hint is not None:
        out["icon_hint"] = icon_hint
    if frame_hint is not None:
        out["frame_hint"] = frame_hint
    if note is not None:
        out["note"] = note
    return out


def render_structured_step(step: dict[str, Any] | None) -> str:
    """Human-readable Instant description from StructuredStep."""
    if not step:
        return ""
    action = (step.get("action") or "").strip().lower()
    name = (step.get("target_name") or "").strip()
    value = step.get("value")
    value_s = "" if value is None else str(value).strip()
    dis = (step.get("disambiguation") or "").strip()
    icon = (step.get("icon_hint") or "").strip()
    suffix = f"（{dis}）" if dis else ""

    if action == "goto":
        label = name or value_s
        return f"打开【{label}】" if label else "打开页面"
    if action == "click":
        if name:
            return f"点击【{name}】{suffix}"
        return f"点击{suffix}" if suffix else "点击"
    if action == "fill":
        if name:
            return f"在【{name}】输入 {value_s}".rstrip()
        return f"输入 {value_s}".rstrip()
    if action == "select":
        # Prefer option in value; field may be target_name
        if name and value_s:
            return f"在【{name}】中选择【{value_s}】"
        opt = value_s or name
        return f"选择【{opt}】" if opt else "选择"
    if action == "check":
        return f"勾选【{name}】" if name else "勾选"
    if action == "uncheck":
        return f"取消勾选【{name}】" if name else "取消勾选"
    if action == "wait":
        label = value_s or name
        if label:
            return f"等待【{label}】出现"
        return "等待页面稳定"
    if action == "assert_text":
        label = value_s or name
        return f"断言页面包含【{label}】" if label else "断言页面包含文案"
    if action == "assert_visible":
        return f"断言【{name}】可见" if name else "断言元素可见"
    if action == "hover":
        return f"悬停【{name}】" if name else "悬停"
    if action == "press_key":
        return f"按键 {value_s or name}".strip()
    if action == "click_blank":
        return "点击空白处"
    if action == "icon_click":
        if icon:
            return icon if icon.startswith("点击") else f"点击{icon}"
        if name:
            return f"点击【{name}】"
        return "点击图标"
    # fallback
    if name:
        return f"{action}【{name}】" + (f" {value_s}" if value_s else "")
    return action or ""


def parse_instant_to_structured(step: str) -> Optional[dict[str, Any]]:
    """Best-effort parse of Instant NL into StructuredStep. Returns None if unknown."""
    s = sanitize_ui_step(step or "").strip()
    if not s:
        return None

    # click blank
    if re.search(r"点击\s*(?:页面)?(?:空白|外侧|遮罩)", s):
        return {"action": "click_blank"}

    # icon visual template
    m = re.match(
        r"^点击(?P<hint>.+?图标(?:（用途：[^）]+）)?)\s*$",
        s,
    )
    if m and "【" not in s:
        return {"action": "icon_click", "icon_hint": f"点击{m.group('hint').strip()}"}

    # goto / open
    m = re.match(r"^(?:打开|进入)\s*【([^】]+)】", s)
    if m:
        return {"action": "goto", "target_name": strip_ellipsis_in_label(m.group(1))}

    # wait
    m = re.match(r"^等待\s*【([^】]+)】\s*出现", s)
    if m:
        label = strip_ellipsis_in_label(m.group(1))
        return {"action": "wait", "target_name": label, "value": label}
    if re.fullmatch(r"等待(?:页面稳定|弹窗或对话框出现|加载完成)", s):
        return {"action": "wait", "value": None, "target_name": None, "note": s}

    # assert
    m = re.match(r"^断言页面包含\s*【([^】]+)】", s)
    if m:
        label = strip_ellipsis_in_label(m.group(1))
        return {"action": "assert_text", "value": label}
    m = re.match(r"^断言\s*【([^】]+)】\s*可见", s)
    if m:
        return {"action": "assert_visible", "target_name": strip_ellipsis_in_label(m.group(1))}

    # fill
    m = re.match(
        r"^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*(?:输入|填写|填入)\s*(.+)$",
        s,
    )
    if m:
        return {
            "action": "fill",
            "target_name": strip_ellipsis_in_label(m.group(1)),
            "target_role": "textbox",
            "value": m.group(2).strip(),
        }

    # select with field
    m = re.match(
        r"^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*选择\s*【([^】]+)】\s*$",
        s,
    )
    if m:
        return {
            "action": "select",
            "target_name": strip_ellipsis_in_label(m.group(1)),
            "target_role": "combobox",
            "value": strip_ellipsis_in_label(m.group(2)),
        }

    # select option only
    m = re.match(r"^选择\s*【([^】]+)】\s*$", s)
    if m:
        return {
            "action": "select",
            "target_name": strip_ellipsis_in_label(m.group(1)),
            "target_role": "option",
            "value": strip_ellipsis_in_label(m.group(1)),
        }

    # check / uncheck
    m = re.match(r"^取消勾选\s*【([^】]+)】", s)
    if m:
        return {
            "action": "uncheck",
            "target_name": strip_ellipsis_in_label(m.group(1)),
            "target_role": "checkbox",
        }
    m = re.match(r"^勾选\s*【([^】]+)】", s)
    if m:
        return {
            "action": "check",
            "target_name": strip_ellipsis_in_label(m.group(1)),
            "target_role": "checkbox",
        }

    # hover
    m = re.match(r"^悬停\s*【([^】]+)】", s)
    if m:
        return {"action": "hover", "target_name": strip_ellipsis_in_label(m.group(1))}

    # click with optional disambiguation
    m = re.match(
        r"^点击\s*【([^】]+)】\s*(?:[（(]([^）)]+)[）)])?\s*$",
        s,
    )
    if m:
        out: dict[str, Any] = {
            "action": "click",
            "target_name": strip_ellipsis_in_label(m.group(1)),
            "target_role": "button",
        }
        if m.group(2):
            out["disambiguation"] = m.group(2).strip()
        return out

    return None


def expand_structured_compounds(step: dict[str, Any]) -> list[dict[str, Any]]:
    """Split structured steps that encode multi-action close-all into two steps."""
    desc = render_structured_step(step)
    parts = expand_compound_ui_step(desc)
    if len(parts) <= 1:
        # select with field+option → keep as one select (executor may split)
        return [step]
    out: list[dict[str, Any]] = []
    for p in parts:
        parsed = parse_instant_to_structured(p)
        out.append(parsed or {"action": "wait", "note": p, "value": None})
    return out


def structured_step_is_complete(step: dict[str, Any] | None) -> bool:
    """Whether execution can skip Intent LLM for this step."""
    if not step or not isinstance(step, dict):
        return False
    action = (step.get("action") or "").strip().lower()
    if action not in UI_ACTIONS:
        return False
    name = (step.get("target_name") or "").strip()
    value = step.get("value")
    value_s = "" if value is None else str(value).strip()
    icon = (step.get("icon_hint") or "").strip()

    if action in ("click", "hover", "check", "uncheck", "assert_visible"):
        return bool(name)
    if action == "fill":
        return bool(name)  # value may be empty string intentionally
    if action == "select":
        return bool(name or value_s)
    if action in ("wait", "assert_text"):
        return True  # wait without label = page settle
    if action == "goto":
        return bool(name or value_s)
    if action == "press_key":
        return bool(value_s or name)
    if action == "click_blank":
        return True
    if action == "icon_click":
        return bool(icon or name)
    return False


def validate_structured_step_fields(
    step: dict[str, Any],
    *,
    index: int = 0,
    require_action: bool = True,
) -> list[str]:
    """Return list of hard-fail reasons for a structured step (UI gen)."""
    errors: list[str] = []
    n = index + 1
    action = (step.get("action") or "").strip().lower()
    if require_action and not action:
        errors.append(f"步骤 {n} 缺少 action")
        return errors
    if action and action not in UI_ACTIONS:
        errors.append(f"步骤 {n} 操作 '{action}' 不在合法列表中")
        return errors

    name = step.get("target_name")
    value = step.get("value")
    value_s = None if value is None else str(value).strip()
    icon = (step.get("icon_hint") or "").strip()

    if label_has_ellipsis(name if isinstance(name, str) else None):
        errors.append(f"步骤 {n} 的 target_name 含省略号，无法可靠定位")
    if label_has_ellipsis(value_s):
        errors.append(f"步骤 {n} 的 value 含省略号，无法可靠定位")
    if label_has_control_type_word(name if isinstance(name, str) else None):
        errors.append(f"步骤 {n} 的 target_name 含控件类型词，禁止: {name}")

    if action in ("click", "hover", "check", "uncheck", "assert_visible") and not (name or "").strip():
        errors.append(f"步骤 {n} ({action}) 缺少 target_name")
    if action == "fill" and not (name or "").strip():
        errors.append(f"步骤 {n} (fill) 缺少 target_name")
    if action == "select" and not ((name or "").strip() or value_s):
        errors.append(f"步骤 {n} (select) 缺少 target_name 或 value")
    if action == "goto" and not ((name or "").strip() or value_s):
        errors.append(f"步骤 {n} (goto) 缺少 target_name 或 value")
    if action == "icon_click" and not icon and not (name or "").strip():
        errors.append(f"步骤 {n} (icon_click) 缺少 icon_hint")
    if action in ("wait", "assert_text"):
        # wait without text is allowed (page settle); assert_text should have value
        if action == "assert_text" and not value_s and not (name or "").strip():
            errors.append(f"步骤 {n} (assert_text) 缺少 value")

    return errors
