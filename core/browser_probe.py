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
    """MCP evaluate 的返回常带 '### Result' 包裹 + 双编码转义（返回字符串时），宽松提取。

    实测格式（playwright-mcp 0.0.80）：
        ### Result
        "{\\"url\\":\\"...\\",\\"elements\\":[...]}"   ← 外层是引号字符串
        ### Ran Playwright code ...
    """
    raw = str(text or "")
    m = re.search(r"###\s*Result\s*\n(.+?)(?:\n###|\Z)", raw, re.S)
    payload = m.group(1).strip() if m else raw
    if payload.startswith('"') and payload.endswith('"'):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = payload[1:-1]
    m2 = re.search(r"\{.*\}", payload, re.S)
    if not m2:
        return {}
    candidate = m2.group(0)
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
    include_hidden: bool = False,
) -> dict[str, Any]:
    """通过 browser_evaluate 采集行动元素。返回 {url,title,count,elements:[...]}。

    032 US7：include_hidden=True 时保留隐藏元素（用于"存在但不可见"诊断）。
    """
    fn = COLLECT_FN
    if include_hidden:
        fn = fn.replace(".filter((e) => e.visible)", ".filter((e) => true)")
    r = await caller("browser_evaluate", {"function": fn})
    if not isinstance(r, dict) or not r.get("success"):
        return {"url": "", "title": "", "count": 0, "elements": []}
    return _extract_json(str(r.get("text") or ""))


async def diagnose_missing(
    caller: Callable[[str, dict], Awaitable[dict]],
    target: str,
) -> dict[str, Any]:
    """032 US7：目标可见未命中时的诊断（hidden / not_in_dom / unknown）。

    - hidden：目标存在于 DOM 但不可见（命中含隐藏元素的采集，且可见性为 false）
    - not_in_dom：含隐藏元素在内都找不到目标
    - unknown：探针本身失败（不做断言）
    """
    try:
        fn = COLLECT_FN.replace(".filter((e) => e.visible)", ".filter((e) => true)")
        r = await caller("browser_evaluate", {"function": fn})
        if not isinstance(r, dict) or not r.get("success"):
            return {"state": "unknown", "note": ""}
        data = _extract_json(str(r.get("text") or ""))
        if "elements" not in data:
            return {"state": "unknown", "note": ""}
        elements = data.get("elements") or []
        matched = match_target(elements, target)
        hidden = [m for m in matched if not m.get("visible", True)]
        if hidden:
            e = hidden[0]
            loc = e.get("selector") or e.get("name") or target
            return {
                "state": "hidden",
                "note": f"目标存在于 DOM 但不可见（hidden）：{loc}",
            }
        if matched:
            return {
                "state": "visible_only",
                "note": "目标在 DOM 中存在且可见（可能被遮挡或不在视口内）",
            }
        return {"state": "not_in_dom", "note": "目标不在当前 DOM（含隐藏元素）中"}
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "note": ""}


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


# ---------------------------------------------------------------------------
# B4：遮挡 / 视口诊断（点击失败时"为什么点不中"）
# ---------------------------------------------------------------------------

_OCCLUSION_FN = r"""() => {
  const sel = %s;
  let el = null;
  try { el = sel ? document.querySelector(sel) : null; } catch (e) { el = null; }
  if (!el) return JSON.stringify({state: 'not_found'});
  const style = window.getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const inVp = r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw && r.width > 0 && r.height > 0;
  const cx = Math.min(Math.max(r.left + r.width / 2, 0), Math.max(vw - 1, 0));
  const cy = Math.min(Math.max(r.top + r.height / 2, 0), Math.max(vh - 1, 0));
  const top = document.elementFromPoint(cx, cy);
  const info = {
    in_viewport: inVp,
    disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    pointer_events: style.pointerEvents,
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    state: 'ok',
  };
  if (info.disabled) { info.state = 'disabled'; }
  else if (style.pointerEvents === 'none') { info.state = 'pointer_events_none'; }
  else if (!inVp) { info.state = 'off_viewport'; }
  else if (!top) { info.state = 'covered_unknown'; }
  else if (top === el || el.contains(top) || top.contains(el)) { info.state = 'ok'; }
  else {
    info.state = 'covered';
    info.by = {
      tag: top.tagName.toLowerCase(),
      cls: String(top.className || '').slice(0, 80),
      text: String(top.innerText || top.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60),
    };
  }
  return JSON.stringify(info);
}"""

_OCCLUSION_NOTES = {
    "covered": "目标被其他元素遮挡：中心点上实际是 {by}，点击无法命中——先关闭/移开遮挡层（弹窗、遮罩、浮层）或滚动后再操作",
    "off_viewport": "目标存在但不在视口内——先滚动页面让目标进入视口再操作",
    "covered_unknown": "目标中心点没有可命中的元素（可能被透明层拦截或动画中）——等待动画结束或滚动后重试",
    "disabled": "目标处于禁用状态（disabled）——当前不可点击，等待其启用或换其他入口",
    "pointer_events_none": "目标设置了 pointer-events:none（视觉存在但不可交互）——换其他入口或先触发使其可交互",
    "ok": "目标位置无遮挡（可能为瞬时拦截/动画中）——可稍后重试",
}


async def probe_occlusion(
    caller: Callable[[str, dict], Awaitable[dict]],
    target: str = "",
    *,
    selector: str | None = None,
) -> dict[str, Any]:
    """B4：目标存在但动作失败时的遮挡/视口诊断（只读，失败静默）。

    - selector 为 CSS 时直接检查；否则从行动元素集合按名称/文本匹配出 CSS 再检查
      （快照 ref 无法在 JS 中解析，调用方不应传 ref）。
    返回 {state, note, ...}；state ∈ covered / off_viewport / covered_unknown /
    disabled / pointer_events_none / ok / not_in_dom / unknown。
    """
    try:
        sel = (selector or "").strip() or None
        if not sel:
            if not target:
                return {"state": "unknown", "note": ""}
            data = await collect_elements(caller, include_hidden=True)
            matched = match_target(data.get("elements") or [], target)
            if not matched:
                return {"state": "not_in_dom", "note": ""}
            sel = str(matched[0].get("selector") or "").strip() or None
            if not sel:
                return {"state": "unknown", "note": ""}
        r = await caller("browser_evaluate", {"function": _OCCLUSION_FN % json.dumps(sel)})
        if not isinstance(r, dict) or not r.get("success"):
            return {"state": "unknown", "note": ""}
        data = _extract_json(str(r.get("text") or ""))
        state = str(data.get("state") or "unknown")
        note = ""
        if state in _OCCLUSION_NOTES:
            by = data.get("by") or {}
            by_label = ""
            if state == "covered":
                parts = [str(by.get("tag") or "?")]
                if by.get("text"):
                    parts.append(f"「{by['text']}」")
                elif by.get("cls"):
                    parts.append(f".{str(by['cls']).split()[0]}")
                by_label = "".join(parts)
            note = _OCCLUSION_NOTES[state].format(by=by_label)
        return {"state": state, "note": note, **{k: v for k, v in data.items() if k != "state"}}
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "note": ""}


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
