# app/gen/agents/validator.py
# Deterministic quality validator for generated test cases.
# Zero LLM calls — pure function checks.

from typing import Any
import re
from dataclasses import asdict as _asdict

from core.step_normalize import (
    UI_ACTIONS,
    coerce_structured_step,
    label_has_control_type_word,
    label_has_ellipsis,
    parse_instant_to_structured,
    render_structured_step,
    validate_structured_step_fields,
)

VALID_ACTIONS = set(UI_ACTIONS) | {
    # legacy aliases accepted if present before coerce
    "assert", "screenshot", "press", "scroll", "input", "type", "navigate", "open",
}
MAX_STEP_DESCRIPTION_LENGTH = 500
TC_NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff\s\-（）(),.。!！?？、：；]{2,100}$")
# Truncated labels inside 【】 cannot be located reliably
_BRACKET_ELLIPSIS_RE = re.compile(r"【[^】]*(?:[…\.]{2,}|。。。)[^】]*】")
# Multi-action / close-all compounds that should have been split
_COMPOUND_STEP_RE = re.compile(
    r"(?:把|将)?所有(?:的)?(?:对话框|弹窗)|(?:关闭|关掉)所有(?:的)?(?:对话框|弹窗)|"
    r"(?:分别|依次|然后|并且).{0,8}(?:点击|输入|选择)|"
    r"(?:点击|输入).{0,20}(?:然后|并且|再).{0,8}(?:点击|输入|选择)"
)


class ValidationResult:
    """校验结果。"""

    def __init__(self) -> None:
        self.passed: bool = True
        self.checks: dict[str, bool] = {}
        self.warnings: list[str] = []

    def fail(self, check: str, reason: str) -> None:
        self.passed = False
        self.checks[check] = False
        self.warnings.append(reason)

    def pass_check(self, check: str) -> None:
        if check not in self.checks:
            self.checks[check] = True


def _is_ui_structured_case(tc: dict[str, Any]) -> bool:
    """True when case already has StructuredStep objects or explicit flag."""
    if tc.get("require_structured_steps"):
        return True
    steps = tc.get("steps") or tc.get("structured_steps")
    if isinstance(steps, list) and steps and all(
        isinstance(s, dict) and s.get("action") for s in steps
    ):
        return True
    return False


