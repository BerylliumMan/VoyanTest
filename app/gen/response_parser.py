"""Parse model responses into FunctionalPoint / TestCase dataclasses.

The model is asked to emit two sections separated by markdown headers:
- ``## 功能点清单`` (functional points)
- ``## 测试用例`` (test cases, as a 7-column markdown table)

As a fallback, the parser also accepts JSON format responses (``{"function_points": [...]}``)
and ``{"test_cases": [...]}`` which some model versions prefer over markdown tables.
"""

import json
import re

from app.gen.models import AnalysisSession, FunctionalPoint, TestCase


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
            return candidate
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
        # Use raw_decode to handle trailing text
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(candidate)
            return json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            return candidate
    return None


def _normalize_fp_item(item: dict) -> dict:
    """Normalize FP item field names (handle camelCase, snake_case, etc.)."""
    normalized = {}
    field_map = {
        "module": ["module", "module_name"],
        "name": ["name", "function_name", "functionname", "function_name", "title"],
        "description": ["description", "desc"],
        "category": ["category", "cat"],
        "priority": ["priority", "pri"],
    }
    for target, candidates in field_map.items():
        for c in candidates:
            val = item.get(c) or item.get(c.lower()) or item.get(c.title())
            if val:
                normalized[target] = val
                break
    return normalized


def _parse_fps_from_text(text: str, session_id: str = "") -> list[FunctionalPoint]:
    """Parse functional points — try JSON format first, fall back to markdown."""
    json_str = _extract_json(text)
    if json_str:
        try:
            data = json.loads(json_str)
            # Handle both {"key": [...]} and raw [...]
            items = data if isinstance(data, list) else (
                data.get("function_points") or data.get("functional_points")
                or data.get("fp_extract") or data.get("fp_list") or [])
            if items:
                fps = []
                for i, item in enumerate(items):
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
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    # Fallback to markdown parsing
    tmp = AnalysisSession()
    tmp.session_id = session_id
    _parse_response(tmp, text)
    return tmp.functional_points


def _normalize_tc_item(item: dict) -> dict:
    """Normalize TC field names (handle camelCase, snake_case, etc.)."""
    normalized = {}
    field_map = {
        "module": ["module", "module_name"],
        "title": ["title", "name", "test_name", "test_name"],
        "preconditions": ["preconditions", "precondition", "precondition"],
        "test_steps": ["test_steps", "testSteps", "test_steps", "steps", "step"],
        "expected_result": ["expected_result", "expectedResult", "expected", "expected_result"],
        "priority": ["priority", "pri"],
    }
    for target, candidates in field_map.items():
        for c in candidates:
            val = item.get(c) or item.get(c[0].upper() + c[1:]) or item.get(c.lower())
            if val:
                normalized[target] = val
                break
    return normalized


def _parse_tcs_from_text(text: str, session_id: str = "", start_index: int = 0) -> list[TestCase]:
    """Parse test cases — try JSON format first, fall back to markdown."""
    json_str = _extract_json(text)
    if json_str:
        try:
            data = json.loads(json_str)
            items = data if isinstance(data, list) else (data.get("test_cases") or [])
            if items:
                tcs = []
                for i, item in enumerate(items):
                    tc = item if isinstance(item, dict) else {}
                    tcs.append(TestCase(
                        test_case_id=f"TC-{start_index + i + 1:03d}",
                        session_id=session_id,
                        module=tc.get("module", tc.get("module_name", "")),
                        title=tc.get("title", tc.get("name", "")),
                        preconditions=tc.get("preconditions", tc.get("precondition", "")),
                        test_steps=tc.get("test_steps", tc.get("steps", tc.get("step", ""))),
                        expected_result=tc.get("expected_result", tc.get("expected", "")),
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
]
