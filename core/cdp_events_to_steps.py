# core/cdp_events_to_steps.py
"""Deterministic CDP recorded events → UI StructuredStep list.

Recording convert must produce Playwright/AI-executable steps (action /
target_name / value / selector), not free-form manual prose. When the event
carries a recorded selector, it is solidified into ``structured_step.selector``.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from core.step_normalize import render_structured_step

_HAS_TEXT_RE = re.compile(
    r"""(?:has-text|text)\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.I,
)
_PLACEHOLDER_ATTR_RE = re.compile(
    r"""placeholder\s*=\s*['"]([^'"]+)['"]""",
    re.I,
)


def _event_type(ev: dict) -> str:
    return str(ev.get("event_type") or ev.get("type") or "").strip().lower()


def _opt_str(*values: Any) -> str | None:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _css_attr_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _placeholder_of(ev: dict | None, selector: str | None = None) -> str | None:
    """Visible placeholder from event metadata or recorded selector attribute."""
    ev = ev or {}
    ph = _opt_str(ev.get("placeholder"))
    if ph:
        return ph
    sel = selector if selector is not None else ev.get("selector")
    if not sel:
        return None
    m = _PLACEHOLDER_ATTR_RE.search(str(sel))
    return m.group(1).strip() if m else None


def _tag_of(ev: dict | None) -> str:
    raw = _opt_str((ev or {}).get("tag")) or "input"
    tag = raw.lower()
    if tag in ("input", "textarea", "button", "select", "a", "div", "span"):
        return tag
    return "input"


def _role_of(ev: dict | None) -> str | None:
    return _opt_str((ev or {}).get("role"), (ev or {}).get("target_role"))


def _visible_label(ev: dict | None, selector: str | None = None) -> str | None:
    """Best human-visible label: placeholder > aria-name > short text."""
    ev = ev or {}
    ph = _placeholder_of(ev, selector)
    if ph:
        return ph
    name = _opt_str(ev.get("name"), ev.get("aria_label"), ev.get("aria-label"))
    if name:
        return name
    text = _opt_str(ev.get("text"))
    if text and len(text) <= 40 and "\n" not in text:
        return text
    return None


def _placeholder_selector(placeholder: str, tag: str = "input") -> str:
    t = (tag or "input").lower()
    if t not in ("input", "textarea"):
        t = "input"
    return f'{t}[placeholder="{_css_attr_escape(placeholder)}"]'


def _expand_field_placeholder(label: str) -> str:
    """Map short field labels (单位) to full placeholder copy (请选择单位)."""
    s = (label or "").strip()
    if not s:
        return s
    if s.startswith("请选择") or s.startswith("请输入"):
        return s
    # Common Ant / Element select trigger copy
    return f"请选择{s}"


def _unit_field_label(ev: dict | None, selector: str | None = None) -> str:
    """Label for the login-page unit picker control.

    Prefer recorded placeholder / selector attribute. Legacy recordings only
    have bare ``input`` — fall back to the product placeholder「请选择单位」.
    """
    label = _visible_label(ev, selector)
    if label:
        return _expand_field_placeholder(label) if "单位" in label else label
    sel = (selector or "").strip().lower()
    # Bare / weak input click at start of login flow
    if sel in ("input",) or sel.startswith("input:") or sel == "input[type=\"text\"]":
        return "请选择单位"
    return "请选择单位"


def _selector_of(ev: dict | None, selector: str | None = None) -> str | None:
    """Normalize recorded Playwright/CSS selector for StructuredStep solidification.

    Bare tags (``input``) and ephemeral ``#el-popover-N`` ids are dropped — they
    fail Playwright strict mode or break across sessions. Prefer placeholder CSS
    built from event metadata / target label instead.
    """
    raw = selector if selector is not None else (ev or {}).get("selector")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        from core.step_intent import is_usable_solidified_selector
        if not is_usable_solidified_selector(s):
            return None
    except Exception:
        # Keep original during import cycles / unit isolation
        if s.lower() in ("input", "button", "div", "span", "a", "select"):
            return None
    return s


