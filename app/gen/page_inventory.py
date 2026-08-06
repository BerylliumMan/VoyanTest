# app/gen/page_inventory.py
"""Lightweight live-page control inventory for generation grounding.

Uses Python Playwright only (no MCP / nl_goal). Soft-fail: never raises to caller.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "listbox",
    "checkbox",
    "radio",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "switch",
    "option",
    "treeitem",
    "slider",
}

MAX_LINES = 80
CAPTURE_TIMEOUT_S = 90
INIT_SCRIPT_TIMEOUT_S = 45

_EVAL_INVENTORY_JS = """() => {
  const roles = new Set(%s);
  const out = [];
  const seen = new Set();
  const push = (role, name) => {
    const r = (role || '').toLowerCase();
    const n = (name || '').replace(/\\s+/g, ' ').trim();
    if (!n || !roles.has(r)) return;
    const key = r + '|' + n;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ role: r, name: n });
  };
  const walk = (root, depth) => {
    if (!root || depth > 3 || out.length >= 120) return;
    let nodes = [];
    try {
      const scope = root.body || root;
      nodes = Array.from((scope.querySelectorAll ? scope : root).querySelectorAll('*'));
    } catch (e) { return; }
    for (const el of nodes) {
      if (out.length >= 120) break;
      try {
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
      } catch (e) { continue; }
      const tag = (el.tagName || '').toLowerCase();
      let role = (el.getAttribute('role') || '').toLowerCase();
      if (!role) {
        if (tag === 'button' || (tag === 'input' && /button|submit|reset/i.test(el.type || ''))) role = 'button';
        else if (tag === 'a') role = 'link';
        else if (tag === 'textarea') role = 'textbox';
        else if (tag === 'select') role = 'combobox';
        else if (tag === 'input') {
          const t = (el.type || 'text').toLowerCase();
          if (t === 'checkbox') role = 'checkbox';
          else if (t === 'radio') role = 'radio';
          else if (t === 'search') role = 'searchbox';
          else role = 'textbox';
        }
      }
      const name = el.getAttribute('aria-label')
        || el.getAttribute('placeholder')
        || el.getAttribute('title')
        || (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
      push(role, name);
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  };
  walk(document, 0);
  return out;
}""" % (repr(sorted(INTERACTIVE_ROLES)))


def _flatten_ax(node: Any, out: list[dict[str, str]]) -> None:
    if not isinstance(node, dict):
        return
    role = (node.get("role") or "").strip().lower()
    name = (node.get("name") or "").strip()
    if role and name:
        out.append({"role": role, "name": name})
    for child in node.get("children") or []:
        _flatten_ax(child, out)


def _format_inventory(rows: list[dict[str, str]]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for row in rows:
        role = (row.get("role") or "").strip().lower()
        name = (row.get("name") or "").strip()
        if not role or not name or role not in INTERACTIVE_ROLES:
            continue
        key = f"{role}|{name}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{role} | {name}")
        if len(lines) >= MAX_LINES:
            break
    if not lines:
        return ""
    header = (
        f"【页面实况控件 — 共 {len(lines)} 项；"
        "步骤【控件】必须能在此列表指认，禁止编造列表外控件】"
    )
    return header + "\n" + "\n".join(lines)


def _normalize_cookies(raw: list | None, base_url: str) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    out: list[dict] = []
    if not raw:
        return out, warnings
    host = ""
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:
        host = ""
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            warnings.append("跳过无效 cookie（缺 name/value）")
            continue
        c: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "path": item.get("path") or "/",
        }
        domain = (item.get("domain") or "").strip()
        if domain:
            c["domain"] = domain
        elif host:
            c["domain"] = host
        else:
            warnings.append(f"cookie {name} 无 domain 且无法从 base_url 推断，已跳过")
            continue
        if item.get("url"):
            c["url"] = item["url"]
        for opt in ("httpOnly", "secure", "sameSite", "expires"):
            if opt in item and item[opt] is not None:
                c[opt] = item[opt]
        out.append(c)
    return out, warnings


def _load_compiled_fn(script: str, case_id: int):
    ns: dict = {"__name__": "__vt_page_inventory__"}
    with tempfile.NamedTemporaryFile(
        prefix=f"vt_inv_{case_id}_", suffix=".py", delete=False, mode="w", encoding="utf-8",
    ) as tf:
        tf.write(script)
        tmp_path = tf.name
    try:
        code = compile(script, tmp_path, "exec")
        exec(code, ns, ns)
        fn = ns.get(f"test_case_{int(case_id)}")
        if not callable(fn):
            for k, v in ns.items():
                if str(k).startswith("test_case_") and callable(v):
                    fn = v
                    break
        if not callable(fn):
            raise RuntimeError("compiled script has no test_case_* entrypoint")
        return fn
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


async def _collect_rows(page) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        snap = await page.accessibility.snapshot()
        flat: list[dict[str, str]] = []
        _flatten_ax(snap, flat)
        rows = flat
    except Exception as exc:
        logger.info("accessibility.snapshot failed, fallback evaluate: %s", exc)
    if not rows:
        try:
            rows = await page.evaluate(_EVAL_INVENTORY_JS) or []
        except Exception as exc:
            logger.warning("inventory evaluate failed: %s", exc)
            rows = []
    return [r for r in rows if isinstance(r, dict)]


async def _capture_impl(
    db: AsyncSession,
    *,
    project_id: int,
    environment_id: int,
    case_kind: str | None = "ui",
) -> tuple[str, list[str]]:
    from app.crud.environment import get_environment
    from app.crud.project import get_project
    from app.crud.testcase import get_init_test_cases

    warnings: list[str] = []
    env = await get_environment(db, environment_id)
    if not env or int(env.project_id) != int(project_id):
        return "", ["环境不存在或不属于当前项目"]

    base_url = (env.base_url or "").strip()
    if not base_url:
        project = await get_project(db, project_id)
        base_url = ((getattr(project, "base_url", None) or "") if project else "").strip()
    if not base_url:
        return "", ["未配置环境/项目 base_url，无法采集页面真值"]

    cookies_raw = list(env.cookies or []) if isinstance(env.cookies, list) else []
    cookies, cw = _normalize_cookies(cookies_raw, base_url)
    warnings.extend(cw)

    inits = await get_init_test_cases(db, project_id, case_kind=case_kind)
    compiled_inits = [
        tc for tc in inits
        if (getattr(tc, "compiled_script", None) or "").strip()
    ]

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            logged_in = False

            if cookies:
                try:
                    await context.add_cookies(cookies)
                    await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(800)
                    logged_in = True
                    logger.info(
                        "page_inventory: cookies injected count=%s project=%s",
                        len(cookies),
                        project_id,
                    )
                except Exception as exc:
                    warnings.append(f"cookies 注入/打开失败: {exc}")
                    logged_in = False

            if not logged_in and compiled_inits:
                for tc in compiled_inits:
                    script = (tc.compiled_script or "").strip()
                    try:
                        fn = _load_compiled_fn(script, int(tc.id))
                        await asyncio.wait_for(
                            fn(page),
                            timeout=INIT_SCRIPT_TIMEOUT_S,
                        )
                        logged_in = True
                        logger.info(
                            "page_inventory: ran compiled init case_id=%s",
                            tc.id,
                        )
                        break
                    except Exception as exc:
                        warnings.append(
                            f"固化 init 用例 {tc.id} 失败: {str(exc)[:120]}"
                        )

            if not logged_in:
                warnings.append(
                    "无可用 cookies / 固化 init，仅打开 base_url（可能未登录）"
                )
                try:
                    await page.goto(
                        base_url, wait_until="domcontentloaded", timeout=60000
                    )
                    await page.wait_for_timeout(800)
                except Exception as exc:
                    return "", warnings + [f"打开 base_url 失败: {exc}"]

            rows = await _collect_rows(page)
            text = _format_inventory(rows)
            if not text:
                warnings.append("页面未解析到带名称的交互控件")
            else:
                n = len([ln for ln in text.splitlines() if " | " in ln])
                logger.info("page_inventory lines=%s project=%s", n, project_id)
            return text, warnings
        finally:
            await browser.close()


async def capture_page_inventory(
    db: AsyncSession,
    *,
    project_id: int,
    environment_id: int,
    case_kind: str | None = "ui",
) -> tuple[str, list[str]]:
    """Return ``(inventory_text, warnings)``. Never raises."""
    try:
        return await asyncio.wait_for(
            _capture_impl(
                db,
                project_id=project_id,
                environment_id=environment_id,
                case_kind=case_kind,
            ),
            timeout=CAPTURE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("page_inventory timed out project=%s", project_id)
        return "", ["页面真值采集超时"]
    except Exception as exc:
        logger.exception("page_inventory failed project=%s", project_id)
        return "", [f"页面真值采集失败: {exc}"]
