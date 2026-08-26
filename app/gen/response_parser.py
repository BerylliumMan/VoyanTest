"""Parse model responses into FunctionalPoint / TestCase dataclasses.

The model is asked to emit two sections separated by markdown headers:
- ``## 功能点清单`` (functional points)
- ``## 测试用例`` (test cases, as a 7-column markdown table)

As a fallback, the parser also accepts JSON format responses (``{"function_points": [...]}``)
and ``{"test_cases": [...]}`` which some model versions prefer over markdown tables.
"""

import json
import logging
import re

from app.gen.models import AnalysisSession, FunctionalPoint, TestCase

logger = logging.getLogger(__name__)


def _clean_text(value: str) -> str:
    value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    # Match both "1. step 2. step" and "1.步骤 2.步骤" (Chinese has no space after dot)
    value = re.sub(r"(\s+)(\d+\.)", r"\n\2", value)
    return value


def _to_html(value: str) -> str:
    return value.replace("\n", "<br>")


def _parse_response(session: AnalysisSession, text: str):
    fp_section = text.split("## 功能点清单")[-1].split("## 测试用例")[0] if "## 功能点清单" in text else ""
    if "## 测试用例" in text:
        tc_section = text.split("## 测试用例")[-1]
    else:
        # Fallback: use entire text if no section marker found
        tc_section = text

    fp_lines = fp_section.strip().split("\n")
    fp_id = 0
    for i, line in enumerate(fp_lines):
        stripped = line.strip()
        # Only match top-level FP lines that contain 【模块名】 prefix
        # This avoids parsing sub-items like "- **交互规则**: ..." as separate FPs
        if (stripped.startswith("- **") or stripped.startswith("* **")) and "【" in stripped:
            name = re.sub(r"^[-*]\s*\*\*([^*]+)\*\*.*", r"\1", stripped).strip()
            desc = re.sub(r"^[-*]\s*\*\*[^*]+\*\*\s*[:：]?\s*", "", stripped).strip()
            cat = "通用"
            module = ""
            # Extract module name from 【】prefix and strip it from name
            if "【" in name and "】" in name:
                m = re.search(r"【([^】]*)】", name)
                if m:
                    module = m.group(1).strip()
                    name = re.sub(r"^【[^】]*】\s*", "", name).strip()
            if "(" in name and ")" in name:
                parts = name.split("(")
                name = parts[0].strip()
                cat = parts[1].rstrip(")").strip()
            fp_id += 1
            session.functional_points.append(FunctionalPoint(
                id=fp_id,
                session_id=session.session_id,
                module=module or "通用",
                name=name,
                description=_clean_text(desc),
                category=cat,
            ))

    import re as _re

    tc_lines = tc_section.strip().split("\n")
    tc_index = 0
    for line in tc_lines:
        line = line.strip()
        if line.startswith("|") and line.count("|") >= 7:
            cells = [c.strip() for c in line.split("|")]
            cells = [c for c in cells if c]
            if len(cells) < 7:
                continue
            # Skip header and separator lines
            if cells[0] == "用例ID":
                continue
            # Skip markdown table separator lines like ---, :---, :-:, etc.
            if _re.match(r"^:?-+:?$", cells[0]):
                continue
            tc_index += 1
            session.test_cases.append(TestCase(
                test_case_id=f"TC-{tc_index:03d}",
                session_id=session.session_id,
                module=_clean_text(cells[1]) if len(cells) > 1 else "",
                title=_clean_text(cells[2]) if len(cells) > 2 else "",
                preconditions=_clean_text(cells[3]) if len(cells) > 3 else "",
                test_steps=_clean_text(cells[4]) if len(cells) > 4 else "",
                expected_result=_clean_text(cells[5]) if len(cells) > 5 else "",
                priority=_clean_text(cells[6]) if len(cells) > 6 else "中",
            ))


# Two-phase pipeline helper functions


