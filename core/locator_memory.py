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
#   - textbox "Username" [ref=f4e11]   (frame-prefixed refs)
_SNAP_LINE_RE = re.compile(
    r"^\s*-\s+"
    r"(?P<role>[a-zA-Z0-9_-]+)"
    r"(?:\s+"
    r"(?:\"(?P<name>[^\"]*)\"|'(?P<name2>[^']*)')"
    r")?"
    r".*?\[ref=(?P<ref>[a-zA-Z]*\d*e\d+)\]",
    re.MULTILINE,
)

_PAGE_URL_RE = re.compile(r"Page\s+URL:\s*(.+?)(?:\r?\n|$)", re.IGNORECASE)
_BRACKET_TEXT_RE = re.compile(r"【([^】]+)】")

_LEARNABLE_ACTIONS = frozenset({
    "click", "fill", "select", "hover", "browser_click", "browser_type", "browser_select_option",
    # wait / assert：有 target_name（或 assert 文案 value）时也写入记忆，供编辑器展示与后续重放提示
    "wait", "assert_text", "assert_visible",
})


def is_learnable_action(action: str | None) -> bool:
    return (action or "").strip().lower() in _LEARNABLE_ACTIONS


def fingerprint_from_target_name(
    *,
    action: str,
    target_name: str | None = None,
    target_role: str | None = None,
    value: str | None = None,
    snapshot: str = "",
) -> Optional[dict[str, Any]]:
    """Build a locator fingerprint from StructuredStep target_name / assert value.

    Used when the MCP tool call has no ref (wait / assert_text), or to backfill
    name/role from structured fields after a successful interactive step.
    """
    name = (target_name or "").strip()
    val = None if value is None else str(value).strip() or None
    if not name and val:
        name = val
    if not name:
        return None

    role = (target_role or "").strip().lower() or None
    if snapshot:
        matches: list[dict[str, str]] = []
        for el in parse_snapshot_elements(snapshot):
            el_name = el.get("name") or ""
            if not el_name:
                continue
            if el_name == name or name in el_name or el_name in name:
                if role and el.get("role") != role:
                    continue
                matches.append(el)
        if len(matches) == 1:
            role = matches[0].get("role") or role
            name = matches[0].get("name") or name

    url = extract_page_url(snapshot) if snapshot else ""
    return {
        "action": (action or "wait").strip().lower(),
        "role": role,
        "name": name,
        "value": val,
        "page_url_hint": page_url_hint(url),
        "hit_count": 1,
        "source": "structured_target",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def learn_fingerprint_after_success(
    *,
    action: str | None,
    selector: str | None = None,
    value: str | None = None,
    snapshot: str = "",
    structured_step: dict | None = None,
    cached_fp: dict | None = None,
    used_replay: bool = False,
) -> Optional[dict[str, Any]]:
    """Prefer AX-ref fingerprint; fall back to structured target_name / value."""
    act = (action or "").strip().lower()
    if not is_learnable_action(act):
        return None

    if used_replay and isinstance(cached_fp, dict):
        return bump_hit_count(cached_fp)

    if selector:
        fp = extract_from_snapshot(
            snapshot or "",
            str(selector),
            action=act,
            value=value,
        )
        if fp:
            # Merge structured target_name when snapshot name empty
            struct = structured_step if isinstance(structured_step, dict) else None
            if struct and not (fp.get("name") or "").strip():
                tn = (struct.get("target_name") or "").strip()
                if tn:
                    fp = {**fp, "name": tn}
            return fp

    struct = structured_step if isinstance(structured_step, dict) else None
    if not struct:
        # wait/assert_text often only have value on the tool call
        if act in ("wait", "assert_text", "assert_visible") and value:
            return fingerprint_from_target_name(
                action=act, value=value, snapshot=snapshot or "",
            )
        return None

    return fingerprint_from_target_name(
        action=act or (struct.get("action") or "wait"),
        target_name=struct.get("target_name"),
        target_role=struct.get("target_role"),
        value=struct.get("value") if struct.get("value") is not None else value,
        snapshot=snapshot or "",
    )


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
    """Return current ref when a unique fingerprint match exists.

    Matching order: exact name → longest prefix (ellipsis salvage) → controlled
    containment when only one candidate remains.
    """
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
    if not role and not name:
        return None

    exact: list[str] = []
    prefix: list[tuple[int, str]] = []  # (prefix_len, ref)
    partial: list[str] = []
    for el in parse_snapshot_elements(snapshot):
        if role and el["role"] != role:
            continue
        el_name = el.get("name") or ""
        if not name:
            exact.append(el["ref"])
            continue
        if el_name == name:
            exact.append(el["ref"])
        elif el_name.startswith(name) or name.startswith(el_name):
            # Require meaningful prefix (≥2 chars) to avoid single-char noise
            plen = min(len(el_name), len(name))
            if plen >= 2:
                prefix.append((plen, el["ref"]))
        elif name in el_name or el_name in name:
            if len(name) >= 2 and len(el_name) >= 2:
                partial.append(el["ref"])

    if len(exact) == 1:
        return exact[0]
    if exact:
        logger.info("locator_memory: ambiguous exact match count=%s, skip replay", len(exact))
        return None

    if prefix:
        prefix.sort(key=lambda x: -x[0])
        best_len = prefix[0][0]
        tops = [r for L, r in prefix if L == best_len]
        if len(tops) == 1:
            logger.info("locator_memory: unique longest-prefix match len=%s", best_len)
            return tops[0]
        logger.info("locator_memory: ambiguous prefix match count=%s, skip replay", len(tops))
        return None

    if len(partial) == 1:
        logger.info("locator_memory: unique containment match")
        return partial[0]
    if partial:
        logger.info("locator_memory: ambiguous partial match count=%s, skip replay", len(partial))
    return None


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


def normalize_learned_blob(blob: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize v1 fingerprint or v2 plan blob."""
    if not isinstance(blob, dict):
        return None
    return blob


def get_plan_steps(blob: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return plan action list; v1 single fingerprint becomes one-step plan."""
    blob = normalize_learned_blob(blob)
    if not blob:
        return []
    plan = blob.get("plan")
    if isinstance(plan, list) and plan:
        return [p for p in plan if isinstance(p, dict)]
    # v1: flat fingerprint
    if blob.get("role") or blob.get("name") or blob.get("action"):
        return [{
            "action": blob.get("action") or "click",
            "role": blob.get("role"),
            "name": blob.get("name"),
            "value": blob.get("value"),
        }]
    return []


def build_plan_blob(
    steps: list[dict[str, Any]],
    *,
    page_url_hint: str = "",
    hit_count: int = 1,
) -> dict[str, Any]:
    return {
        "version": 2,
        "plan": steps,
        "page_url_hint": page_url_hint or "",
        "hit_count": hit_count,
        "source": "playwright_mcp",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def try_replay_plan_mcp(
    mcp_manager,
    learned: dict[str, Any],
    *,
    snapshot: str,
    step_description: str = "",
) -> dict[str, Any]:
    """Replay multi-step plan by rebinding each fingerprint to current snapshot.

    On any step failure returns success=False (caller should invalidate + LLM fallback).
    """
    plan = get_plan_steps(learned)
    if not plan:
        return {"success": False, "skipped": True, "error": "empty plan"}

    # Soft URL gate using top-level hint if present
    if learned.get("page_url_hint") and not url_hint_ok(
        {"page_url_hint": learned.get("page_url_hint")}, snapshot,
    ):
        return {"success": False, "skipped": True, "error": "plan url hint mismatch"}

    actions_log: list[str] = []
    last_tool: dict[str, Any] | None = None
    last_exec: dict[str, Any] | None = None
    current_snap = snapshot

    for i, step_fp in enumerate(plan):
        fp = {
            "action": step_fp.get("action") or "click",
            "role": step_fp.get("role"),
            "name": step_fp.get("name"),
            "value": step_fp.get("value"),
            "page_url_hint": learned.get("page_url_hint") or "",
        }
        # Only enforce step-description name check on the last/primary target
        desc = step_description if i == len(plan) - 1 else ""
        one = await try_replay_mcp(
            mcp_manager, fp, snapshot=current_snap, step_description=desc,
        )
        if one.get("skipped") or not one.get("success"):
            return {
                "success": False,
                "skipped": bool(one.get("skipped")),
                "error": one.get("error") or f"plan step {i + 1} failed",
                "plan_index": i,
            }
        tc = one.get("tool_call") or {}
        actions_log.append(f"{tc.get('action')}({tc.get('selector')})")
        last_tool = tc
        last_exec = one.get("exec_result") or {"success": True}
        if i < len(plan) - 1:
            try:
                current_snap = await mcp_manager.get_dom_snapshot()
            except Exception as exc:
                return {
                    "success": False,
                    "skipped": False,
                    "error": f"snapshot refresh after plan step {i + 1}: {exc}",
                }

    return {
        "success": True,
        "skipped": False,
        "error": None,
        "tool_call": last_tool,
        "exec_result": last_exec,
        "plan_replay": True,
        "actions_log": actions_log,
    }