def _solidify_selector(
    ev: dict | None,
    selector: str | None,
    *,
    label: str | None = None,
    prefer_placeholder: bool = False,
) -> str | None:
    """Usable recorded selector, or build ``tag[placeholder=\"…\"]`` from label."""
    if not prefer_placeholder:
        sel = _selector_of(ev, selector)
        if sel:
            return sel
    ph = _placeholder_of(ev, selector) or label
    if ph:
        ph = _expand_field_placeholder(ph) if (
            "单位" in ph and not ph.startswith("请")
        ) else ph
        built = _placeholder_selector(ph, _tag_of(ev))
        try:
            from core.step_intent import is_usable_solidified_selector
            if is_usable_solidified_selector(built):
                return built
        except Exception:
            return built
        return built
    return _selector_of(ev, selector)


def _extract_has_text(selector: str | None) -> str | None:
    if not selector:
        return None
    m = _HAS_TEXT_RE.search(selector)
    return m.group(1).strip() if m else None


def _looks_like_password(selector: str | None, ev: dict | None = None) -> bool:
    s = (selector or "").lower()
    if "password" in s or "passwd" in s or "ant-input-password" in s:
        return True
    typ = _opt_str((ev or {}).get("type")) or ""
    if typ.lower() == "password":
        return True
    ph = (_placeholder_of(ev, selector) or "").lower()
    return "密码" in ph or "password" in ph


def _looks_like_username(selector: str | None, ev: dict | None = None) -> bool:
    s = (selector or "").lower()
    if "user" in s or "account" in s or "login-name" in s:
        return True
    ph = (_placeholder_of(ev, selector) or "").lower()
    return any(k in ph for k in ("用户", "账号", "帐号", "user", "account"))


def _looks_like_unit_filter(selector: str | None, ev: dict | None = None) -> bool:
    """True only for the unit-picker filter / trigger — not username/password."""
    if _looks_like_password(selector, ev) or _looks_like_username(selector, ev):
        return False
    ph = _placeholder_of(ev, selector) or ""
    # Element tree-select filter box
    if "关键词" in ph or "筛选" in ph or "搜索" in ph:
        return True
    if "单位" in ph:
        return True
    s = (selector or "").lower()
    if any(
        x in s
        for x in (
            "popover", "el-select", "rc-select", "tree-select",
            "ant-select", "dropdown",
        )
    ):
        return True
    return False


def _auth_field_name(selector: str | None, ev: dict | None, auth_fills: int) -> str:
    """Name auth fields by placeholder/type first — never let fill order override."""
    ph = _placeholder_of(ev, selector) or ""
    if _looks_like_password(selector, ev) or "密码" in ph or "password" in ph.lower():
        return ph if ph else "密码"
    if _looks_like_username(selector, ev) or any(
        k in ph for k in ("用户", "账号", "帐号", "user", "account")
    ):
        return ph if ph else "用户名"
    if ph.startswith("请输入"):
        return ph
    # Fallback only when placeholder/type unknown
    if auth_fills >= 1:
        return "密码"
    return "用户名"


def _is_chrome_click(selector: str | None, has_text: str | None) -> bool:
    """Clicks on popover chrome / anonymous divs — skip."""
    if has_text:
        return False
    s = (selector or "").strip().lower()
    if not s or s == "input" or "placeholder=" in s:
        return False
    if s == "button" or s.endswith(" button"):
        return True  # bare button without label — usually noise
    if "popover" in s or "dropdown" in s or "min_width" in s:
        return True
    if s.startswith("#el-") or "nth-of-type" in s:
        # structural click without accessible name
        return True
    return False


def _is_unit_trigger_click(ev: dict | None, selector: str | None) -> bool:
    """True when this click opens the unit (组织) picker."""
    ph = _placeholder_of(ev, selector) or ""
    if "单位" in ph:
        return True
    sel = (selector or "").strip().lower()
    if sel == "input" or (sel.startswith("input[placeholder=") and "单位" in sel):
        return True
    return False


