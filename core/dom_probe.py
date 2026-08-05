# core/dom_probe.py
"""Generic DOM probe helpers for nl_goal failure recovery (Cursor-style).

Read-only probing: after a click/fill step misses, the system runs a generic
JS probe over the live DOM, returns visible clickable candidates matching the
step keywords, and feeds a compact summary into the next LLM decide round.
The probe never clicks — clicking is the LLM's decision (evaluate or new ref).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# 动词壳 / 连接词 / 通用词：从步骤描述里剥离，避免污染关键词。
# 「关闭/确定/消息/单位」这类实体词与 placeholder 值必须保留，故不在此列。
_PROBE_STOPWORDS = {
    # 中文动词壳与助词
    "点击", "单击", "双击", "输入", "填写", "填入", "选择", "选中", "勾选",
    "打开", "跳转", "跳转到", "导航", "导航到", "访问", "滚动", "等待",
    "然后", "接着", "再", "并且", "以及", "的", "和", "或", "在", "到",
    "中", "上", "下", "里", "请", "后", "前", "需要", "进行", "完成",
    "结束", "继续", "页面", "当前", "所有", "出现", "按钮", "图标",
    "查看", "显示", "进入", "退出", "确认", "提交",
    # 英文
    "click", "tap", "press", "select", "fill", "type", "input", "enter",
    "open", "goto", "navigate", "the", "and", "to", "in", "on", "for",
    "with", "please", "then", "next", "button", "icon",
}

_VERB_SHELL_RE = re.compile(
    r"^(?:点击|单击|双击|输入|填写|填入|选择|选中|勾选|打开|跳转到|导航到|访问|"
    r"滚动|等待|然后|接着|再|并且|以及|和|并|且|请|需要|进行|完成|结束|继续|"
    r"查看|显示|进入|退出)+"
)

_ENTITY_RE = re.compile(
    r"【([^】]+)】|「([^」]+)」|『([^』]+)』|“([^”]+)”|"
    r'"([^"]+)"|\'([^\']+)\'|（([^）]+)）|\(([^)]+)\)'
)

_CN_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")

# stable_hint 里的定位键值，如 placeholder=请选择单位 / role=button name=登录 / text=zzz
_STABLE_HINT_PARTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"placeholder\s*=\s*([^;\s，,]+)", re.I), "placeholder"),
    (re.compile(r"aria-label\s*=\s*([^;\s，,]+)", re.I), "aria_label"),
    (re.compile(r"role\s*=\s*([a-z_\-]+)", re.I), "role"),
    (re.compile(r"name\s*=\s*([^;\s，,]+)", re.I), "name"),
    (re.compile(r"text\s*=\s*([^;\s，,]+)", re.I), "text"),
    (re.compile(r"label\s*=\s*([^;\s，,]+)", re.I), "label"),
    (re.compile(r"title\s*=\s*([^;\s，,]+)", re.I), "title"),
]

# 通用可点击元素选择器（probe 定位符与 evaluate 点击都基于它）
CLICKABLE_SELECTOR = (
    'button, a, input, select, textarea, [role], [onclick], '
    '[class*="close"], [class*="confirm"], [class*="primary"]'
)

GENERIC_DOM_PROBE_JS = r"""() => {
  const kws = __PROBE_KEYWORDS__;
  const norm = (s) => (s || '').replace(/\s+/g, '').toLowerCase();
  const kwSet = [];
  for (const k of kws) {
    const n = norm(k);
    if (n) kwSet.push(n);
  }
  const CLICKABLE_SEL = '""" + CLICKABLE_SELECTOR + r"""';
  const clickables = Array.from(document.querySelectorAll(CLICKABLE_SEL));
  const visible = (el) => {
    try {
      const st = window.getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    } catch (e) { return false; }
  };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const matches = (el) => {
    const parts = [
      textOf(el),
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('placeholder') || '',
      el.getAttribute('role') || '',
      el.getAttribute('name') || '',
      el.tagName,
    ];
    const blob = norm(parts.join(' '));
    return kwSet.some((k) => blob.includes(k));
  };
  const clickable = (el) => {
    if (el.getAttribute('disabled') != null || el.getAttribute('aria-disabled') === 'true') return false;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (['button', 'a', 'input', 'select', 'textarea'].includes(tag)) return true;
    if (['button', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'checkbox', 'radio', 'tab', 'link', 'switch', 'treeitem'].includes(role)) return true;
    const cls = typeof el.className === 'string' ? el.className : '';
    if (el.onclick) return true;
    if (/close|confirm|primary/i.test(cls)) return true;
    return false;
  };
  const candidates = [];
  const seen = new Set();
  const all = Array.from(document.querySelectorAll('body *'));
  for (const el of all) {
    if (!visible(el)) continue;
    if (!clickable(el)) continue;
    if (seen.has(el)) continue;
    if (!matches(el)) continue;
    seen.add(el);
    const idx = clickables.indexOf(el);
    candidates.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      name: (el.getAttribute('aria-label') || el.getAttribute('title') || ''),
      text: textOf(el).slice(0, 150),
      placeholder: el.getAttribute('placeholder') || '',
      visible: true,
      candidate_index: candidates.length,
      dom_index: idx,
      locator: idx >= 0 ? ('probe_idx_' + idx) : '',
    });
    if (candidates.length >= 20) break;
  }
  return { ok: true, keywords: kws, candidates: candidates };
}"""


def build_probe_js(keywords: list[str] | None) -> str:
    """Serialize keywords into GENERIC_DOM_PROBE_JS for browser_evaluate."""
    return GENERIC_DOM_PROBE_JS.replace(
        "__PROBE_KEYWORDS__",
        json.dumps(list(keywords or []), ensure_ascii=False),
    )


def parse_probe_result_text(text: str | None) -> Optional[dict]:
    """Parse browser_evaluate probe text (JSON) into a dict, tolerant of chatter.

    browser_evaluate appends "### Result" / "### Ran Playwright code" blocks, so
    we extract the FIRST complete JSON object via raw_decode instead of matching
    braces (the trailing JS block can contain braces too).
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # Strip a leading "### Result" / "### Error" marker line if present
    if "\n" in raw:
        first = raw.split("\n", 1)[0].strip()
        if first.startswith("###"):
            raw = raw.split("\n", 1)[1].strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start != -1:
        try:
            data, _ = decoder.raw_decode(raw[start:])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
    return None


