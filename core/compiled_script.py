# core/compiled_script.py
"""Build / hash / validate whole-case Playwright Python scripts.

After a successful UI run, VoyanTest solidifies the locators actually used into
one async Playwright script. Editing case steps clears the script.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(
    r"""placeholder\s*=\s*['"]([^'"]+)['"]""",
    re.I,
)
_HAS_TEXT_RE = re.compile(
    r"""(?:has-text|text)\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.I,
)
_EPHEMERAL_ID_RE = re.compile(
    r"#el-(?:popover|popper|tooltip|message|notification|dialog|drawer|select|dropdown)-\d+"
    r"|#el-[a-z]+-\d+",
    re.I,
)
_AX_REF_RE = re.compile(r"^[a-zA-Z]*\d*e\d+$")
_BARE_TAG_RE = re.compile(r"^(input|button|div|span|a|select|textarea)$", re.I)


def steps_content_hash(steps: list[dict[str, Any]] | list[Any]) -> str:
    """Stable fingerprint of step content (order, description, structured_step)."""
    payload: list[dict[str, Any]] = []
    for s in steps or []:
        if hasattr(s, "step_order"):
            payload.append(
                {
                    "order": int(getattr(s, "step_order", 0) or 0),
                    "description": str(getattr(s, "description", "") or ""),
                    "structured_step": getattr(s, "structured_step", None),
                    "parsed_result": getattr(s, "parsed_result", None),
                }
            )
        elif isinstance(s, dict):
            payload.append(
                {
                    "order": int(s.get("step_order") or s.get("step_number") or 0),
                    "description": str(s.get("description") or s.get("original_description") or ""),
                    "structured_step": s.get("structured_step"),
                    "parsed_result": s.get("parsed_result") or s.get("expected_result"),
                }
            )
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_usable_script_locator(selector: str | None) -> bool:
    """Reject ephemeral refs, bare tags, and Element UI dynamic ids."""
    if not selector:
        return False
    s = str(selector).strip()
    if not s:
        return False
    if _AX_REF_RE.fullmatch(s):
        return False
    if _BARE_TAG_RE.fullmatch(s):
        return False
    if _EPHEMERAL_ID_RE.search(s):
        return False
    try:
        from core.step_intent import is_usable_solidified_selector
        return bool(is_usable_solidified_selector(s))
    except Exception:
        return any(ch in s for ch in ("#", ".", "[", ">", "=", '"', "'")) or "has-text" in s.lower()


def _py_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _locator_expr(selector: str, *, role: str | None = None, name: str | None = None) -> str:
    """Map a solidified selector to a Playwright Python locator expression on ``page``."""
    s = (selector or "").strip()
    ph = None
    m = _PLACEHOLDER_RE.search(s)
    if m:
        ph = m.group(1)
    if ph:
        return f"page.get_by_placeholder({_py_str(ph)})"
    ht = None
    m2 = _HAS_TEXT_RE.search(s)
    if m2:
        ht = m2.group(1)
    if ht and s.lower().startswith("button"):
        return f"page.get_by_role('button', name={_py_str(ht)})"
    if ht:
        return f"page.get_by_text({_py_str(ht)}, exact=True)"
    if role and name:
        return f"page.get_by_role({_py_str(role)}, name={_py_str(name)})"
    if name and (role or "").lower() in ("textbox", "combobox", "searchbox"):
        return f"page.get_by_placeholder({_py_str(name)})"
    return f"page.locator({_py_str(s)})"