def _collapse_events(events: list[dict]) -> list[dict]:
    """Merge consecutive input events on the same selector (keep last value).

    Only merge when values look like progressive typing (prefix / same).
    Distinct values on a weak selector (e.g. bare ``input`` for username then
    password) must remain separate steps.
    """
    out: list[dict] = []
    for raw in events:
        ev = dict(raw or {})
        et = _event_type(ev)
        if et in ("input", "type", "fill", "change") and out:
            prev = out[-1]
            if _event_type(prev) in ("input", "type", "fill", "change") and (
                (prev.get("selector") or "") == (ev.get("selector") or "")
            ):
                prev_v = str(prev.get("value") or "")
                cur_v = str(ev.get("value") or "")
                progressive = (
                    cur_v == prev_v
                    or (prev_v and cur_v.startswith(prev_v))
                    or (cur_v and prev_v.startswith(cur_v))
                )
                if progressive:
                    out[-1] = ev
                    continue
        if et in ("navigation", "navigate"):
            url = (ev.get("url") or ev.get("value") or "").strip()
            if not url:
                continue
            if out and _event_type(out[-1]) in ("navigation", "navigate"):
                prev_url = (out[-1].get("url") or out[-1].get("value") or "").strip()
                if prev_url == url:
                    continue
        out.append(ev)
    return out


def _goto_target(url: str) -> str:
    try:
        path = urlparse(url).path or "/"
    except Exception:
        path = url
    path = path.strip("/") or "首页"
    seg = path.split("/")[-1] if path else "首页"
    return seg or url


def _expected_for(action: str, target: str | None = None) -> str:
    if action == "goto":
        return "页面加载完成"
    if action == "fill":
        return "输入成功"
    if action == "select":
        return "选项已选中"
    if action == "click" and target and any(
        k in (target or "") for k in ("登录", "提交", "确定", "保存")
    ):
        return "操作成功或页面跳转"
    if action == "wait":
        return "目标文案已出现"
    return "步骤执行成功"


def _pack(structured: dict[str, Any], expected: str | None = None) -> dict[str, Any]:
    desc = render_structured_step(structured) or structured.get("target_name") or structured.get("action") or ""
    return {
        "step_description": desc,
        "expected_result": expected or _expected_for(
            str(structured.get("action") or ""),
            structured.get("target_name"),
        ),
        "structured_step": {
            k: v
            for k, v in structured.items()
            if v is not None and v != ""
        },
        "action": structured.get("action"),
        "target_name": structured.get("target_name"),
        "target_role": structured.get("target_role"),
        "value": structured.get("value"),
        "selector": structured.get("selector"),
    }


