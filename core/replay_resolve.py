# core/replay_resolve.py
"""Resolve durable replay locators for nl_goal journal entries."""
from __future__ import annotations

import re
from typing import Any, Optional

from core.codegen_locator import merge_codegen_into_replay
from core.goal_agent_loop import (
    is_click_checklist_step,
    is_close_messages_checklist_step,
    is_fill_checklist_step,
    is_press_key_checklist_step,
    is_select_checklist_step,
)

_BRACKET_RE = re.compile(r"【([^】]+)】")

# Backward-compatible alias; prefer codegen resolve in manager.
RESOLVE_DOM_ATTRS_JS = r"""() => {
  const out = { active: null };
  const el = document.activeElement;
  if (el && el !== document.body) {
    const r = el.getBoundingClientRect();
    out.active = {
      tag: (el.tagName || '').toLowerCase(),
      type: el.getAttribute('type') || '',
      placeholder: el.getAttribute('placeholder') || '',
      role: el.getAttribute('role') || '',
      name: el.getAttribute('name') || '',
      text: (el.innerText || el.textContent || '').trim().slice(0, 80),
      valueLen: (el.value || '').length,
      className: (el.className || '').toString().slice(0, 120),
      visible: r.width > 0 && r.height > 0,
    };
  }
  return out;
}"""


def is_ephemeral_ref(selector: str | None) -> bool:
    """快照 ref（``e12`` / ``f3e24``）等临时定位符判定，见 canonical 实现。"""
    from core.locator_candidates import is_ephemeral_locator_ref

    return is_ephemeral_locator_ref(selector)


_QUOTE_PAIRS = {"「": "」", "【": "】", "『": "』", "[": "]", "（": "）", "(": ")"}

_KEY_ALIASES = {
    "esc": "Escape", "escape": "Escape", "退出": "Escape", "退出键": "Escape",
    "enter": "Enter", "return": "Enter", "回车": "Enter", "回车键": "Enter",
    "space": "Space", "空格": "Space", "空格键": "Space",
    "tab": "Tab", "制表": "Tab", "制表键": "Tab",
    "delete": "Delete", "del": "Delete", "删除": "Delete", "删除键": "Delete",
    "backspace": "Backspace", "退格": "Backspace", "退格键": "Backspace",
    "上": "ArrowUp", "方向键上": "ArrowUp", "up": "ArrowUp", "arrowup": "ArrowUp",
    "下": "ArrowDown", "方向键下": "ArrowDown", "down": "ArrowDown", "arrowdown": "ArrowDown",
    "左": "ArrowLeft", "方向键左": "ArrowLeft", "left": "ArrowLeft", "arrowleft": "ArrowLeft",
    "右": "ArrowRight", "方向键右": "ArrowRight", "right": "ArrowRight",
    "arrowright": "ArrowRight",
}

_KEY_TOKEN_RE = re.compile(
    r"(?:按键|按|按下|快捷键)\s*[「【\"']?"
    r"([A-Za-z0-9+]+|回车键?|空格键?|制表键?|删除键?|退格键?|方向键[上下左右])"
    r"\s*[」】\"']?"
)


def normalize_key_name(raw: str | None) -> str | None:
    """按键名归一为 Playwright 规范名（ESC→Escape、回车→Enter、f5→F5）。"""
    s = clean_target_name(raw)
    if not s:
        return None
    low = s.lower()
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    if re.fullmatch(r"f\d{1,2}", low):
        return low.upper()
    if re.fullmatch(r"[a-z0-9]", low):
        return low.upper()
    if len(s) <= 20 and re.fullmatch(r"[A-Za-z0-9+\-]+", s):
        return s
    return None


def press_key_for_step(step: dict[str, Any] | None) -> str | None:
    """按键步骤 → 键名；非按键步骤一律返回 None（模板将回退 LLM 合成）。"""
    st = _structured(step)
    desc = _step_desc(step)
    if str(st.get("action") or "").lower() != "press_key" and not is_press_key_checklist_step(desc):
        return None
    raw = st.get("value") or st.get("target_name")
    key = normalize_key_name(str(raw) if raw is not None else None)
    if key:
        return key
    m = _KEY_TOKEN_RE.search(desc)
    if m:
        return normalize_key_name(m.group(1))
    return None


def clean_target_name(name: str | None) -> str:
    """剥离生成格式遗留的外层包裹符号（「Login」→ Login）；不动内含符号（删除【草稿】）。"""
    s = str(name or "").strip()
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for a, b in _QUOTE_PAIRS.items():
            if s.startswith(a) and s.endswith(b):
                s = s[1:-1].strip()
                changed = True
    return s


def _brackets(desc: str) -> list[str]:
    return [
        clean_target_name(m.group(1))
        for m in _BRACKET_RE.finditer(desc or "")
        if clean_target_name(m.group(1))
    ]


def _structured(step: dict[str, Any] | None) -> dict[str, Any]:
    if not step:
        return {}
    st = step.get("structured_step")
    return st if isinstance(st, dict) else {}


def _step_desc(step: dict[str, Any] | None) -> str:
    if not step:
        return ""
    return str(step.get("description") or step.get("original_description") or "").strip()


def looks_like_unit_filter_step(desc: str, st: dict[str, Any]) -> bool:
    blob = desc + " " + str(st.get("target_name") or "") + " " + str(st.get("value") or "")
    return bool(re.search(r"筛选|关键词|filter", blob))


def looks_like_unit_open_step(desc: str, st: dict[str, Any]) -> bool:
    blob = desc + " " + str(st.get("target_name") or "")
    return bool(re.search(r"请选择单位|单位选择|treeSelect|选择单位", blob))


