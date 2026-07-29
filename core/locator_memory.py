"""Learned locator memory: cache successful element fingerprints for faster replay.

MCP refs (e15) and browser-use indices are ephemeral. We store stable fingerprints
(role/name/text/xpath) and re-bind to the current snapshot on later runs.

Safety: unique match only; optional URL hint; expected-result failure invalidates cache.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Playwright MCP accessibility snapshot lines, e.g.:
#   - button "登录" [ref=e15]
#   - textbox "用户名" [ref=e12] [cursor=pointer]:
_SNAP_LINE_RE = re.compile(
    r"^\s*-\s+"
    r"(?P<role>[a-zA-Z0-9_-]+)"
    r"(?:\s+"
    r"(?:\"(?P<name>[^\"]*)\"|'(?P<name2>[^']*)')"
    r")?"
    r".*?\[ref=(?P<ref>e\d+)\]",
    re.MULTILINE,
)

_PAGE_URL_RE = re.compile(r"Page\s+URL:\s*(.+?)(?:\r?\n|$)", re.IGNORECASE)
_BRACKET_TEXT_RE = re.compile(r"【([^】]+)】")

_LEARNABLE_ACTIONS = frozenset({
    "click", "fill", "select", "hover", "browser_click", "browser_type", "browser_select_option",
})


def is_learnable_action(action: str | None) -> bool:
    return (action or "").strip().lower() in _LEARNABLE_ACTIONS


def extract_page_url(snapshot: str) -> str:
    m = _PAGE_URL_RE.search(snapshot or "")
    return (m.group(1).strip() if m else "") or ""


def page_url_hint(url: str) -> str:
    """Path (+ query stripped) used as soft URL gate on replay."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return (p.path or "/").rstrip("/") or "/"
    except Exception:
        return url[:120]


def parse_snapshot_elements(snapshot: str) -> list[dict[str, str]]:
    """Parse accessibility snapshot into {role, name, ref} rows."""
    rows: list[dict[str, str]] = []
    for m in _SNAP_LINE_RE.finditer(snapshot or ""):
        name = (m.group("name") or m.group("name2") or "").strip()
        rows.append({
            "role": (m.group("role") or "").strip().lower(),
            "name": name,
            "ref": m.group("ref"),
        })
    return rows