def _pick_selector_for_step(
    step: dict[str, Any],
    result: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    """Return (selector, role, name) for script generation."""
    structured = step.get("structured_step") if isinstance(step.get("structured_step"), dict) else {}
    role = structured.get("target_role") if isinstance(structured, dict) else None
    name = structured.get("target_name") if isinstance(structured, dict) else None

    candidates: list[str] = []
    if isinstance(result, dict):
        for key in ("resolved_selector", "selector", "action_selector"):
            v = result.get(key)
            if v:
                candidates.append(str(v))
        ll = result.get("learned_locator")
        if isinstance(ll, dict):
            if ll.get("name"):
                name = name or ll.get("name")
            if ll.get("role"):
                role = role or ll.get("role")
            # plan blob
            plan = ll.get("plan") if isinstance(ll.get("plan"), list) else None
            if plan:
                last = plan[-1] if plan else {}
                if isinstance(last, dict):
                    name = name or last.get("name")
                    role = role or last.get("role")
    if isinstance(structured, dict) and structured.get("selector"):
        candidates.append(str(structured["selector"]))

    for c in candidates:
        if is_usable_script_locator(c):
            return c, (str(role) if role else None), (str(name) if name else None)

    # Synthesize from placeholder-like name
    if name and str(name).startswith(("请选择", "请输入", "输入")):
        return f'input[placeholder="{name}"]', role, name
    if name and role in ("button", "link"):
        return f'{role}:has-text("{name}")', role, name
    if name:
        return f'text={name}', role, name
    return None, (str(role) if role else None), (str(name) if name else None)


def build_script_from_run(
    *,
    case_id: int,
    case_name: str,
    steps: list[dict[str, Any]],
    step_results: list[dict[str, Any]],
    base_url: str | None = None,
    steps_hash: str | None = None,
) -> str | None:
    """DEPRECATED template locator→script. Prefer script_synthesize from journal.

    Returns None if not enough stable locators were captured.
    """
    by_num = {
        int(r.get("step_number") or r.get("step_order") or 0): r
        for r in (step_results or [])
        if isinstance(r, dict)
    }
    lines: list[str] = []
    usable_steps = 0
    for step in sorted(steps or [], key=lambda s: int(s.get("step_order") or 0)):
        order = int(step.get("step_order") or 0)
        result = by_num.get(order) or {}
        if not result.get("success"):
            continue
        structured = step.get("structured_step") if isinstance(step.get("structured_step"), dict) else {}
        action = (structured.get("action") if structured else None) or ""
        value = structured.get("value") if structured else None
        if not action and isinstance(result.get("action"), str):
            # result.action may be free text; prefer structured
            pass
        action = str(action or "click").lower().strip()
        desc = step.get("description") or result.get("original_description") or f"step {order}"

        if action in ("goto", "navigate"):
            url = value or base_url or ""
            if not url:
                continue
            lines.append(f"    # {order}. {desc}")
            lines.append(f"    await page.goto({_py_str(str(url))}, wait_until='domcontentloaded')")
            lines.append("    await page.wait_for_timeout(500)")
            usable_steps += 1
            continue

        if action == "wait":
            target = value or (structured.get("target_name") if structured else None) or ""
            if target:
                lines.append(f"    # {order}. {desc}")
                lines.append(f"    await page.get_by_text({_py_str(str(target))}).first.wait_for(state='visible', timeout=15000)")
                usable_steps += 1
            continue

        sel, role, name = _pick_selector_for_step(step, result if isinstance(result, dict) else None)
        if not sel and not name:
            logger.info("compiled_script skip step %s — no stable locator", order)
            continue
        loc = _locator_expr(sel or "", role=role, name=name)
        lines.append(f"    # {order}. {desc}")
        if action in ("fill", "type", "input", "change"):
            lines.append(f"    await {loc}.first.fill({_py_str('' if value is None else str(value))})")
        elif action in ("select",):
            # select option by text
            opt = value or name or ""
            lines.append(f"    await page.get_by_text({_py_str(str(opt))}, exact=True).first.click()")
        else:
            lines.append(f"    await {loc}.first.click()")
        lines.append("    await page.wait_for_timeout(300)")
        usable_steps += 1

    if usable_steps < 1 or not lines:
        return None

    h = steps_hash or steps_content_hash(steps)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = (base_url or "").strip()
    header = f'''# Auto-generated by VoyanTest — do not edit by hand
# case_id={case_id}
# case_name={_py_str(case_name)}
# generated_at={ts}
# steps_hash={h}
# base_url={_py_str(base)}
#
# Cleared automatically when case steps are modified.

from __future__ import annotations

import asyncio
import sys


async def test_case_{int(case_id)}(page) -> None:
    """Solidified replay for case {int(case_id)}: {case_name}"""
'''
    body = "\n".join(lines) + "\n"
    footer = f'''

async def _main() -> None:
    from playwright.async_api import async_playwright

    base_url = {_py_str(base)}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        if base_url:
            await page.goto(base_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
        await test_case_{int(case_id)}(page)
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as exc:
        print(f"COMPILED_SCRIPT_FAILED: {{exc}}", file=sys.stderr)
        raise
'''
    return header + body + footer


def persist_compiled_script(
    case,
    *,
    script: str,
    steps_hash: str,
) -> None:
    """Write script fields onto ORM TestCase (caller commits)."""
    case.compiled_script = script
    case.compiled_script_hash = steps_hash
    case.compiled_at = datetime.now(timezone.utc)


def clear_compiled_script(case) -> bool:
    """Clear solidified script fields. Returns True if anything changed."""
    changed = bool(
        getattr(case, "compiled_script", None)
        or getattr(case, "compiled_script_hash", None)
        or getattr(case, "compiled_at", None)
    )
    case.compiled_script = None
    case.compiled_script_hash = None
    case.compiled_at = None
    return changed


def persist_compiled_script_after_run(
    case,
    *,
    orm_steps: list,
    step_results: list[dict],
    base_url: str | None = None,
) -> str | None:
    """Build+attach compiled script on ORM case. Returns hash if persisted, else None.

    Caller must commit the session.
    """
    if not case or (getattr(case, "case_kind", None) or "") != "ui":
        return None
    if not (step_results and all(r.get("success") for r in step_results)):
        return None
    step_dicts = []
    for s in orm_steps:
        d = {
            "id": getattr(s, "id", None),
            "step_order": getattr(s, "step_order", 0),
            "description": getattr(s, "description", "") or "",
            "parsed_result": getattr(s, "parsed_result", None),
            "structured_step": dict(s.structured_step)
            if isinstance(getattr(s, "structured_step", None), dict)
            else {},
        }
        rr = next(
            (r for r in step_results if r.get("step_number") == d["step_order"]),
            None,
        )
        if rr and rr.get("resolved_selector") and isinstance(d["structured_step"], dict):
            if not d["structured_step"].get("selector"):
                d["structured_step"]["selector"] = rr["resolved_selector"]
        step_dicts.append(d)
    h = steps_content_hash(step_dicts)
    script = build_script_from_run(
        case_id=int(getattr(case, "id", 0) or 0),
        case_name=getattr(case, "name", None) or f"Case {getattr(case, 'id', '')}",
        steps=step_dicts,
        step_results=step_results,
        base_url=base_url,
        steps_hash=h,
    )
    if not script:
        return None
    persist_compiled_script(case, script=script, steps_hash=h)
    return h