def extract_probe_keywords(description: str | None, stable_hint: str | None) -> list[str]:
    """Extract entity keywords from a step description / stable_hint.

    Drops verb shells (点击/输入/选择...) and generic words, keeps entity terms
    like 关闭/确定/京州市院 and placeholder values like 请选择单位. stable_hint
    `placeholder=xxx` / `role=button name=yyy` / `text=zzz` are parsed too.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(k: Any) -> None:
        k = str(k or "").strip()
        if not k or k in seen:
            return
        seen.add(k)
        out.append(k)

    # stable_hint 定位键值优先（placeholder/role/name/text 等）
    for pat, _tag in _STABLE_HINT_PARTS:
        if not stable_hint:
            break
        for m in pat.finditer(stable_hint):
            add(m.group(1))

    desc = (description or "").strip()
    if not desc:
        return out

    # 括号/引号实体整块保留（含 placeholder 值），并补充内部细粒度 token
    for m in _ENTITY_RE.finditer(desc):
        entity = next((g for g in m.groups() if g), "").strip()
        if entity:
            add(entity)
            for tok in _CN_TOKEN_RE.findall(entity):
                if tok != entity:
                    add(_VERB_SHELL_RE.sub("", tok))
    rest = _ENTITY_RE.sub(" ", desc)

    for tok in _CN_TOKEN_RE.findall(rest):
        t = _VERB_SHELL_RE.sub("", tok)
        if t and t not in _PROBE_STOPWORDS:
            add(t)
    for tok in _EN_TOKEN_RE.findall(rest):
        t = tok.lower()
        if t not in _PROBE_STOPWORDS:
            add(t)
    return out


def build_probe_summary(result: dict | None) -> str:
    """Turn a probe result dict into compact text for the LLM."""
    if not isinstance(result, dict):
        return "DOM PROBE found no candidates (probe returned nothing)."
    candidates = result.get("candidates") or []
    kws = result.get("keywords") or []
    lines = [
        f"DOM PROBE found {len(candidates)} visible clickable candidate(s) "
        f"for keywords={kws or []}:",
    ]
    for i, c in enumerate(candidates[:20]):
        tag = c.get("tag") or "?"
        role = c.get("role") or "-"
        name = (c.get("name") or "").strip()
        text = (c.get("text") or "").strip().replace("\n", " ")
        ph = (c.get("placeholder") or "").strip()
        loc = c.get("locator") or c.get("dom_index")
        label = text or name or ph
        lines.append(
            f"[{i + 1}] <{tag}> role={role} locator={loc} label={label[:80]}"
        )
    lines.append(
        "SUGGESTED evaluate click: "
        f"document.querySelectorAll('{CLICKABLE_SELECTOR}')[N].click() "
        "with N = dom_index (locator=probe_idx_N), or find by exact text/aria-label."
    )
    if not candidates:
        lines.append(
            "No visible clickable candidate matched — target may be hidden, in an "
            "iframe, or not loaded yet; wait or status=fail only if the page is stable."
        )
    return "\n".join(lines)
