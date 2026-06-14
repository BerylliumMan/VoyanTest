"""Parse model responses into FunctionalPoint / TestCase dataclasses.

The model is asked to emit two sections separated by markdown headers:
- ``## 功能点清单`` (functional points)
- ``## 测试用例`` (test cases, as a 7-column markdown table)

Helpers here split the raw text into those sections, normalize whitespace,
and populate the dataclass instances used by the rest of the pipeline.
"""

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


def _parse_fps_from_text(text: str, session_id: str = "") -> list[FunctionalPoint]:
    """Parse only functional points from model response text."""
    tmp = AnalysisSession()
    tmp.session_id = session_id
    _parse_response(tmp, text)
    return tmp.functional_points


def _parse_tcs_from_text(text: str, session_id: str = "", start_index: int = 0) -> list[TestCase]:
    """Parse only test cases from model response text.

    Args:
        start_index: Starting index for TC numbering (default 0 → TC-001).
    """
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
