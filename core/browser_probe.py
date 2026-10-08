"""DOM 探针：从页面采集"行动元素"的富信息（角色/可访问名/属性/CSS/XPath/可见性）。

用途（给宏动作单元/OTA 用）：
  · 当模型给的选择器（ref/text=）找不到目标时，用探针在真实 DOM 里定位目标，
    并派生**确定性回退定位符**（data-testid → #id → 短 CSS → text= → XPath）
  · 找不到（0 匹配）就直接给证据，不再盲目重试；匹配多个→明确"歧义"而不是乱点

设计参考 zai-org/ZCode（Apache-2.0）apps/zcode-cli/packages/adapters/src/browser/snapshot.ts
（紧凑快照、隐藏元素过滤、隐式角色、可访问名回退顺序、CSS/XPath 生成、上限 200）。
本文件为按我们的 MCP(browser_evaluate) 通道的独立实现：无法创建 Playwright ElementHandle，
因此不产出 ref；其余字段对齐其形状，便于对照与后续演进。
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

MAX_ELEMENTS = 200

# 与 ZCode snapshot.ts 的 actionSelector 一致的思路：可交互/可操作的节点
COLLECT_FN = r"""() => {
  const actionSelector = "a[href],button,input,textarea,select,[role],[onclick],[tabindex],summary,label,[contenteditable]";
  const max = %d;
  const norm = (v, n) => String(v ?? "").trim().replace(/\s+/g, " ").slice(0, n);
  const hidden = (el) => {
    const s = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display === "none" || s.visibility === "hidden" || s.opacity === "0" || (r.width <= 0 && r.height <= 0);
  };
  const implicitRole = (el, tag) => {
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag !== "input") return "";
    const t = (el.getAttribute("type") || "text").toLowerCase();
    if (t === "checkbox") return "checkbox";
    if (t === "radio") return "radio";
    if (["button", "submit", "reset"].includes(t)) return "button";
    if (t === "search") return "searchbox";
    return "textbox";
  };
  const accName = (el) => norm(
    el.getAttribute("aria-label") || el.getAttribute("alt") || el.getAttribute("title") ||
    el.getAttribute("placeholder") || el.innerText || el.textContent ||
    (("value" in el) ? String(el.value) : ""), 120);
  const attrs = (el) => {
    const out = {};
    for (const k of ["id","name","type","placeholder","title","alt","role","aria-label","data-testid","data-test","data-qa","href"]) {
      const v = norm(el.getAttribute(k), 240);
      if (v) out[k] = v;
    }
    return out;
  };
  const cssPath = (el) => {
    if (el.id && /^[A-Za-z][A-Za-z0-9_-]*$/.test(el.id)) return "#" + el.id;
    const parts = [];
    let cur = el;
    for (let d = 0; cur && d < 6; d += 1) {
      const tag = cur.tagName.toLowerCase();
      const sib = cur.parentElement ? [...cur.parentElement.children].filter((c) => c.tagName === cur.tagName) : [];
      const idx = sib.indexOf(cur);
      parts.unshift(idx >= 0 ? tag + ":nth-of-type(" + (idx + 1) + ")" : tag);
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  };
  const xpath = (el) => {
    const parts = [];
    let cur = el;
    while (cur) {
      const tag = cur.tagName.toLowerCase();
      const sib = cur.parentElement ? [...cur.parentElement.children].filter((c) => c.tagName === cur.tagName) : [];
      parts.unshift(tag + "[" + (sib.indexOf(cur) + 1) + "]");
      cur = cur.parentElement;
    }
    return "/" + parts.join("/");
  };
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const inViewport = (r) => r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;

  const nodes = [...document.querySelectorAll(actionSelector)].slice(0, max);
  const elements = nodes.map((el) => {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || implicitRole(el, tag);
    const name = accName(el);
    const text = norm(el.innerText, 100);
    const value = ("value" in el) ? norm(String(el.value), 240) : "";
    const disabled = ("disabled" in el) && Boolean(el.disabled);
    const checked = ("checked" in el) ? Boolean(el.checked) : undefined;
    const visible = !hidden(el);
    const a = attrs(el);
    return {
      tag, role: role || undefined, name: name || undefined, text: text || undefined,
      value: value || undefined, disabled, checked,
      selector: cssPath(el), xpath: xpath(el),
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      inViewport: inViewport(rect), visible,
      attributes: Object.keys(a).length ? a : undefined,
    };
  }).filter((e) => e.visible);

  return JSON.stringify({
    url: location.href,
    title: document.title,
    count: elements.length,
    elements,
  });
}""" % MAX_ELEMENTS


def _extract_json(text: str) -> dict[str, Any]:
    """MCP evaluate 的返回常带 '### Result' 包裹/转义，宽松提取 JSON。"""
    raw = str(text or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    candidate = m.group(0)
    for probe in (candidate, candidate.replace('\\"', '"').replace("\\n", " ")):
        try:
            data = json.loads(probe)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return {}


async def collect_elements(
    caller: Callable[[str, dict], Awaitable[dict]],
    *,
    max_elements: int = MAX_ELEMENTS,
) -> dict[str, Any]:
    """通过 browser_evaluate 采集行动元素。返回 {url,title,count,elements:[...]}。"""
    r = await caller("browser_evaluate", {"function": COLLECT_FN})
    if not isinstance(r, dict) or not r.get("success"):
        return {"url": "", "title": "", "count": 0, "elements": []}
    return _extract_json(str(r.get("text") or ""))


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def match_target(elements: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """按"精确名/文本 → 包含"匹配；返回按优先级排序的候选（不改判歧义，交给调用方）。"""
    t = _norm(target)
    if not t:
        return []
    exact: list[dict[str, Any]] = []
    contains: list[dict[str, Any]] = []
    for e in elements or []:
        names = [_norm(e.get("name")), _norm(e.get("text")), _norm(e.get("value"))]
        if t in names:
            exact.append(e)
        elif any(t in n for n in names if n):
            contains.append(e)
    return exact + contains


def derive_fallbacks(entry: dict[str, Any]) -> list[str]:
    """从探针条目派生确定性回退定位符（按可靠性排序）。"""
    out: list[str] = []
    attrs = entry.get("attributes") or {}
    if attrs.get("data-testid"):
        out.append(f'[data-testid="{attrs["data-testid"]}"]')
    if attrs.get("data-test"):
        out.append(f'[data-test="{attrs["data-test"]}"]')
    if attrs.get("id") and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", attrs["id"]):
        out.append(f'#{attrs["id"]}')
    if entry.get("name"):
        out.append(f'text={entry["name"]}')
    if entry.get("selector"):
        out.append(str(entry["selector"]))
    if entry.get("xpath"):
        out.append(f"xpath={entry['xpath']}")
    # 去重保序
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


async def probe_fallbacks(
    caller: Callable[[str, dict], Awaitable[dict]],
    target: str,
) -> tuple[list[str], dict[str, Any]]:
    """探针定位：返回 (回退定位符列表, 证据)。0 匹配/歧义都如实体现在证据里。"""
    snap = await collect_elements(caller)
    matched = match_target(snap.get("elements") or [], target)
    evidence = {
        "url": snap.get("url", ""),
        "elements": snap.get("count", 0),
        "matched": len(matched),
        "names": [str(m.get("name") or m.get("text") or "")[:40] for m in matched[:3]],
    }
    if len(matched) == 1:
        return derive_fallbacks(matched[0]), evidence
    if len(matched) > 1:
        # 歧义：只取名字完全相等的那些，若仍多个则放弃（不乱点）
        t = _norm(target)
        exacts = [m for m in matched if _norm(m.get("name")) == t or _norm(m.get("text")) == t]
        if len(exacts) == 1:
            return derive_fallbacks(exacts[0]), evidence
        evidence["ambiguous"] = True
    return [], evidence
