# core/codegen_locator.py
"""Load Playwright codegen IIFE and build evaluate payloads for locator solidify."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ASSET = Path(__file__).resolve().parent / "assets" / "codegen_locator.iife.js"


@lru_cache(maxsize=1)
def load_codegen_iife() -> str:
    """Return the minified IIFE source (installs window.__vtCodegen)."""
    if not _ASSET.is_file():
        raise FileNotFoundError(
            f"codegen IIFE missing: {_ASSET}. Run: node scripts/build_codegen_iife.mjs"
        )
    return _ASSET.read_text(encoding="utf-8")


def build_codegen_inject_js() -> str:
    """JS function that installs the IIFE once (idempotent)."""
    iife = load_codegen_iife()
    return (
        "() => {\n"
        "  if (!window.__vtCodegen || !window.__vtCodegen.resolvePlaywrightLocator) {\n"
        f"    {iife}\n"
        "  } else if (window.__vtCodegen.installCapture) {\n"
        "    window.__vtCodegen.installCapture();\n"
        "  }\n"
        "  return { ok: Boolean(window.__vtCodegen && window.__vtCodegen.resolvePlaywrightLocator) };\n"
        "}"
    )


def build_codegen_resolve_js(hint: dict[str, Any] | None = None) -> str:
    """JS function for browser_evaluate: resolve locator (assumes IIFE injected).

    ``hint`` may include placeholder / exact_text / name so we can find the node
    even when MCP clicks do not fire DOM capture listeners.
    Falls back to injecting IIFE inline if missing (larger payload).
    """
    import json

    hint_json = json.dumps(
        {
            "placeholder": (hint or {}).get("placeholder"),
            "exact_text": (hint or {}).get("exact_text"),
            "name": (hint or {}).get("name"),
        },
        ensure_ascii=False,
    )
    # Prefer thin call; re-inject only if prior inject was skipped/failed.
    iife = load_codegen_iife()
    return (
        "() => {\n"
        "  if (!window.__vtCodegen || !window.__vtCodegen.resolvePlaywrightLocator) {\n"
        f"    {iife}\n"
        "  } else if (window.__vtCodegen.installCapture) {\n"
        "    window.__vtCodegen.installCapture();\n"
        "  }\n"
        f"  const hint = {hint_json};\n"
        "  return window.__vtCodegen.resolvePlaywrightLocator(hint);\n"
        "}"
    )


def normalize_playwright_locator(loc: str | None) -> str | None:
    import re

    if not loc or not isinstance(loc, str):
        return None
    s = loc.strip()
    if s.startswith("page."):
        s = s[5:].strip()
    if not s:
        return None
    # reject bare ephemeral snapshot refs mistaken as locators
    if re.fullmatch(r"(?:e|f5e|ref_|probe_idx_)\d+", s, re.I):
        return None
    return s


def merge_codegen_into_replay(
    replay: dict[str, Any] | None,
    codegen: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach playwright_locator from codegen resolve without overriding checklist values."""
    out = dict(replay or {})
    if not isinstance(codegen, dict):
        return out
    loc = normalize_playwright_locator(codegen.get("playwright_locator"))
    if loc:
        out["playwright_locator"] = loc
    cands = codegen.get("locator_candidates")
    if isinstance(cands, list):
        cleaned = [normalize_playwright_locator(c) for c in cands]
        out["locator_candidates"] = [c for c in cleaned if c][:6]
    active = codegen.get("active")
    if isinstance(active, dict):
        if active.get("placeholder") and not out.get("placeholder"):
            out["placeholder"] = active["placeholder"]
        if active.get("text") and not out.get("exact_text"):
            # only soft-fill when strategy is click-like without better label
            if out.get("strategy") in ("click_text", "click_role", None):
                out.setdefault("exact_text", active["text"])
    return out