def events_to_structured_steps(events: list[dict]) -> list[dict]:
    """Convert recorded CDP events into UI-executable step dicts."""
    if not events:
        return []

    collapsed = _collapse_events(events)
    steps: list[dict] = []
    saw_goto = False
    # unit_open → filtering unit dropdown; authed_fields → username/password fills
    phase = "start"
    auth_fills = 0
    unit_label = "请选择单位"

    for ev in collapsed:
        et = _event_type(ev)
        selector = ev.get("selector") or ""
        value = ev.get("value")
        url = (ev.get("url") or "").strip()
        has_text = _extract_has_text(selector)

        if et in ("navigation", "navigate"):
            nav_url = (ev.get("value") or url or "").strip()
            if not nav_url:
                continue
            if not saw_goto:
                steps.append(
                    _pack(
                        {
                            "action": "goto",
                            "target_name": _goto_target(nav_url),
                            "value": nav_url,
                        }
                    )
                )
                saw_goto = True
            else:
                label = _goto_target(nav_url)
                steps.append(
                    _pack(
                        {
                            "action": "wait",
                            "target_name": label,
                            "value": label,
                        },
                        expected="页面跳转完成",
                    )
                )
                phase = "after_nav"
            continue

        if et in ("input", "type", "fill", "change"):
            text = "" if value is None else str(value)
            # Drop IME pinyin intermediate values (jing'zhou'shi'yuan)
            if re.fullmatch(r"[a-z]+('[a-z]+)+", text.strip(), flags=re.I):
                continue
            if not text.strip():
                continue

            # 仅当事件本身是单位筛选/触发器时走单位逻辑。
            # 禁止用 phase==unit_open 把后续用户名/密码也标成「请选择单位」。
            if _looks_like_unit_filter(selector, ev):
                phase = "unit_open"
                filter_ph = _placeholder_of(ev, selector)
                fill_label = filter_ph or unit_label or _unit_field_label(ev, selector)
                step = _pack(
                    {
                        "action": "fill",
                        "target_name": fill_label,
                        "target_role": "textbox",
                        "value": text,
                        "selector": _solidify_selector(
                            ev, selector, label=fill_label, prefer_placeholder=True,
                        ),
                    }
                )
                if (
                    steps
                    and steps[-1].get("action") == "fill"
                    and steps[-1].get("value") == text
                    and steps[-1].get("selector") == step.get("selector")
                ):
                    continue
                steps.append(step)
                continue

            # 选项点击可能漏录：离开单位阶段，进入账号填写
            if phase == "unit_open":
                phase = "unit_done"

            name = _auth_field_name(selector, ev, auth_fills)
            auth_fills += 1
            phase = "auth"
            steps.append(
                _pack(
                    {
                        "action": "fill",
                        "target_name": name,
                        "target_role": "textbox",
                        "value": text,
                        "selector": _solidify_selector(
                            ev, selector, label=name, prefer_placeholder=True,
                        ) or _selector_of(ev, selector),
                    }
                )
            )
            continue

        if et == "click":
            if not has_text:
                has_text = _opt_str(ev.get("text"))
                if has_text and len(has_text) > 40:
                    has_text = None
            if has_text:
                # Unit tree option (after filter) vs normal button
                if phase in ("unit_open", "start") and (
                    "院" in has_text or "省" in has_text or "市" in has_text
                    or "单位" in has_text
                ):
                    steps.append(
                        _pack(
                            {
                                "action": "select",
                                "target_name": has_text,
                                "target_role": "option",
                                "value": has_text,
                                "selector": _selector_of(ev, selector),
                            }
                        )
                    )
                    phase = "unit_done"
                    continue
                role = _role_of(ev) or "button"
                steps.append(
                    _pack(
                        {
                            "action": "click",
                            "target_name": has_text,
                            "target_role": role if role in ("button", "link", "menuitem") else "button",
                            "selector": _selector_of(ev, selector),
                        }
                    )
                )
                if any(k in has_text for k in ("登录", "提交")):
                    phase = "submitted"
                continue

            if _is_chrome_click(selector, has_text):
                # Opening unit picker: bare `input` or placeholder-bearing trigger
                if phase == "start" and _is_unit_trigger_click(ev, selector):
                    unit_label = _unit_field_label(ev, selector)
                    # Ant Design / Element select trigger is AX textbox, not combobox
                    role = _role_of(ev) or "textbox"
                    if role == "combobox":
                        role = "textbox"
                    steps.append(
                        _pack(
                            {
                                "action": "click",
                                "target_name": unit_label,
                                "target_role": role,
                                "selector": _solidify_selector(
                                    ev, selector, label=unit_label, prefer_placeholder=True,
                                ),
                            }
                        )
                    )
                    phase = "unit_open"
                # else skip chrome / anonymous buttons
                continue

            # Bare / field input click (incl. placeholder-solidified selectors)
            sel_l = (selector or "").lower()
            if "input" in sel_l or _placeholder_of(ev, selector):
                if phase == "start" and _is_unit_trigger_click(ev, selector):
                    unit_label = _unit_field_label(ev, selector)
                    role = _role_of(ev) or "textbox"
                    if role == "combobox":
                        role = "textbox"
                    steps.append(
                        _pack(
                            {
                                "action": "click",
                                "target_name": unit_label,
                                "target_role": role,
                                "selector": _solidify_selector(
                                    ev, selector, label=unit_label, prefer_placeholder=True,
                                ),
                            }
                        )
                    )
                    phase = "unit_open"
                    continue
                # Focus username/password before typing — fill follows, skip
                if phase in ("unit_done", "auth"):
                    continue
            continue

        continue

    cleaned: list[dict] = []
    for st in steps:
        if (
            cleaned
            and cleaned[-1].get("action") == st.get("action") == "wait"
            and cleaned[-1].get("target_name") == st.get("target_name")
        ):
            continue
        # Drop trailing anonymous button clicks after navigation wait
        if (
            cleaned
            and cleaned[-1].get("action") == "wait"
            and st.get("action") == "click"
            and st.get("target_name") in ("按钮", "控件")
        ):
            continue
        cleaned.append(st)
    return cleaned
