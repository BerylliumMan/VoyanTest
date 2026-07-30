# core/blank_click.py
"""Detect and execute “click blank area” steps.

Playwright MCP ``browser_click`` only works on accessibility-tree refs.
Blank / outside clicks (dismiss dropdown, close overlay) have no useful ref,
so LLM often picks a wrong element or a no-op. We detect those steps and
drive a real viewport mouse click via ``browser_run_code_unsafe``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 点击页面空白处 / 单击空白区域 / 点击外侧关闭下拉 …
_BLANK_CLICK_RE = re.compile(
    r"(?:"
    r"(?:点击|单击|点一下|点下).{0,16}(?:页面)?(?:空白|空处|空余)(?:处|区域|地方|位置)?"
    r"|(?:点击|单击).{0,12}(?:页面外|外侧|外部|外面|遮罩|蒙层|蒙版)"
    r"|click\s+(?:on\s+)?(?:the\s+)?(?:blank|empty|outside)\b"
    r"|click\s+outside\b"
    r")",
    re.IGNORECASE,
)

# Selectors that mean “page chrome / document”, not a real control
_BLANK_SELECTOR_RE = re.compile(
    r"^(?:body|html|document|#?root|#?app|main)?$",
    re.IGNORECASE,
)

BLANK_CLICK_ACTION = "click_blank"

# Prefer a non-interactive viewport point; fall back to left-middle.
_BLANK_CLICK_CODE = """async (page) => {
  const pt = await page.evaluate(() => {
    const w = window.innerWidth || 1280;
    const h = window.innerHeight || 720;
    const candidates = [
      [8, Math.floor(h / 2)],
      [8, 8],
      [w - 8, Math.floor(h / 2)],
      [Math.floor(w / 2), h - 8],
      [Math.floor(w / 2), 8],
    ];
    const blocked =
      "a,button,input,select,textarea,label," +
      "[role='button'],[role='link'],[role='menuitem'],[role='option']," +
      "[role='combobox'],[role='listbox'],[role='dialog']," +
      ".ant-select-dropdown,.ant-picker-dropdown,.ant-dropdown," +
      ".arco-trigger-popup,.arco-select-popup,.el-select-dropdown," +
      ".el-picker-panel,.el-popper";
    for (const [x, y] of candidates) {
      const el = document.elementFromPoint(x, y);
      if (!el) continue;
      if (el.closest(blocked)) continue;
      return { x, y, tag: el.tagName || "" };
    }
    return { x: 8, y: Math.floor(h / 2), tag: "fallback" };
  });
  await page.mouse.click(pt.x, pt.y, { delay: 30 });
  return pt;
}"""


def is_blank_area_click_step(text: str | None) -> bool:
    """True when the step asks to click page blank / outside area."""
    if not text or not str(text).strip():
        return False
    return bool(_BLANK_CLICK_RE.search(str(text)))


def is_blank_area_selector(selector: str | None) -> bool:
    """True when selector is body/html/document-like (not a control ref)."""
    if selector is None:
        return False
    s = str(selector).strip()
    if not s:
        return False
    # Accessibility refs look like e12 / e123 — never treat as blank
    if re.fullmatch(r"e\d+", s, flags=re.IGNORECASE):
        return False
    if s.lower() in ("body", "html", "document", "root", "app", "main"):
        return True
    if s in ("#root", "#app", "body > *"):
        return True
    return bool(_BLANK_SELECTOR_RE.fullmatch(s))


def should_use_blank_click(
    *,
    action: str | None = None,
    selector: str | None = None,
    step_description: str | None = None,
) -> bool:
    """Whether execution should use viewport blank-click instead of browser_click."""
    act = (action or "").strip().lower()
    if act in (BLANK_CLICK_ACTION, "click_outside", "click_page"):
        return True
    if is_blank_area_click_step(step_description):
        return True
    if act in ("click", "browser_click") and is_blank_area_selector(selector):
        return True
    return False


async def execute_blank_click(
    call_tool: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run a real mouse click on a blank viewport point via MCP."""
    result = await call_tool(
        "browser_run_code_unsafe",
        {"code": _BLANK_CLICK_CODE},
    )
    if result.get("success"):
        logger.info(
            "Blank-area mouse click ok: %s",
            (result.get("text") or "")[:120],
        )
        return result

    # Fallback: Escape often dismisses the same overlays/dropdowns
    logger.warning(
        "Blank-area mouse click failed (%s); falling back to Escape",
        (result.get("error") or result.get("text") or "")[:160],
    )
    esc = await call_tool("browser_press_key", {"key": "Escape"})
    if esc.get("success"):
        return {
            "success": True,
            "text": "blank click fallback: Escape",
            "error": None,
        }
    return {
        "success": False,
        "error": (
            result.get("error")
            or result.get("text")
            or esc.get("error")
            or "blank click failed"
        ),
        "text": result.get("text") or "",
    }