def looks_like_unit_option_step(desc: str, st: dict[str, Any]) -> bool:
    if looks_like_unit_filter_step(desc, st) or looks_like_unit_open_step(desc, st):
        return False
    if is_select_checklist_step(desc):
        return True
    return bool(re.search(r"中选择|选中|选择【", desc))


def build_replay_from_step(
    step: dict[str, Any] | None,
    *,
    action: str,
    selector: str | None = None,
    value: str | None = None,
    dom_attrs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a durable replay dict from checklist step (+ optional DOM/codegen attrs)."""
    desc = _step_desc(step)
    st = _structured(step)
    action_l = (action or "").strip().lower()
    brackets = _brackets(desc)
    st_value = st.get("value")
    st_target = clean_target_name(st.get("target_name")) or None
    intent_value = (
        str(st_value).strip()
        if st_value is not None and str(st_value).strip()
        else None
    )
    final_value = intent_value or (
        str(value).strip() if value is not None and str(value).strip() else None
    )

    replay: dict[str, Any] = {
        "strategy": None,
        "placeholder": None,
        "exact_text": None,
        "role": None,
        "name": None,
        "css": None,
        "value": None,
        "js_click_text": None,
        "playwright_locator": None,
    }

    if is_press_key_checklist_step(desc) or action_l in (
        "press_key", "browser_press_key", "press",
    ):
        key = press_key_for_step(step) or normalize_key_name(
            value if isinstance(value, str) else None
        )
        if key:
            replay["strategy"] = "press_key"
            replay["value"] = key
            return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if is_close_messages_checklist_step(desc):
        replay["strategy"] = "close_overlays"
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if action_l in ("goto", "navigate", "browser_navigate") or re.match(
        r"^打开【|^打开\s|跳转到|导航到", desc
    ):
        replay["strategy"] = "goto"
        url = final_value or (st.get("selector") if isinstance(st.get("selector"), str) else None)
        if url and str(url).startswith("http"):
            replay["value"] = str(url)
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    # Open combobox / placeholder click (no project-specific CSS)
    if looks_like_unit_open_step(desc, st):
        replay["strategy"] = "click_placeholder"
        replay["placeholder"] = st_target or (brackets[0] if brackets else "请选择单位")
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if looks_like_unit_filter_step(desc, st) and (
        action_l in ("fill", "type", "browser_type") or is_fill_checklist_step(desc)
    ):
        replay["strategy"] = "fill_filter_press"
        replay["placeholder"] = st_target or (
            brackets[0] if brackets else "输入关键词进行筛选"
        )
        replay["value"] = final_value
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if looks_like_unit_option_step(desc, st):
        option = intent_value
        if not option and len(brackets) >= 2:
            option = brackets[-1]
        if not option and brackets:
            option = brackets[0]
        if not option:
            option = final_value
        replay["strategy"] = "click_text"
        replay["exact_text"] = option
        replay["value"] = option
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if is_fill_checklist_step(desc) or action_l in ("fill", "type", "browser_type"):
        ph = st_target or (brackets[0] if brackets else None)
        active = (dom_attrs or {}).get("active") if isinstance(dom_attrs, dict) else None
        if isinstance(active, dict) and active.get("placeholder"):
            ph = ph or active["placeholder"]
        replay["strategy"] = "fill_placeholder"
        replay["placeholder"] = ph
        replay["value"] = final_value
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if is_click_checklist_step(desc) or action_l in ("click", "browser_click"):
        label = st_target or (brackets[0] if brackets else None) or final_value
        if label and re.search(r"登录|提交|确定|确认|搜索|查询|保存|下一步", label):
            replay["strategy"] = "click_role"
            replay["role"] = "button"
            replay["name"] = label
            replay["exact_text"] = label
        elif label and re.search(r"请选择|请输入|placeholder", label):
            replay["strategy"] = "click_placeholder"
            replay["placeholder"] = label
        else:
            replay["strategy"] = "click_text"
            replay["exact_text"] = label
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    if action_l in ("evaluate", "browser_evaluate", "js", "eval"):
        replay["strategy"] = "evaluate_js"
        if final_value and len(final_value) < 2000:
            replay["value"] = final_value
        return {k: v for k, v in replay.items() if v is not None or k == "strategy"}

    active = (dom_attrs or {}).get("active") if isinstance(dom_attrs, dict) else None
    if isinstance(active, dict):
        if active.get("placeholder") and not replay.get("placeholder"):
            replay["placeholder"] = active["placeholder"]
        if active.get("text") and not replay.get("exact_text"):
            replay["exact_text"] = active["text"]

    if not replay.get("strategy"):
        replay["strategy"] = "click_text" if action_l.startswith("click") else "evaluate_js"
    return {k: v for k, v in replay.items() if v is not None or k == "strategy"}


def merge_dom_into_replay(
    replay: dict[str, Any] | None,
    dom_attrs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge DOM/codegen resolve payload into replay (codegen preferred)."""
    out = dict(replay or {})
    if not isinstance(dom_attrs, dict):
        return out
    # New path: codegen resolve payload
    if dom_attrs.get("playwright_locator") is not None or dom_attrs.get("ok") is not None:
        return merge_codegen_into_replay(out, dom_attrs)
    active = dom_attrs.get("active")
    if isinstance(active, dict):
        if active.get("placeholder") and not out.get("placeholder"):
            out["placeholder"] = active["placeholder"]
        if active.get("text") and not out.get("exact_text"):
            out.setdefault("exact_text", active["text"])
    return out
