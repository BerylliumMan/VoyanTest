# core/script_synthesize.py
"""LLM synthesis of Playwright Python from an NL-goal action journal."""
from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from core.goal_agent_loop import (
    REPAIR_SYSTEM_PROMPT,
    is_close_messages_checklist_step,
    journal_entry_covers_checklist,
    parse_checklist_index,
)
from core.locator_candidates import is_ephemeral_locator_ref
from core.replay_resolve import clean_target_name
from core.script_templates import try_build_templated_script

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:python)?\s*|\s*```$", re.I | re.M)

SYNTH_SYSTEM = """You write a durable Playwright async Python script from a successful browser action journal.

The script is replayed on the live app WITHOUT a model — every locator must be
deterministic. The CHECKLIST is the source of truth for WHAT to do. Prefer each
journal entry's ``replay`` field:
  - ``playwright_locator`` (codegen string, e.g. get_by_role("button", name="登录")) FIRST
  - then strategy / placeholder / exact_text / value
NEVER invent project-specific tree CSS (.treeSelect_div, .el-tree-node__label, …).
NEVER use ephemeral snapshot refs (e12, f5e21, probe_idx_N).
NEVER copy intermediate/failed journal values (typos, parent tree nodes).

Requirements:
1. Output ONLY Python source — no markdown fences, no commentary.
2. Define exactly: async def test_case_{case_id}(page) -> None:
   First lines inside the function MUST set global timeouts:
     page.set_default_timeout(30000)
     page.set_default_navigation_timeout(60000)
   Rely on Playwright auto-wait. NEVER use page.wait_for_timeout / locator.wait_for /
   explicit sleeps. After login you may use: await expect(page).to_have_url(...)
3. For each successful journal step with playwright_locator, emit:
   await page.<playwright_locator>.first.click() / .fill(value) / .press_sequentially(value)
   (strip a leading page. if present). Use checklist ``value`` for fills.
4. If playwright_locator is missing, use get_by_placeholder / get_by_role / get_by_text(exact=True).
5. ALWAYS disambiguate duplicates with .first (or .filter(visible=True) when needed).
6. Filter/typeahead: AFTER opening a unit/org picker, first
   await expect(page.get_by_role('treeitem').first).to_be_attached()
   THEN type the filter — early keystrokes are ignored once the list loads.
   Prefer press_sequentially; select via get_by_role('treeitem', name=...).
7. Option clicks: get_by_role('treeitem', name=exact checklist text).
8. Close overlays: loop visible dialogs → footer 关闭 / header close; then notifications.
   NEVER click 消息铃铛 / 去查看.
9. page.goto(base_url) at start when base_url is provided.
10. Import re / expect only when used. NEVER use wait_for_timeout / locator.wait_for.
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


def _step_intent_text(step_rec: dict[str, Any] | None) -> str:
    """Authoritative intent for a checklist step (description + structured hints)."""
    if not step_rec:
        return ""
    desc = str(
        step_rec.get("description") or step_rec.get("original_description") or ""
    ).strip()
    st = step_rec.get("structured_step") if isinstance(step_rec.get("structured_step"), dict) else {}
    bits: list[str] = []
    if desc:
        bits.append(desc)
    if st.get("value") is not None and str(st.get("value")).strip():
        bits.append(f"value={st['value']}")
    if st.get("target_name"):
        bits.append(f"target_name={st['target_name']}")
    if st.get("selector"):
        bits.append(f"selector_hint={st['selector']}")
    return " | ".join(bits)


def _checklist_for_synth(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, s in enumerate(steps or []):
        desc = str(
            s.get("description") or s.get("original_description") or ""
        ).strip()
        if not desc:
            continue
        out.append(
            {
                "order": int(s.get("step_order") or s.get("step_number") or i + 1),
                "description": desc,
                "intent": _step_intent_text(s),
            }
        )
    return out


def sanitize_journal_for_synth(
    journal: list[dict[str, Any]] | None,
    steps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep only success entries that truly advance a checklist step.

    Per checklist index only the LAST successful cover is kept, and each kept
    entry carries an ``intent`` derived from the step description — so wrong
    intermediate journal values (test10, 汉东省院, …) never reach the LLM.
    """
    by_order: dict[int, dict[str, Any]] = {}
    for i, s in enumerate(steps or []):
        o = int(s.get("step_order") or s.get("step_number") or i + 1)
        by_order[o] = s

    last_cover: dict[int, dict[str, Any]] = {}
    dropped_noise = 0
    for e in journal or []:
        idx = parse_checklist_index(
            e.get("checklist_note"),
            explicit=e.get("checklist_index"),
        )
        if idx is None or not e.get("success"):
            dropped_noise += 1
            continue
        step_rec = by_order.get(idx)
        desc = str(
            (step_rec or {}).get("description")
            or (step_rec or {}).get("original_description")
            or e.get("checklist_note")
            or ""
        )
        if not journal_entry_covers_checklist(e, step_description=desc):
            dropped_noise += 1
            continue
        item = dict(e)
        item["intent"] = _step_intent_text(step_rec)
        last_cover[idx] = item  # last successful cover wins

    cleaned = [last_cover[k] for k in sorted(last_cover)]
    if dropped_noise:
        logger.info(
            "journal sanitized for synth: kept=%s dropped_noise=%s",
            len(cleaned),
            dropped_noise,
        )
    return cleaned


# UI field labels that appear in 【…】 but are not the value the script must type/click.
_FIELD_LABEL_SKIP = re.compile(
    r"^(?:单位选择|请选择单位|用户名|请输入用户名|密码|请输入密码|"
    r"关键词|筛选|搜索|输入框|下拉框|文本框|按钮)$"
)


def extract_required_targets(steps: list[dict[str, Any]] | None) -> list[str]:
    """Target strings a synthesized script must reference (click/fill intents).

    Prefer structured ``value`` (what to type/select). Fall back to bracket
    labels / target_name only when they look like real option/button text —
    not field labels like 用户名 / 单位选择. Close-message steps are excluded.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(v: str) -> None:
        v = clean_target_name(v)
        if not v or len(v) < 2 or v in seen:
            return
        if re.fullmatch(r"(?:点击|输入|选择|打开|关闭|提交|确认|确定|取消)", v):
            return
        if _FIELD_LABEL_SKIP.match(v):
            return
        seen.add(v)
        out.append(v)

    for s in steps or []:
        desc = str(s.get("description") or s.get("original_description") or "")
        if is_close_messages_checklist_step(desc):
            continue
        st = s.get("structured_step") if isinstance(s.get("structured_step"), dict) else {}
        # Prefer value (typed/selected content) as the must-appear string
        if st.get("value") is not None and str(st.get("value")).strip():
            val = str(st["value"]).strip()
            # Full URLs are navigation intents; scripts may wait_for_url / use origin
            if not re.match(r"https?://", val, re.I):
                _add(val)
            # Also keep button labels like 登录 when value is absent-ish
            continue
        # No value → use 【…】 labels and target_name (e.g. 登录 button)
        for m in re.finditer(r"【([^】]+)】", desc):
            _add(m.group(1))
        if st.get("target_name"):
            _add(str(st["target_name"]))
        # Bare text after 选择/输入 without brackets, e.g. "选择京州市院"
        m = re.search(r"(?:选择|输入|点击)\s*([^\s【】，,。；;]+)\s*$", desc)
        if m:
            _add(m.group(1))
    return out


def check_script_covers_intents(
    script: str,
    steps: list[dict[str, Any]] | None,
) -> list[str]:
    """Return required target strings missing from the script (empty = ok)."""
    required = extract_required_targets(steps)
    if not required:
        return []
    body = script or ""
    return [t for t in required if t not in body]


# 选择器类 API：参数就是 Playwright selector，绝不能是快照 ref
_SELECTOR_CALL_RE = re.compile(
    r"""\.(?:locator|wait_for_selector|query_selector|frame_locator|
          get_by_role|get_by_text|get_by_label|get_by_placeholder|get_by_title|
          get_by_alt_text|get_by_test_id)\s*\(\s*(['"])(.*?)\1""",
    re.X,
)
# 无参调用选择器 API：Frame.wait_for_selector() missing 1 required positional argument
_EMPTY_SELECTOR_CALL_RE = re.compile(
    r"""\.(?:locator|wait_for_selector|query_selector)\s*\(\s*\)"""
)
# 提示词明令禁止的固定等待（慢 + 抖动），仅告警不拒绝
_FORBIDDEN_WAIT_RE = re.compile(r"""\.(?:wait_for_timeout)\s*\(|\.wait_for\s*\(""")


def check_script_sanity(script: str) -> list[str]:
    """Return hard problems that make the synthesized script unrunnable.

    这些是「必然失败」而非「风格问题」，命中即拒绝入库：坏脚本一旦被固化，
    下次执行会优先直跑它（compiled_script first），白白超时后才回退 nl_goal。

    覆盖的真实线上失败：
    - ``waiting for locator("e3")`` / ``locator("f3e24")``：快照 ref 被当成 CSS
    - ``Frame.wait_for_selector() missing 1 required positional argument``
    - ``frame`` 变量未定义（LLM 凭想象引入 iframe 对象）
    """
    body = script or ""
    problems: list[str] = []

    try:
        tree = ast.parse(body)
    except SyntaxError as exc:
        return [f"script is not valid Python: {exc}"]

    for m in _SELECTOR_CALL_RE.finditer(body):
        raw = m.group(2).strip()
        if is_ephemeral_locator_ref(raw):
            problems.append(f"ephemeral snapshot ref used as a locator: {raw!r}")

    if _EMPTY_SELECTOR_CALL_RE.search(body):
        problems.append("selector API called without a selector")

    # 未定义的 frame / self.frame 变量
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned.add(node.id)
        elif isinstance(node, ast.arg):
            assigned.add(node.arg)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == "frame"
            and "frame" not in assigned
        ):
            problems.append("undefined variable 'frame' referenced")
            break

    return problems


def check_script_warnings(script: str) -> list[str]:
    """Non-fatal smells worth logging (fixed sleeps defeat Playwright auto-wait)."""
    if _FORBIDDEN_WAIT_RE.search(script or ""):
        return ["script uses fixed waits (wait_for_timeout / locator.wait_for)"]
    return []


_UNICODE_LOOKALIKES: tuple[tuple[str, str], ...] = (
    ("→", "->"), ("⇒", "=>"), ("➜", "->"), ("⟶", "->"), ("←", "<-"),
    ("：", ":"), ("，", ","), ("。", "."), ("、", ","),
    ("（", "("), ("）", ")"), ("【", "["), ("】", "]"),
    ("「", '"'), ("」", '"'), ("『", '"'), ("』", '"'),
    ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"),
    ("…", "..."), ("——", "--"), ("—", "-"), ("–", "-"), ("−", "-"),
    ("～", "~"),
    ("％", "%"), ("＋", "+"), ("－", "-"), ("＝", "="),
    ("！", "!"), ("？", "?"), ("；", ";"),
)


def repair_unicode_lookalikes(script: str) -> str:
    """Replace unicode punctuation/arrows with ASCII equivalents.

    Qwen 常在代码里混入全角标点与 → 箭头，导致 ast 解析失败。
    只改代码区：字符串字面值与注释原样保留（字面值可能是覆盖门要验的
    目标，注释本就不影响解析）。修完后调用方仍重跑覆盖+sanity 双门。
    """
    lines: list[str] = []
    for line in (script or "").splitlines():
        lines.append(_repair_code_span(line))
    repaired = _strip_trailing_arrow_annotations("\n".join(lines))
    return _strip_leading_prose(repaired)


def _repair_code_span(line: str) -> str:
    """Apply the lookalike map to the code part of one line only."""
    code_end = _code_span_end(line)
    head, tail = line[:code_end], line[code_end:]
    for src, dst in _UNICODE_LOOKALIKES:
        if src in head:
            head = head.replace(src, dst)
    return head + tail


def _code_spans(line: str) -> list[tuple[int, int]]:
    """Ranges of one line that are real code (not strings/comments)."""
    spans: list[tuple[int, int]] = []
    start = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if line.startswith('"""', i) or line.startswith("'''", i):
            spans.append((start, i))
            q = line[i]
            i += 3
            while i < n and not line.startswith(q * 3, i):
                i += 2 if line[i] == "\\" else 1
            i += 3
            start = i
            continue
        if ch in ("'", '"'):
            spans.append((start, i))
            q = ch
            i += 1
            while i < n and line[i] != q:
                i += 2 if line[i] == "\\" else 1
            i += 1
            start = i
            continue
        if ch == "#":
            spans.append((start, i))
            return [(a, b) for a, b in spans if a < b]
        i += 1
    spans.append((start, n))
    return [(a, b) for a, b in spans if a < b]


def _repair_code_span(line: str) -> str:
    """Apply the lookalike map to code spans of one line only."""
    chars = list(line)
    for a, b in _code_spans(line):
        seg = "".join(chars[a:b])
        for src, dst in _UNICODE_LOOKALIKES:
            if src in seg:
                seg = seg.replace(src, dst)
        chars[a:b] = list(seg)
    return "".join(chars)


_CODE_START_PREFIXES = (
    "import ", "from ", "async ", "def ", "#", "@", '"""', "'''",
)


def _strip_leading_prose(body: str) -> str:
    """Drop LLM preamble lines before the first code/comment line.

    模型偶尔在代码块首行写标题（如 `— 登录脚本`），字符替换救不了
    非注释散文行，直接删到第一个代码行。
    """
    lines = (body or "").splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith(_CODE_START_PREFIXES):
            break
        i += 1
    return "\n".join(lines[i:]) if i else body


def _strip_trailing_arrow_annotations(body: str) -> str:
    """Cut `-> comment` tails the model appends after real statements.

    `->` 只在 def 行合法；其他行的 `->`（字符串内除外）必是伪代码批注，
    截掉箭头及之后内容，保留前面真实语句。
    """
    out: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(("#", "def ", "async def ", "class ", "@"))
            or "->" not in line
        ):
            out.append(line)
            continue
        cut = _arrow_outside_string(line)
        out.append(line[:cut].rstrip() if cut is not None else line)
    return "\n".join(out)


def _arrow_outside_string(line: str) -> int | None:
    """Return index of `->` outside string literals, else None."""
    quote: str | None = None
    triple = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote is None:
            if line.startswith('"""', i) or line.startswith("'''", i):
                quote = line[i]
                triple = True
                i += 3
                continue
            if ch in ("'", '"'):
                quote = ch
                triple = False
                i += 1
                continue
            if line.startswith("->", i):
                return i
        else:
            if ch == "\\":
                i += 2
                continue
            if triple and line.startswith(quote * 3, i):
                quote = None
                triple = False
                i += 3
                continue
            if not triple and ch == quote:
                quote = None
        i += 1
    return None


def _accept_synthesized_script(
    script: str,
    *,
    case_id: int | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> str:
    """Reject unrunnable scripts so a broken locator never gets persisted."""
    problems = check_script_sanity(script)
    if problems and any("not valid Python" in p for p in problems):
        repaired = repair_unicode_lookalikes(script)
        if repaired != script:
            logger.info(
                "synth script repaired unicode lookalikes case=%s", case_id
            )
            problems = check_script_sanity(repaired)
            if not problems and steps is not None:
                missing = check_script_covers_intents(repaired, steps)
                if missing:
                    problems = [
                        "repair broke required literals: " + ", ".join(missing)
                    ]
            if not problems:
                script = repaired
    if problems:
        logger.warning(
            "synthesized script rejected case=%s problems=%s",
            case_id, problems,
        )
        raise ValueError(
            "synthesized script failed sanity check: " + "; ".join(problems)
        )
    for warning in check_script_warnings(script):
        logger.warning("synthesized script smell case=%s: %s", case_id, warning)
    return script


async def synthesize_playwright_script(
    *,
    client: AsyncOpenAI,
    model: str,
    case_id: int,
    case_name: str,
    goal_text: str,
    journal: list[dict[str, Any]],
    steps: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> str:
    """Generate Playwright Python from successful journal (template-first)."""
    checklist = _checklist_for_synth(steps)
    journal_clean = sanitize_journal_for_synth(journal, steps)

    templated = try_build_templated_script(
        case_id=int(case_id),
        steps=steps,
        base_url=base_url,
        journal=journal_clean,
    )
    if templated:
        missing_t = check_script_covers_intents(templated, steps)
        if not missing_t:
            script = harden_locators_with_first(
                _ensure_entrypoint(templated, int(case_id))
            )
            script = _accept_synthesized_script(script, case_id=case_id, steps=steps)
            logger.info(
                "synthesized script from templates case=%s bytes=%s",
                case_id,
                len(script),
            )
            return script
        logger.info(
            "templated script incomplete targets=%s — falling back to LLM case=%s",
            missing_t,
            case_id,
        )

    payload = {
        "case_id": int(case_id),
        "case_name": case_name,
        "base_url": base_url or "",
        "goal": goal_text,
        "checklist": checklist,
        "journal_clean": journal_clean,
        "template_hint": (templated[:4000] if templated else None),
    }
    required_literals = extract_required_targets(steps)
    literal_rule = (
        f"HARD RULE: 以下字面值必须原样出现在代码中（缺一不可）："
        f"{required_literals}\n" if required_literals else ""
    )
    user = (
        f"Synthesize async Playwright script for case_id={int(case_id)}.\n"
        f"Function name MUST be: async def test_case_{int(case_id)}(page)\n"
        f"The CHECKLIST is the source of truth — every action must fulfill it.\n"
        f"Prefer journal_clean[].replay strategies; NEVER emit snapshot refs.\n"
        f"Use ASCII punctuation only in code (no → ， ： （ ） 「 」).\n"
        f"{literal_rule}\n"
        f"CONTEXT JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )
    # 思考模式保持开启（调用方显式需要推理能力）；解析经共享提取器
    # 兼容思考字段（content 为空时回退 reasoning），只加鲁棒性不减能力。
    from core.precondition import _response_text
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=8192,
    )
    raw_message = resp.choices[0].message
    script = _strip_fences(_response_text(raw_message))
    if not script or "async def" not in script:
        logger.warning(
            "synth response unparseable content_len=%s reasoning_len=%s",
            len(getattr(raw_message, "content", None) or ""),
            len(getattr(raw_message, "reasoning_content", None) or ""),
        )
    if not script or "async def" not in script:
        raise ValueError("LLM returned empty/invalid Playwright script")
    script = harden_locators_with_first(_ensure_entrypoint(script, int(case_id)))
    missing = check_script_covers_intents(script, steps)
    if missing:
        logger.warning(
            "synthesized script missing required targets %s case=%s", missing, case_id
        )
        raise ValueError(
            "synthesized script missing required targets: " + ", ".join(missing)
        )
    return _accept_synthesized_script(script, case_id=case_id, steps=steps)


async def repair_playwright_script(
    *,
    client: AsyncOpenAI,
    model: str,
    case_id: int,
    script: str,
    error: str,
    journal: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    temperature: float = 0.1,
) -> str:
    """One-shot repair after dry-run failure (checklist still the source of truth)."""
    checklist = _checklist_for_synth(steps)
    user = (
        f"case_id={int(case_id)}\n"
        f"DRY-RUN ERROR:\n{error}\n\n"
        f"CURRENT SCRIPT:\n{script}\n\n"
        f"CHECKLIST (source of truth):\n{json.dumps(checklist, ensure_ascii=False, indent=2)}\n\n"
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
    from core.precondition import _response_text as _rt
    fixed = _strip_fences(_rt(resp.choices[0].message))
    if not fixed or "async def" not in fixed:
        raise ValueError("LLM repair returned empty/invalid script")
    fixed = harden_locators_with_first(_ensure_entrypoint(fixed, int(case_id)))
    missing = check_script_covers_intents(fixed, steps)
    if missing:
        raise ValueError(
            "repaired script missing required targets: " + ", ".join(missing)
        )
    return _accept_synthesized_script(fixed, case_id=case_id, steps=steps)