def _normalize_steps_for_validation(tc: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize steps to list of dicts with description/action/expected."""
    _er_raw = tc.get("expected_result") or ""
    if isinstance(_er_raw, list):
        _er_raw = " ".join(str(v) for v in _er_raw if v)
    if isinstance(_er_raw, str) and _er_raw.strip():
        _er_parts = [p.strip() for p in re.split(r"\d+\.\s*", _er_raw.strip()) if p.strip()]
    else:
        exp = tc.get("expected")
        if isinstance(exp, list):
            _er_parts = [str(x).strip() if x is not None else "" for x in exp]
        else:
            _er_parts = []

    steps = tc.get("steps") or tc.get("structured_steps") or tc.get("test_steps") or []
    if isinstance(steps, str):
        parts = re.split(r"\d+\.\s*", steps.strip())
        steps = [p.strip() for p in parts if p.strip()] or [steps.strip()]

    if not isinstance(steps, list):
        return []

    _er_padded = _er_parts[: len(steps)]
    if len(_er_padded) < len(steps):
        _er_padded = [""] * (len(steps) - len(_er_padded)) + _er_padded

    out: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        if isinstance(step, str):
            parsed = parse_instant_to_structured(step) or {}
            out.append({
                **parsed,
                "description": step,
                "action": parsed.get("action") or "",
                "expected": _er_padded[i] if i < len(_er_padded) else "",
            })
            continue
        if not isinstance(step, dict):
            out.append({"description": str(step), "action": "", "expected": ""})
            continue
        coerced = coerce_structured_step(step) or {}
        if not coerced.get("action"):
            desc0 = (step.get("description") or step.get("desc") or "").strip()
            if desc0:
                coerced = parse_instant_to_structured(desc0) or coerced
        desc = (
            step.get("description")
            or step.get("desc")
            or render_structured_step(coerced)
            or ""
        ).strip()
        expected = (
            step.get("parsed_result")
            or step.get("expected")
            or (_er_padded[i] if i < len(_er_padded) else "")
            or ""
        )
        merged = {**coerced, "description": desc, "expected": str(expected).strip()}
        if not merged.get("action") and step.get("action"):
            merged["action"] = str(step.get("action") or "").strip().lower()
        out.append(merged)
    return out


def _validate_test_case(
    tc: dict[str, Any],
    *,
    require_structured: bool | None = None,
) -> ValidationResult:
    """Validate a single generated test case."""
    if not isinstance(tc, dict):
        tc = _asdict(tc)
    result = ValidationResult()
    if require_structured is None:
        require_structured = _is_ui_structured_case(tc)

    title = (tc.get("title") or tc.get("name") or "").strip()
    if not title:
        result.fail("title_required", "用例标题不能为空")
    elif not TC_NAME_PATTERN.match(title):
        result.fail("title_format", f"用例标题格式异常: {title[:30]}")
    else:
        result.pass_check("title_required")

    steps = _normalize_steps_for_validation(tc)
    if len(steps) < 1:
        result.fail("steps_required", "用例至少需要 1 个步骤")
    else:
        result.pass_check("steps_required")

    for i, step in enumerate(steps):
        desc = (step.get("description") or "").strip()
        action = (step.get("action") or "").strip().lower()

        if require_structured:
            # Hard-check raw fields BEFORE coerce strips ellipsis / type suffixes
            raw_steps = tc.get("steps") or tc.get("structured_steps") or []
            raw = raw_steps[i] if isinstance(raw_steps, list) and i < len(raw_steps) else None
            if isinstance(raw, dict):
                raw_name = raw.get("target_name") or raw.get("target") or raw.get("name")
                raw_value = raw.get("value")
                if label_has_ellipsis(raw_name if isinstance(raw_name, str) else None):
                    result.fail(
                        f"step_{i}_ellipsis",
                        f"步骤 {i + 1} 的 target_name 含省略号: {raw_name}",
                    )
                if label_has_ellipsis(str(raw_value) if raw_value is not None else None):
                    result.fail(
                        f"step_{i}_ellipsis_val",
                        f"步骤 {i + 1} 的 value 含省略号: {raw_value}",
                    )
                if label_has_control_type_word(raw_name if isinstance(raw_name, str) else None):
                    result.fail(
                        f"step_{i}_ctrl_type",
                        f"步骤 {i + 1} 的 target_name 含控件类型词: {raw_name}",
                    )
            for reason in validate_structured_step_fields(step, index=i, require_action=True):
                # skip duplicates already covered by raw checks
                if "省略号" in reason or "控件类型词" in reason:
                    continue
                result.fail(f"step_{i}_struct", reason)
        else:
            if not desc:
                result.fail(f"step_{i}_desc", f"步骤 {i + 1} 描述不能为空")
            if action and action not in VALID_ACTIONS:
                result.fail(
                    f"step_{i}_action",
                    f"步骤 {i + 1} 操作 '{action}' 不在合法操作列表中",
                )
            if _BRACKET_ELLIPSIS_RE.search(desc):
                result.fail(
                    f"step_{i}_ellipsis",
                    f"步骤 {i + 1} 的【】内含省略号，无法可靠定位: {desc[:60]}",
                )
            if _COMPOUND_STEP_RE.search(desc):
                result.fail(
                    f"step_{i}_compound",
                    f"步骤 {i + 1} 疑似并步（多动作/关闭所有弹窗），须拆成单步: {desc[:60]}",
                )

        if len(desc) > MAX_STEP_DESCRIPTION_LENGTH:
            result.fail(
                f"step_{i}_length",
                f"步骤 {i + 1} 描述过长（{len(desc)} > {MAX_STEP_DESCRIPTION_LENGTH}）",
            )

    nonempty_expected = 0
    for step in steps:
        exp = (step.get("parsed_result") or step.get("expected") or "").strip()
        if exp and not re.fullmatch(r"(?:\d+[\.、]\s*)+", exp):
            nonempty_expected += 1
    if len(steps) >= 5 and nonempty_expected == 0:
        result.warnings.append(
            f"用例「{(tc.get('title') or tc.get('name') or '')[:40]}」有 {len(steps)} 步但无任何可观察预期，手册可能缺断言"
        )

    module = tc.get("module") or ""
    if not module:
        result.fail("module_required", "用例模块不能为空")
    else:
        result.pass_check("module_required")

    priority = tc.get("priority") or "P2"
    pri_parts = [p.strip() for p in str(priority).replace(",", "/").split("/")]
    valid = False
    valid_priorities = ("P0", "P1", "P2", "P3", "高", "中", "低", "HIGH", "MEDIUM", "LOW")
    valid_upper = {p.upper() for p in valid_priorities}
    for part in pri_parts:
        if part.upper() in valid_upper:
            valid = True
            break
    if not valid:
        result.fail("priority_invalid", f"优先级 '{priority}' 不是有效值（P0/P1/P2/P3 或 高/中/低）")
    else:
        result.pass_check("priority_invalid")

    return result


def validate_test_cases(
    test_cases: list[dict[str, Any]],
    functional_points: list[dict[str, Any]] | None = None,
    *,
    require_structured: bool | None = None,
) -> dict[str, Any]:
    """Validate a list of generated test cases.

    ``require_structured=True`` forces UI StructuredStep hard gates.
    ``None`` auto-detects from object steps / case_kind.
    """
    valid_cases: list[tuple[dict[str, Any], ValidationResult]] = []
    invalid_cases: list[tuple[dict[str, Any], ValidationResult]] = []

    for tc in test_cases:
        vr = _validate_test_case(tc, require_structured=require_structured)
        if vr.passed:
            valid_cases.append((tc, vr))
        else:
            invalid_cases.append((tc, vr))

    warnings: list[str] = []

    if invalid_cases:
        warnings.append(f"{len(invalid_cases)}/{len(test_cases)} 个用例未通过校验")

    for _, vr in valid_cases:
        warnings.extend(vr.warnings)
    for _, vr in invalid_cases:
        warnings.extend(vr.warnings)

    if functional_points:
        def _fp_title(fp):
            if not isinstance(fp, dict):
                return getattr(fp, "name", getattr(fp, "title", "")) or ""
            return fp.get("name", fp.get("title", "")) or ""

        fp_titles = {_fp_title(fp).strip() for fp in functional_points if _fp_title(fp)}
        covered_fps: set[str] = set()
        for tc, _ in valid_cases:
            tc_title = getattr(tc, "title", tc.get("title", "") if isinstance(tc, dict) else "")
            for fp_title in fp_titles:
                if fp_title and (fp_title in tc_title or tc_title in fp_title):
                    covered_fps.add(fp_title)

        uncovered = fp_titles - covered_fps
        if uncovered:
            warnings.append(
                f"{len(uncovered)}/{len(fp_titles)} 个功能点未被覆盖: {', '.join(list(uncovered)[:5])}"
            )

    annotated_invalid: list = []
    for tc, vr in invalid_cases:
        err_text = "; ".join(vr.warnings)
        if hasattr(tc, "validation_errors"):
            tc.validation_errors = err_text
        elif isinstance(tc, dict):
            tc["validation_errors"] = err_text
        annotated_invalid.append(tc)

    return {
        "passed": len(test_cases) == 0 or len(invalid_cases) <= len(test_cases) // 2,
        "warnings": warnings,
        "valid_count": len(valid_cases),
        "invalid_count": len(invalid_cases),
        "valid_cases": [tc for tc, _ in valid_cases],
        "invalid_cases": annotated_invalid,
    }
