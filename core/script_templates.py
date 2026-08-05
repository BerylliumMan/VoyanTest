# core/script_templates.py
"""Generic Playwright async snippets from journal.replay (no project UI templates)."""
from __future__ import annotations

import json
import re
from typing import Any

from core.goal_agent_loop import is_close_messages_checklist_step
from core.replay_resolve import build_replay_from_step

# Playwright auto-waits on actions; set_default_timeout is the global budget.
# Do NOT emit wait_for / wait_for_timeout (explicit waits).
DEFAULT_ACTION_TIMEOUT_MS = 30_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 60_000


def _esc(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _emit_default_timeouts() -> list[str]:
    return [
        f"    page.set_default_timeout({DEFAULT_ACTION_TIMEOUT_MS})",
        f"    page.set_default_navigation_timeout({DEFAULT_NAVIGATION_TIMEOUT_MS})",
        "",
    ]


def _step_meta(step: dict[str, Any], i: int) -> tuple[int, str, dict[str, Any]]:
    order = int(step.get("step_order") or step.get("step_number") or i + 1)
    desc = str(step.get("description") or step.get("original_description") or "").strip()
    st = step.get("structured_step") if isinstance(step.get("structured_step"), dict) else {}
    return order, desc, st


def _normalize_locator(loc: str | None) -> str | None:
    if not loc or not isinstance(loc, str):
        return None
    s = loc.strip()
    if s.startswith("page."):
        s = s[5:].strip()
    return s or None


def _emit_locator_action(
    locator: str,
    *,
    kind: str,
    value: str | None = None,
) -> list[str]:
    """Emit click/fill/press_sequentially against a codegen locator expression."""
    loc = _normalize_locator(locator)
    if not loc:
        return []
    # Harden duplicates: append .first when not already chained with first/nth
    if not re.search(r"\.(first|nth\(|last)(\(|$)", loc):
        expr = f"page.{loc}.first"
    else:
        expr = f"page.{loc}"
    lines: list[str] = []
    if kind == "fill":
        lines.append(f"    await {expr}.fill({_esc(value or '')})")
    elif kind == "press_sequentially":
        lines.append(f"    await {expr}.click()")
        lines.append(f"    await {expr}.fill('')")
        lines.append(f"    await {expr}.press_sequentially({_esc(value or '')}, delay=80)")
    else:
        lines.append(f"    await {expr}.click()")
    lines.append("")
    return lines


def _emit_fill_placeholder(placeholder: str, value: str) -> list[str]:
    return [
        f"    await page.get_by_placeholder({_esc(placeholder)}).first.fill({_esc(value)})",
        "",
    ]


def _emit_click_placeholder(placeholder: str) -> list[str]:
    lines = [
        f"    await page.get_by_placeholder({_esc(placeholder)}).first.click()",
    ]
    # Unit/org picker loads options async. Type-ahead before the list exists is
    # ignored after load — wait for at least one treeitem to be attached first
    # (Playwright expect auto-wait; not sleep / wait_for_timeout).
    if re.search(r"选择单位|选择组织|请选择", placeholder or ""):
        lines.append(
            "    await expect(page.get_by_role('treeitem').first).to_be_attached()"
        )
    lines.append("")
    return lines


def _emit_filter_press(placeholder: str, value: str) -> list[str]:
    """Type into a dropdown filter after the option list is ready."""
    return [
        f"    filt = page.get_by_placeholder({_esc(placeholder)}).filter(visible=True).last",
        "    await filt.click()",
        "    await filt.fill('')",
        f"    await filt.press_sequentially({_esc(value)}, delay=80)",
        "",
    ]


def _emit_click_text(text: str) -> list[str]:
    # Tree options often expose accessible name via role=treeitem (title/label).
    return [
        f"    await page.get_by_role('treeitem', name={_esc(text)}).filter(visible=True).first.click()",
        "",
    ]


def _emit_click_button(name: str) -> list[str]:
    return [
        f"    await page.get_by_role('button', name={_esc(name)}).first.click()",
        "",
    ]


def _emit_close_overlays() -> list[str]:
    """Generic overlay close (Element-ish dialogs + notifications)."""
    return [
        "    # Close visible dialogs / notifications (short timeouts; skip if unstable)",
        "    for _ in range(5):",
        "        wrappers = page.locator('.el-dialog__wrapper:visible, [role=\"dialog\"]:visible')",
        "        if await wrappers.count() == 0:",
        "            break",
        "        footer = wrappers.first.get_by_role('button', name=re.compile(r'关\\s*闭'))",
        "        header = wrappers.first.locator('.el-dialog__headerbtn, [aria-label=\"Close\"]')",
        "        try:",
        "            if await footer.count() and await footer.first.is_visible():",
        "                await footer.first.click(timeout=3000)",
        "            elif await header.count() and await header.first.is_visible():",
        "                await header.first.click(timeout=3000)",
        "            else:",
        "                break",
        "        except Exception:",
        "            break",
        "    noti = page.locator('.el-notification:visible').first",
        "    if await noti.count() and await noti.is_visible():",
        "        btn = noti.locator('.el-notification__closeBtn')",
        "        try:",
        "            if await btn.count() and await btn.is_visible():",
        "                await btn.first.click(timeout=2000)",
        "            else:",
        "                await noti.evaluate('el => el.remove()')",
        "        except Exception:",
        "            await noti.evaluate('el => el.remove()')",
        "",
    ]


def _emit_goto(url: str) -> list[str]:
    return [
        f"    await page.goto({_esc(url)}, wait_until='domcontentloaded')",
        "",
    ]


def _action_kind(strategy: str, st: dict[str, Any], desc: str) -> str:
    if strategy in ("fill_placeholder", "fill_filter_press"):
        return "press_sequentially" if strategy == "fill_filter_press" else "fill"
    if st.get("action") == "fill" or re.search(r"输入|填写|fill", desc):
        if re.search(r"筛选|关键词", desc):
            return "press_sequentially"
        return "fill"
    return "click"


def try_build_templated_script(
    *,
    case_id: int,
    steps: list[dict[str, Any]] | None,
    base_url: str | None = None,
    journal: list[dict[str, Any]] | None = None,
) -> str | None:
    """Build async Playwright script from per-step replay locators.

    Returns None if any checklist step cannot be expressed (falls back to LLM).
    """
    steps = list(steps or [])
    if not steps:
        return None

    lines: list[str] = [
        "import re",
        "from playwright.async_api import expect",
        "",
        f"async def test_case_{int(case_id)}(page) -> None:",
    ]
    lines.extend(_emit_default_timeouts())

    base = (base_url or "").strip()
    # Login pages often live at site origin; /xtmh is post-login app path.
    nav = base
    if base:
        blob0 = " ".join(
            str(s.get("description") or "")
            + " "
            + str((s.get("structured_step") or {}).get("target_name") or "")
            for s in steps[:3]
        )
        if re.search(r"请选择单位|请输入用户名|请输入密码", blob0):
            m = re.match(r"(https?://[^/]+)/?", base)
            if m:
                nav = m.group(1) + "/"
    emitted_goto = False
    if nav:
        lines.extend(_emit_goto(nav))
        emitted_goto = True

    replay_by_order: dict[int, dict[str, Any]] = {}
    for e in journal or []:
        idx = e.get("checklist_index")
        if idx is None or not e.get("success"):
            continue
        rp = e.get("replay")
        if isinstance(rp, dict) and (rp.get("strategy") or rp.get("playwright_locator")):
            replay_by_order[int(idx)] = rp

    covered_orders: set[int] = set()
    unknown = False

    for i, step in enumerate(steps):
        order, desc, st = _step_meta(step, i)
        if order in covered_orders:
            continue

        rp = replay_by_order.get(order) or build_replay_from_step(
            step, action="", selector=None, value=None
        )
        strategy = (rp.get("strategy") or "").strip()
        loc = _normalize_locator(rp.get("playwright_locator"))

        if strategy == "goto":
            url = rp.get("value") or base
            if url and not (
                emitted_goto and base and str(url).rstrip("/") == base.rstrip("/")
            ):
                if emitted_goto and base and "xtmh" in str(url) and "xtmh" in base:
                    covered_orders.add(order)
                    continue
                lines.extend(_emit_goto(str(url)))
                emitted_goto = True
            covered_orders.add(order)
            continue

        if strategy == "close_overlays" or is_close_messages_checklist_step(desc):
            lines.extend(_emit_close_overlays())
            covered_orders.add(order)
            continue

        kind = _action_kind(strategy, st, desc)
        val = rp.get("value")
        if val is None and st.get("value") is not None:
            val = st.get("value")
        if kind in ("fill", "press_sequentially") and val is None:
            unknown = True
            break

        if loc:
            lines.extend(
                _emit_locator_action(loc, kind=kind, value=None if kind == "click" else str(val))
            )
            if strategy == "click_role" and re.search(
                r"登录", str(rp.get("name") or rp.get("exact_text") or "")
            ):
                if base and "xtmh" in base:
                    lines.append(
                        "    await expect(page).to_have_url(re.compile(r'.*xtmh.*'))"
                    )
                lines.append("")
            covered_orders.add(order)
            continue

        # Fallback without codegen locator (generic get_by_*)
        if strategy in ("fill_placeholder", "fill_filter_press"):
            ph = rp.get("placeholder") or st.get("target_name")
            if not ph or val is None:
                unknown = True
                break
            if strategy == "fill_filter_press":
                lines.extend(_emit_filter_press(str(ph), str(val)))
            else:
                lines.extend(_emit_fill_placeholder(str(ph), str(val)))
            covered_orders.add(order)
            continue

        if strategy == "click_placeholder":
            ph = rp.get("placeholder") or st.get("target_name")
            if not ph:
                unknown = True
                break
            lines.extend(_emit_click_placeholder(str(ph)))
            covered_orders.add(order)
            continue

        if strategy == "click_role":
            name = rp.get("name") or rp.get("exact_text")
            if not name:
                unknown = True
                break
            lines.extend(_emit_click_button(str(name)))
            if re.search(r"登录", str(name)):
                if base and "xtmh" in base:
                    lines.append(
                        "    await expect(page).to_have_url(re.compile(r'.*xtmh.*'))"
                    )
                lines.append("")
            covered_orders.add(order)
            continue

        if strategy == "click_text":
            text = rp.get("exact_text") or rp.get("value")
            if not text:
                unknown = True
                break
            lines.extend(_emit_click_text(str(text)))
            covered_orders.add(order)
            continue

        if st.get("action") == "fill" and st.get("target_name") and st.get("value") is not None:
            lines.extend(
                _emit_fill_placeholder(str(st["target_name"]), str(st["value"]))
            )
            covered_orders.add(order)
            continue
        if st.get("action") == "click" and st.get("target_name"):
            tn = str(st["target_name"])
            if re.search(r"登录|提交|确定", tn):
                lines.extend(_emit_click_button(tn))
            else:
                lines.extend(_emit_click_text(tn))
            covered_orders.add(order)
            continue

        unknown = True
        break

    all_orders = {
        int(s.get("step_order") or s.get("step_number") or i + 1)
        for i, s in enumerate(steps)
    }
    if unknown or covered_orders != all_orders:
        return None

    lines.append("    # done")
    lines.append("")
    return "\n".join(lines)
