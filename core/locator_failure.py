# core/locator_failure.py
"""Shared heuristics: classify MCP/LLM step failures as locator vs assertion."""

from __future__ import annotations

_LOCATOR_FAIL_HINTS = (
    "not found",
    "no element",
    "no popup",
    "no modal",
    "no dialog",
    "no alert",
    "no matching",
    "locator",
    "timeout",
    "timed out",
    "unable to find",
    "unable to locate",
    "cannot find",
    "can't find",
    "could not find",
    "cannot proceed",
    "can't proceed",
    "strict mode violation",
    "waiting for",
    "does not exist",
    "does not match",
    "doesn't match",
    "isn't visible",
    "is not visible",
    "not visible",
    "ambiguous or missing",
    "structured bind failed",
    "找不到",
    "无法定位",
    "定位失败",
    "无法确定本步",
    "无法确定",
    "看不到",
    "不存在",
    "没有找到",
    "未找到",
    "element is not",
)


def is_locator_failure(result: dict | None) -> bool:
    """True when MCP/LLM failure looks like element locating, not assertion."""
    if not result:
        return False
    err = f"{result.get('error') or ''} {result.get('action') or ''}"
    low = err.lower()
    if "verification failed" in low or "expected result verification" in low or "断言" in err:
        return False
    action = (result.get("action") or "").strip().lower()
    if action == "error" or action.startswith("error("):
        return True
    if any(h in low for h in _LOCATOR_FAIL_HINTS):
        return True
    if "found" in low and (
        low.startswith("no ")
        or " no " in low
        or "none " in low
        or "没有" in err
        or "未找到" in err
    ):
        return True
    return False


def should_hybrid_browser_use_fallback(
    *, hybrid: bool, result: dict | None, action_lower: str = ""
) -> bool:
    if not hybrid or not result or result.get("success") or action_lower == "done":
        return False
    # assert / wait 是读页面，不是定位失败救场对象；交给 browser-use 会改页面「凑」断言
    if (action_lower or "").strip().lower() in (
        "assert_text", "assert_visible", "wait", "assert", "screenshot",
    ):
        return False
    return is_locator_failure(result)