def _extract_json(text: str) -> str | None:
    """Extract JSON content from text, handling markdown code blocks and surrounding text."""
    # First try to find ```json ... ``` blocks
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("[") or candidate.startswith("{"):
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(candidate)
                return json.dumps(obj, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                pass
    # Truncated fence without closing ```
    m2 = re.search(r"```(?:json)?\s*\n?([\[{].*)", text, re.DOTALL)
    if m2:
        candidate = re.sub(r"\n?```\s*$", "", m2.group(1)).strip()
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(candidate)
            return json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass
    # No code block found or content inside block not JSON, scan raw text
    cleaned = text.strip()
    brace = cleaned.find("{")
    bracket = cleaned.find("[")
    start = -1
    if brace >= 0 and bracket >= 0:
        start = min(brace, bracket)
    elif brace >= 0:
        start = brace
    elif bracket >= 0:
        start = bracket
    if start >= 0:
        candidate = cleaned[start:].strip()
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(candidate)
            return json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _iter_complete_json_objects(array_body: str) -> list[dict]:
    """Extract complete ``{...}`` objects from a (possibly truncated) JSON array body."""
    objects: list[dict] = []
    i = 0
    n = len(array_body)
    while i < n:
        while i < n and array_body[i] != "{":
            i += 1
        if i >= n:
            break
        start = i
        depth = 0
        in_str = False
        escape = False
        ok = False
        while i < n:
            ch = array_body[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        ok = True
                        i += 1
                        break
            i += 1
        if not ok:
            break
        snippet = array_body[start:i]
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict):
                objects.append(obj)
        except (json.JSONDecodeError, ValueError):
            continue
    return objects


def looks_truncated_fp_output(text: str) -> bool:
    """Heuristic: model output cut off mid-JSON / mid-fence."""
    if not text or not text.strip():
        return False
    t = text.strip()
    if t.startswith("```") and t.count("```") < 2:
        return True
    if '"functional_points"' in t or '"function_points"' in t or t.lstrip().startswith("["):
        # Unbalanced braces/brackets often mean truncation
        if t.count("{") > t.count("}"):
            return True
        if t.count("[") > t.count("]"):
            return True
        if re.search(r'[,:\[]\s*$', t):
            return True
        if re.search(r'"\s*:\s*"[^"]*$', t):
            return True
    return False


def _salvage_fp_items_from_truncated(text: str) -> list[dict]:
    """Best-effort recovery of FP dicts when model output is truncated mid-JSON."""
    if not text or not text.strip():
        return []
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    key_match = re.search(
        r'"(?:functional_points|function_points|fp_list|功能点列表)"\s*:\s*\[',
        cleaned,
    )
    if key_match:
        items = _iter_complete_json_objects(cleaned[key_match.end():])
        if items:
            logger.info("Salvaged %d FP objects from truncated JSON object", len(items))
            return items

    bracket = cleaned.find("[")
    if bracket >= 0:
        items = _iter_complete_json_objects(cleaned[bracket + 1:])
        if items and any(
            isinstance(it, dict) and (it.get("name") or it.get("title") or it.get("功能点"))
            for it in items
        ):
            logger.info("Salvaged %d FP objects from truncated JSON array", len(items))
            return items
    return []


def _normalize_fp_item(item: dict) -> dict:
    """Normalize FP item field names (handle camelCase, snake_case, Chinese)."""
    normalized = {}
    field_map = {
        "module": ["module", "module_name", "模块", "所属模块", "模块名"],
        "name": ["name", "function_name", "functionname", "title", "功能点标题",
                 "功能点", "功能名称", "标题", "名称"],
        "description": ["description", "desc", "功能描述", "描述", "说明"],
        "category": ["category", "cat", "分类"],
        "priority": ["priority", "pri", "优先级"],
    }
    for target, candidates in field_map.items():
        for c in candidates:
            val = item.get(c)
            if val is None:
                # case-insensitive key match
                for k, v in item.items():
                    if str(k).strip().lower() == c.lower() and v:
                        val = v
                        break
            if val:
                normalized[target] = str(val).strip()
                break
    return normalized


def _split_md_table_row(line: str) -> list[str]:
    """Split a markdown table row into cells (strip leading/trailing empty pipes)."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [c.strip() for c in cells]


def _is_md_separator_row(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(re.match(r"^:?-+:?$", c.replace(" ", "")) for c in cells if c)


def _map_fp_header_indexes(headers: list[str]) -> dict[str, int]:
    """Map common Chinese/English FP table headers to field indexes."""
    mapping: dict[str, int] = {}
    aliases = {
        "name": ("功能点标题", "功能点", "功能名称", "标题", "名称", "name", "title", "function"),
        "description": ("功能描述", "描述", "说明", "description", "desc"),
        "module": ("模块", "所属模块", "模块名", "module"),
        "priority": ("优先级", "priority", "pri"),
        "category": ("分类", "category", "cat"),
    }
    for i, h in enumerate(headers):
        h_norm = re.sub(r"\s+", "", h.lower())
        for field, names in aliases.items():
            if field in mapping:
                continue
            for n in names:
                if re.sub(r"\s+", "", n.lower()) in h_norm or h_norm in re.sub(r"\s+", "", n.lower()):
                    mapping[field] = i
                    break
    return mapping


def _parse_fps_from_markdown_tables(text: str, session_id: str = "") -> list[FunctionalPoint]:
    """Parse FPs from markdown tables under module headings.

    Supports outputs like::

        ## 用户管理模块
        | 序号 | 功能点标题 | 功能描述 | 优先级 |
        | 1 | 用户注册 | ... | P0 |
    """
    fps: list[FunctionalPoint] = []
    current_module = "通用"
    header_map: dict[str, int] | None = None
    skip_generic = {"功能点列表", "功能点清单", "概述", "目录", "说明"}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        heading = re.match(r"^#{1,4}\s+(.+)$", line)
        if heading:
            title = heading.group(1).strip()
            title = re.sub(r"^[\d.、]+\s*", "", title)
            title = re.sub(r"[（(].*?[）)]\s*$", "", title).strip()
            # "用户管理模块" / "P0 优先级" — keep module-like headings
            if title and title not in skip_generic and "优先级" not in title:
                current_module = title.replace("模块", "").strip() or title
            header_map = None
            continue

        if not line.startswith("|"):
            header_map = None
            continue

        cells = _split_md_table_row(line)
        if not cells or _is_md_separator_row(cells):
            continue

        # Header row
        if header_map is None:
            mapped = _map_fp_header_indexes(cells)
            if "name" in mapped or "description" in mapped:
                header_map = mapped
            continue

        name_idx = header_map.get("name")
        desc_idx = header_map.get("description")
        mod_idx = header_map.get("module")
        cat_idx = header_map.get("category")

        # Skip pure index-only rows without name/desc
        name = cells[name_idx].strip() if name_idx is not None and name_idx < len(cells) else ""
        desc = cells[desc_idx].strip() if desc_idx is not None and desc_idx < len(cells) else ""
        if not name and not desc:
            continue
        # If only one text column exists, treat first non-index cell as name
        if not name:
            for c in cells:
                if c and not re.match(r"^\d+$", c) and c.upper() not in ("P0", "P1", "P2", "P3"):
                    name = c
                    break
        if not name:
            continue
        # Skip header-like repeated rows
        if name in ("功能点标题", "标题", "名称", "功能点"):
            continue

        module = current_module
        if mod_idx is not None and mod_idx < len(cells) and cells[mod_idx].strip():
            module = cells[mod_idx].strip()
        category = "通用"
        if cat_idx is not None and cat_idx < len(cells) and cells[cat_idx].strip():
            category = cells[cat_idx].strip()

        fps.append(FunctionalPoint(
            id=len(fps) + 1,
            session_id=session_id,
            module=module or "通用",
            name=name,
            description=_clean_text(desc),
            category=category,
        ))

    return fps


def _fps_from_item_dicts(items: list, session_id: str = "") -> list[FunctionalPoint]:
    fps: list[FunctionalPoint] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        fp = _normalize_fp_item(item)
        if not fp.get("name"):
            continue
        fps.append(FunctionalPoint(
            id=i + 1, session_id=session_id,
            module=fp.get("module", ""),
            name=fp["name"],
            description=fp.get("description", ""),
            category=fp.get("category", "通用"),
        ))
    return fps


def _parse_fps_from_text(text: str, session_id: str = "") -> list[FunctionalPoint]:
    """Parse functional points — JSON, truncated salvage, then markdown."""
    json_str = _extract_json(text)
    if json_str:
        try:
            data = json.loads(json_str)
            items = data if isinstance(data, list) else (
                data.get("function_points") or data.get("functional_points")
                or data.get("fp_extract") or data.get("fp_list")
                or data.get("功能点列表") or [])
            fps = _fps_from_item_dicts(items, session_id=session_id)
            if fps:
                return fps
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    salvaged = _salvage_fp_items_from_truncated(text)
    if salvaged:
        fps = _fps_from_item_dicts(salvaged, session_id=session_id)
        if fps:
            return fps

    # Markdown tables under module headings (common minimax / zen output)
    table_fps = _parse_fps_from_markdown_tables(text, session_id=session_id)
    if table_fps:
        return table_fps

    # Fallback to bullet markdown parsing (- **【模块】名称**)
    tmp = AnalysisSession()
    tmp.session_id = session_id
    _parse_response(tmp, text)
    return tmp.functional_points


def _coerce_text_field(value) -> str:
    """Normalize LLM output fields to plain text for TestCase storage.

    Lists become newline-delimited ``1. xxx`` items (space after marker) so that
    later splitting won't break on numeric values like ``2.0``.
    Empty list items are dropped here; callers that need index alignment should
    use ``_listify_field`` + ``align_expected_to_steps`` first.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for v in value:
            p = str(v).strip()
            if not p:
                continue
            p = re.sub(r"^\d+[\.、]\s*", "", p)
            parts.append(f"{len(parts) + 1}. {p}")
        return "\n".join(parts)
    return str(value).strip()


def _listify_field(value) -> list[str]:
    """Convert steps/expected field to a list of plain strings (keep empties)."""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            p = str(v).strip() if v is not None else ""
            p = re.sub(r"^\d+[\.、]\s*", "", p)
            # Bare leftover markers / pure numbering junk → empty
            if re.fullmatch(r"(?:\d+[\.、]\s*)*", p or ""):
                p = ""
            out.append(p)
        return out
    from app.gen.adapter import split_numbered_items
    return split_numbered_items(str(value))


def _to_numbered_text(parts: list[str]) -> str:
    """Join parts as ``1. xxx`` lines; keep empty slots for index alignment."""
    return "\n".join(f"{i + 1}. {p}" for i, p in enumerate(parts))


def _to_numbered_expected(parts: list[str]) -> str:
    """Serialize expected results; omit empty slots (no bare ``1. 2. 3.`` junk).

    Non-empty items keep their 1-based step index so import can right-align /
    index-map correctly. All-empty → ``\"\"``.
    """
    lines = []
    for i, p in enumerate(parts):
        body = (p or "").strip()
        if not body:
            continue
        # Drop items that are still only numbering after cleanup
        if re.fullmatch(r"(?:\d+[\.、]\s*)+", body):
            continue
        lines.append(f"{i + 1}. {body}")
    return "\n".join(lines)


# Re-export shared normalize helpers (tests import these private names)
from core.step_normalize import (  # noqa: E402
    expand_compound_ui_step as _expand_compound_ui_step,
    sanitize_brackets_ellipsis as _sanitize_brackets_ellipsis,
    sanitize_ui_step as _sanitize_ui_step,
    strip_ellipsis_in_label as _strip_ellipsis_in_bracket_label,
    coerce_structured_step,
    expand_structured_compounds,
    parse_instant_to_structured,
    render_structured_step,
)


def _sanitize_tc_title(title: str) -> str:
    """Strip trailing scenario-type tags from titles (e.g. 「…-正常」)."""
    t = (title or "").strip()
    if not t:
        return t
    # Repeatedly strip common suffixes: -正常 / —异常流程 / _边界场景
    pattern = (
        r"(?:[-—–_／/]\s*)"
        r"(?:正常|异常|边界|组合)"
        r"(?:流程|场景|用例|测试)?"
        r"(?:流程|场景)?"
        r"\s*$"
    )
    prev = None
    while prev != t:
        prev = t
        t = re.sub(pattern, "", t).strip()
    return t or (title or "").strip()


_DESC_VALUE_PAT = re.compile(r"^(输入|点击|选择|在|填写|勾选)|一个|某个|任意")


_QUOTED_VALUE = re.compile(r"[「『\u300c]([^」』\u300d]+)[」』\u300d]")


def sanitize_structured_value(step: dict) -> dict:
    """027-e2e-fixes: value 错位净化。

    1) value 含「」引号 → 提取引号内具体值（LLM 常写成 `框输入「X」`）
    2) 否则超长(>40)或描述句式 → 清空
    """
    v = step.get("value")
    if isinstance(v, str) and v.strip():
        m = _QUOTED_VALUE.search(v)
        if m:
            step = dict(step)
            step["value"] = m.group(1).strip()
            return step
        if len(v) > 40 or _DESC_VALUE_PAT.search(v):
            step = dict(step)
            step["value"] = ""
    return step


def _normalize_tc_item(item: dict) -> dict:
    """Normalize TC field names (handle Chinese, camelCase, snake_case)."""
    from app.gen.adapter import align_expected_to_steps

    normalized = {}
    field_map = {
        "module":          ["module", "module_name", "所属模块", "模块", "module_name"],
        "title":           ["title", "name", "test_name", "用例标题", "测试标题", "标题",
                            "case_name", "caseName", "featureName", "feature_name", "functionName"],
        "preconditions":   ["preconditions", "precondition", "前置条件"],
        "priority":        ["priority", "pri", "优先级", "level"],
    }
    for target, candidates in field_map.items():
        for c in candidates:
            val = item.get(c)
            if val:
                normalized[target] = (
                    _coerce_text_field(val) if target == "preconditions" else val
                )
                break

    if normalized.get("title"):
        normalized["title"] = _sanitize_tc_title(str(normalized["title"]))

    # Pair steps/expected with right-align so shorter expected maps to trailing steps
    steps_raw = None
    expected_raw = None
    for c in (
        "test_steps", "testSteps", "steps", "step", "测试步骤", "步骤",
        "test_step", "description", "desc",
    ):
        if item.get(c) is not None:
            steps_raw = item.get(c)
            break
    for c in ("expected_result", "expectedResult", "expected", "预期结果", "预期", "expect"):
        if item.get(c) is not None:
            expected_raw = item.get(c)
            break

    expected = _listify_field(expected_raw)
    # Treat placeholder phrases as empty (must not invent expected for flow manuals)
    _EMPTY_EXPECTED_MARKERS = {
        "文档未写明预期",
        "文档未写明",
        "无",
        "无预期",
        "暂无",
        "按页面实际",
        "界面状态符合当前操作预期",
        "成功",
        "完成",
        "同上",
        "见上",
        "略",
    }
    expected = [
        ""
        if (not e)
        or (e.strip() in _EMPTY_EXPECTED_MARKERS)
        or re.fullmatch(r"(?:\d+[\.、]\s*)+", e.strip())
        or re.fullmatch(r"[；;\s]+", e.strip())  # align merge-of-empties artifact
        or not re.sub(r"[\d\.、\s；;]+", "", e.strip())
        else e
        for e in expected
    ]

    # Structured object steps (UI gen) vs Instant string steps
    structured_in: list[dict] = []
    string_in: list[str] = []
    if isinstance(steps_raw, list) and steps_raw and all(
        isinstance(x, dict) for x in steps_raw
    ):
        for item_step in steps_raw:
            st = coerce_structured_step(item_step)
            if st:
                structured_in.extend(expand_structured_compounds(st))
            else:
                desc = (item_step.get("description") or item_step.get("desc") or "").strip()
                if desc:
                    parsed = parse_instant_to_structured(_sanitize_ui_step(desc))
                    if parsed:
                        structured_in.extend(expand_structured_compounds(parsed))
                    else:
                        string_in.append(_sanitize_ui_step(desc))
    else:
        string_in = [_sanitize_ui_step(s) for s in _listify_field(steps_raw)]

    # If we mixed leftovers into string_in while having structured, upgrade leftovers
    if structured_in and string_in:
        for s in string_in:
            parsed = parse_instant_to_structured(s)
            if parsed:
                structured_in.extend(expand_structured_compounds(parsed))
        string_in = []

    # Prefer structured path when we have objects; else Instant strings
    if structured_in:
        # 027-e2e-fixes: value 错位净化（LLM 把描述句当值输出）
        structured_in = [sanitize_structured_value(s) for s in structured_in]
        while structured_in and not (
            structured_in[-1].get("action")
            or structured_in[-1].get("target_name")
            or structured_in[-1].get("value")
        ):
            structured_in.pop()
        expected = align_expected_to_steps(
            [render_structured_step(s) for s in structured_in], expected
        )
        expected = [
            "" if re.fullmatch(r"[；;\s]*", (e or "").strip() or "") else e
            for e in expected
        ]
        steps_desc = [render_structured_step(s) for s in structured_in]
        normalized["test_steps"] = _to_numbered_text(steps_desc)
        normalized["expected_result"] = _to_numbered_expected(expected)
        normalized["structured_steps"] = structured_in
        normalized["steps"] = [
            {**s, "description": d, "expected": e}
            for s, d, e in zip(structured_in, steps_desc, expected)
        ]
    elif string_in:
        while string_in and not string_in[-1]:
            string_in.pop()
        expected = align_expected_to_steps(string_in, expected)
        expected = [
            "" if re.fullmatch(r"[；;\s]*", (e or "").strip() or "") else e
            for e in expected
        ]
        steps: list[str] = []
        structured_out: list[dict | None] = []
        aligned_expected: list[str] = []
        for step, exp in zip(string_in, expected):
            parts = _expand_compound_ui_step(step)
            if not parts:
                continue
            # 027-e2e-fixes: string 步骤解析结果同样过 value 净化
            def _parsed_sanitized(txt):
                ps = parse_instant_to_structured(txt)
                return sanitize_structured_value(ps) if ps else None
            if len(parts) == 1:
                steps.append(parts[0])
                structured_out.append(_parsed_sanitized(parts[0]))
                aligned_expected.append(exp)
            else:
                for i, part in enumerate(parts):
                    steps.append(part)
                    structured_out.append(_parsed_sanitized(part))
                    aligned_expected.append(exp if i == len(parts) - 1 else "")
        normalized["test_steps"] = _to_numbered_text(steps)
        normalized["expected_result"] = _to_numbered_expected(aligned_expected)
        normalized["structured_steps"] = [s for s in structured_out if s]
        # Keep parallel list (may include None) for import by index
        normalized["structured_steps_aligned"] = structured_out
    elif expected:
        normalized["expected_result"] = _to_numbered_expected(expected)

    return normalized


def _parse_tcs_from_text(text: str, session_id: str = "", start_index: int = 0) -> list[TestCase]:
    """Parse test cases — try JSON format first, fall back to markdown."""
    json_str = _extract_json(text)
    if json_str:
        try:
            data = json.loads(json_str)
            items = data if isinstance(data, list) else (
                data.get("test_cases") or data.get("测试用例列表") or [])
            if items:
                tcs = []
                for i, item in enumerate(items):
                    tc = _normalize_tc_item(item if isinstance(item, dict) else {})
                    if not tc.get("title"):
                        logger.warning("TC normalize failed. item=%s norm=%s",
                                      {k: str(v)[:30] for k, v in (item.items() if isinstance(item, dict) else {})},
                                      {k: str(v)[:30] for k, v in tc.items()})
                    tcs.append(TestCase(
                        test_case_id=f"TC-{start_index + i + 1:03d}",
                        session_id=session_id,
                        module=tc.get("module", ""),
                        title=tc.get("title", ""),
                        preconditions=tc.get("preconditions", ""),
                        test_steps=tc.get("test_steps", ""),
                        structured_steps=list(
                            tc.get("structured_steps_aligned")
                            or tc.get("structured_steps")
                            or []
                        ),
                        expected_result=tc.get("expected_result", ""),
                        priority=tc.get("priority", "中"),
                    ))
                return tcs
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    tmp = AnalysisSession()
    tmp.session_id = session_id
    _parse_response(tmp, text)
    for tc in tmp.test_cases:
        start_index += 1
        tc.test_case_id = f"TC-{start_index:03d}"
    return tmp.test_cases


__all__ = [
    "_clean_text",
    "_to_html",
    "_parse_response",
    "_parse_fps_from_text",
    "_parse_tcs_from_text",
    "looks_truncated_fp_output",
]