def extract_from_snapshot(
    snapshot: str,
    ref: str,
    *,
    action: str,
    value: str | None = None,
) -> Optional[dict[str, Any]]:
    """Build a fingerprint for the element with the given ref."""
    ref = (ref or "").strip()
    if not ref:
        return None
    for el in parse_snapshot_elements(snapshot):
        if el["ref"] != ref:
            continue
        if not el["name"] and not el["role"]:
            return None
        url = extract_page_url(snapshot)
        return {
            "action": (action or "click").strip().lower(),
            "role": el["role"],
            "name": el["name"],
            "value": value,
            "page_url_hint": page_url_hint(url),
            "hit_count": 1,
            "source": "playwright_mcp",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    return None


def fingerprint_matches_step_description(fp: dict[str, Any], description: str) -> bool:
    """Require fingerprint name to appear in step text / 【】 so we avoid cross-page same-label."""
    name = (fp.get("name") or "").strip()
    if not name:
        return True  # role-only fingerprints rely on unique match + URL
    desc = description or ""
    if name in desc:
        return True
    for m in _BRACKET_TEXT_RE.finditer(desc):
        if name in m.group(1) or m.group(1) in name:
            return True
    return False


def url_hint_ok(fp: dict[str, Any], snapshot: str) -> bool:
    hint = (fp.get("page_url_hint") or "").strip()
    if not hint or hint == "/":
        return True
    current = page_url_hint(extract_page_url(snapshot))
    if not current:
        return True  # no URL in snapshot — don't block
    return hint in current or current in hint


def resolve_ref(
    snapshot: str,
    fingerprint: dict[str, Any],
    *,
    step_description: str = "",
) -> Optional[str]:
    """Return current ref only when exactly one element matches the fingerprint."""
    if not fingerprint:
        return None
    if not url_hint_ok(fingerprint, snapshot):
        logger.info("locator_memory: URL hint mismatch, skip replay")
        return None
    if step_description and not fingerprint_matches_step_description(fingerprint, step_description):
        logger.info("locator_memory: fingerprint name not in step description, skip replay")
        return None

    role = (fingerprint.get("role") or "").strip().lower()
    name = (fingerprint.get("name") or "").strip()
    matches: list[str] = []
    for el in parse_snapshot_elements(snapshot):
        if role and el["role"] != role:
            continue
        if name and el["name"] != name:
            continue
        if not role and not name:
            continue
        matches.append(el["ref"])

    if len(matches) != 1:
        if matches:
            logger.info("locator_memory: ambiguous match count=%s, skip replay", len(matches))
        return None
    return matches[0]


def bump_hit_count(fp: dict[str, Any]) -> dict[str, Any]:
    out = dict(fp)
    out["hit_count"] = int(fp.get("hit_count") or 0) + 1
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def format_hint_for_agent(fp: dict[str, Any] | None) -> str:
    """NL hint injected into browser-use step task when direct replay is unavailable."""
    if not fp:
        return ""
    role = fp.get("role") or ""
    name = fp.get("name") or ""
    action = fp.get("action") or "click"
    parts = [f"优先操作记忆元素：action={action}"]
    if role:
        parts.append(f"role={role}")
    if name:
        parts.append(f"文案「{name}」")
    parts.append("不要探索无关控件")
    return "；".join(parts)


async def persist_learned_locators_from_results(
    db,
    step_results: list[dict],
    *,
    steps_by_order: dict[int, Any] | None = None,
) -> int:
    """Apply learned_locator / invalidate flags from step results onto TestStep rows.

    ``steps_by_order`` maps step_order → ORM TestStep (or object with .learned_locator).
    Returns number of rows updated.
    """
    if not step_results or not steps_by_order:
        return 0
    updated = 0
    for r in step_results:
        order = r.get("step_number") or r.get("step_order")
        if order is None:
            continue
        obj = steps_by_order.get(int(order))
        if obj is None:
            continue
        if r.get("invalidate_learned_locator"):
            obj.learned_locator = None
            updated += 1
            continue
        learned = r.get("learned_locator")
        if isinstance(learned, dict):
            obj.learned_locator = learned
            updated += 1
    if updated:
        try:
            await db.commit()
        except Exception:
            logger.warning("persist learned_locator commit failed", exc_info=True)
            return 0
    return updated


async def try_replay_mcp(
    mcp_manager,
    fingerprint: dict[str, Any],
    *,
    snapshot: str,
    step_description: str = "",
) -> dict[str, Any]:
    """Replay via MCP without LLM. Returns {success, skipped?, error?, tool_call?, ref?}."""
    ref = resolve_ref(snapshot, fingerprint, step_description=step_description)
    if not ref:
        return {"success": False, "skipped": True, "error": "no unique locator match"}

    action = (fingerprint.get("action") or "click").strip().lower()
    # Normalize browser_* names
    if action.startswith("browser_"):
        action = {
            "browser_click": "click",
            "browser_type": "fill",
            "browser_select_option": "select",
        }.get(action, action.replace("browser_", "", 1))

    tool_call = {
        "action": action,
        "selector": ref,
        "value": fingerprint.get("value"),
        "thinking": f"locator_memory replay ref={ref} role={fingerprint.get('role')} name={fingerprint.get('name')}",
        "next_goal": "",
    }
    try:
        exec_result = await mcp_manager.execute_tool_call(tool_call)
    except Exception as exc:
        logger.warning("locator_memory MCP replay failed: %s", exc, exc_info=True)
        return {"success": False, "skipped": False, "error": str(exc), "tool_call": tool_call, "ref": ref}

    return {
        "success": bool(exec_result.get("success")),
        "skipped": False,
        "error": exec_result.get("error"),
        "tool_call": tool_call,
        "ref": ref,
        "exec_result": exec_result,
    }


async def try_replay_browser_use(
    session,
    fingerprint: dict[str, Any],
    *,
    step_description: str = "",
) -> dict[str, Any]:
    """Best-effort CDP text/role click for browser-use sessions.

    Returns skipped=True when unsafe / unsupported so caller falls back to Agent.
    """
    if not session or not fingerprint:
        return {"success": False, "skipped": True, "error": "no session"}
    name = (fingerprint.get("name") or "").strip()
    if not name:
        return {"success": False, "skipped": True, "error": "no name for CDP replay"}
    if step_description and not fingerprint_matches_step_description(fingerprint, step_description):
        return {"success": False, "skipped": True, "error": "name not in step"}

    action = (fingerprint.get("action") or "click").strip().lower()
    if action not in ("click", "browser_click"):
        # fill/select need more context; fall back to Agent with hint
        return {"success": False, "skipped": True, "error": "only click supported for BU direct replay"}

    # Escape for JS string
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    js = f"""(() => {{
      const name = '{safe}';
      const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
      const hits = nodes.filter(el => {{
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        return t === name || t.includes(name);
      }}).filter(el => {{
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }});
      if (hits.length !== 1) return {{ ok: false, count: hits.length }};
      hits[0].click();
      return {{ ok: true, count: 1 }};
    }})()"""

    try:
        focus = getattr(session, "agent_focus", None)
        if focus is None:
            get_sess = getattr(session, "get_or_create_cdp_session", None)
            if callable(get_sess):
                focus = await get_sess(focus=True)
        if focus is None:
            return {"success": False, "skipped": True, "error": "no CDP focus"}

        cdp = getattr(focus, "cdp_client", None)
        sid = getattr(focus, "session_id", None)
        if cdp is None:
            return {"success": False, "skipped": True, "error": "no cdp client"}

        result = await cdp.send.Runtime.evaluate(
            params={"expression": js, "returnByValue": True},
            session_id=sid,
        )
        value = (result or {}).get("result", {}).get("value") or {}
        if value.get("ok"):
            return {"success": True, "skipped": False, "error": None}
        return {
            "success": False,
            "skipped": True,
            "error": f"CDP unique click failed count={value.get('count')}",
        }
    except Exception as exc:
        logger.warning("locator_memory browser-use replay failed: %s", exc, exc_info=True)
        return {"success": False, "skipped": True, "error": str(exc)}
