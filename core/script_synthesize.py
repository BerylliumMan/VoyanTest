# core/script_synthesize.py
"""LLM synthesis of Playwright Python from an NL-goal action journal."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from core.goal_agent_loop import REPAIR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:python)?\s*|\s*```$", re.I | re.M)

SYNTH_SYSTEM = """You write a durable Playwright async Python script from a successful browser action journal.

Requirements:
1. Output ONLY Python source — no markdown fences, no commentary.
2. Define exactly: async def test_case_{case_id}(page) -> None:
3. Prefer get_by_placeholder / get_by_role / get_by_text(exact) / locator with stable CSS.
4. NEVER use ephemeral accessibility refs (e12, e15, …).
5. ALWAYS disambiguate duplicate placeholders (Ant/Element often keep visible+hidden twins).
   Prefer CSS :visible / :not([disabled]):visible, then .first — e.g.
   page.locator('input[placeholder="请选择单位"]:visible').first.click()
   page.locator('input[placeholder="输入关键词进行筛选"]:not([disabled]):visible').first.press_sequentially(...)
   Never bare get_by_placeholder(...).click() (strict mode). Bare .first alone may hit a hidden/disabled twin.
6. For Element UI / Ant Design unit/tree selects:
   - open the visible combobox/placeholder
   - type into the enabled+visible filter input (prefer press_sequentially)
   - click the tree/list label with EXACT text match (not a longer similar name)
7. For closing homepage dialogs/notifications (Element UI, Cursor-proven):
   - loop .el-dialog__wrapper:visible → click footer button matching /关\\s*闭/ else .el-dialog__headerbtn
   - then .el-notification:visible → click .el-notification__closeBtn (or remove node)
   - NEVER click 消息铃铛 / 去查看. Repeat 2–3 times until no visible overlays.
8. Use await page.wait_for_timeout(...) sparingly for UI settle.
9. Include short comments matching checklist intents.
10. Assume caller may already have navigated to base_url; still call page.goto(base) at start if base_url provided.
11. Import only what you need from playwright.async_api if helpers need expect — or use plain awaits.

Style reference (conceptual): placeholder clicks, exact tree label, close dialogs loop — like a hand-written login script.
"""

# Bare get_by_*().action → insert .first before action (strict-mode safety net).
_GET_BY_FIRST_RE = re.compile(
    r"""(?P<loc>page\.get_by_(?:placeholder|role|text|label|title|alt_text)\([^;\n]*?\))"""
    r"""(?!\s*\.first)"""
    r"""(?P<trail>\s*\.\s*(?:click|fill|press|press_sequentially|type|check|uncheck|select_option|hover|focus|blur|wait_for)\s*\()""",
    re.I,
)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:python)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _ensure_entrypoint(script: str, case_id: int) -> str:
    """Guarantee test_case_{id} exists; rename first test_case_* if needed."""
    name = f"test_case_{int(case_id)}"
    if re.search(rf"async\s+def\s+{re.escape(name)}\s*\(", script):
        return script
    m = re.search(r"async\s+def\s+(test_case_\w+)\s*\(", script)
    if m:
        return script.replace(m.group(1), name, 1)
    # wrap body — last resort
    return (
        "from playwright.async_api import expect\n\n"
        f"async def {name}(page) -> None:\n"
        "    raise RuntimeError('synthesized script missing body')\n"
    )


def harden_locators_with_first(script: str) -> str:
    """Insert .first before actions on bare get_by_* locators (strict mode)."""
    if not script:
        return script
    return _GET_BY_FIRST_RE.sub(r"\g<loc>.first\g<trail>", script)


async def synthesize_playwright_script(
    *,
    client: AsyncOpenAI,
    model: str,
    case_id: int,
    case_name: str,
    goal_text: str,
    journal: list[dict[str, Any]],
    base_url: str | None = None,
    temperature: float = 0.1,
) -> str:
    """Generate Playwright Python from successful journal."""
    payload = {
        "case_id": int(case_id),
        "case_name": case_name,
        "base_url": base_url or "",
        "goal": goal_text,
        "journal": journal,
    }
    user = (
        f"Synthesize async Playwright script for case_id={int(case_id)}.\n"
        f"Function name MUST be: async def test_case_{int(case_id)}(page)\n\n"
        f"CONTEXT JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=8192,
    )
    script = _strip_fences(resp.choices[0].message.content or "")
    if not script or "async def" not in script:
        raise ValueError("LLM returned empty/invalid Playwright script")
    return harden_locators_with_first(_ensure_entrypoint(script, int(case_id)))


async def repair_playwright_script(
    *,
    client: AsyncOpenAI,
    model: str,
    case_id: int,
    script: str,
    error: str,
    journal: list[dict[str, Any]] | None = None,
    temperature: float = 0.1,
) -> str:
    """One-shot repair after dry-run failure."""
    user = (
        f"case_id={int(case_id)}\n"
        f"DRY-RUN ERROR:\n{error}\n\n"
        f"CURRENT SCRIPT:\n{script}\n\n"
        f"JOURNAL (optional):\n{json.dumps(journal or [], ensure_ascii=False)[:20000]}\n"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": REPAIR_SYSTEM_PROMPT.replace("{case_id}", str(int(case_id))),
            },
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=8192,
    )
    fixed = _strip_fences(resp.choices[0].message.content or "")
    if not fixed or "async def" not in fixed:
        raise ValueError("LLM repair returned empty/invalid script")
    return harden_locators_with_first(_ensure_entrypoint(fixed, int(case_id)))
