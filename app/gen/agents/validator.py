# app/gen/agents/validator.py
# Deterministic quality validator for generated test cases.
# Zero LLM calls — pure function checks.

from typing import Any
import re
from dataclasses import asdict as _asdict

VALID_ACTIONS = {
    "click", "fill", "goto", "select", "hover",
    "scroll", "wait", "assert", "screenshot", "press",
}
MAX_STEP_DESCRIPTION_LENGTH = 500
TC_NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff\s\-（）(),.。!！?？、：；]{2,100}$")


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


def _validate_test_case(tc: dict[str, Any]) -> ValidationResult:
    """Validate a single generated test case."""
    if not isinstance(tc, dict):
        tc = _asdict(tc)
    result = ValidationResult()

    # Check 1: Title exists and is valid
    title = (tc.get("title") or tc.get("name") or "").strip()
    if not title:
        result.fail("title_required", "用例标题不能为空")
    elif not TC_NAME_PATTERN.match(title):
        result.fail("title_format", f"用例标题格式异常: {title[:30]}")
    else:
        result.pass_check("title_required")

    # 预解析 expected_result（支持编号格式："1.结果1 2.结果2"，或无编号的纯文本）
    # 注意：编号可能跳过中间步骤（如仅 "3.保存成功"），解析后需要右对齐
    _er_raw = tc.get("expected_result") or tc.get("test_steps") or ""
    if isinstance(_er_raw, list):
        _er_raw = " ".join(str(v) for v in _er_raw if v)
    if isinstance(_er_raw, str):
        _er_parts = [p.strip() for p in re.split(r'\d+\.\s*', _er_raw.strip()) if p.strip()]
    else:
        _er_parts = []

    # Check 2: At least one step（支持 test_steps 字符串字段）
    steps = tc.get("steps") or tc.get("test_steps") or []
    if isinstance(steps, str):
        # 用编号拆分 steps（"1. step1 2. step2" → ["step1", "step2"]），兼容无编号的纯描述
        parts = re.split(r'\d+\.\s*', steps.strip())
        steps = [p.strip() for p in parts if p.strip()] or [steps.strip()]

    # 右对齐 _er_parts：如果预期结果少于步骤，优先匹配末尾步骤
    _er_padded = _er_parts[:len(steps)]  # 截断多余的
    if len(_er_padded) < len(steps):
        _er_padded = [''] * (len(steps) - len(_er_padded)) + _er_padded  # 右对齐

    if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
        # 如果 steps 是字符串列表（从没进入 string 拆分的路径），转为 dict
        steps = [
            {"description": s, "action": "", "expected": _er_padded[i] if i < len(_er_padded) else ""}
            for i, s in enumerate(steps)
        ]
    if not isinstance(steps, list):
        result.fail("steps_required", "steps 必须是数组")
    elif len(steps) < 1:
        result.fail("steps_required", "用例至少需要 1 个步骤")
    else:
        result.pass_check("steps_required")

    # Check 3: Each step has description and valid action
    for i, step in enumerate(steps):
        if isinstance(step, str):
            step = {"description": step, "action": "", "expected": _er_padded[i] if i < len(_er_padded) else ""}
            steps[i] = step
        desc = (step.get("description") or step.get("desc") or "").strip()
        if not desc:
            result.fail(f"step_{i}_desc", f"步骤 {i + 1} 描述不能为空")

        action = (step.get("action") or "").strip().lower()
        if action and action not in VALID_ACTIONS:
            result.fail(f"step_{i}_action", f"步骤 {i + 1} 操作 '{action}' 不在合法操作列表中")

        expected = (step.get("parsed_result") or step.get("expected") or "").strip()
        # 只校验最后一步的预期结果（LLM 通常不生成中间步骤的预期结果）
        if not expected and i < len(steps) - 1:
            pass  # 中间步骤允许没有预期结果
        elif not expected:
            result.fail(f"step_{i}_expected", f"步骤 {i + 1} 预期结果不能为空")

    # Check 4: Steps have basic sanity (description length)
    for i, step in enumerate(steps):
        desc = (step.get("description") or step.get("desc") or "").strip()
        if len(desc) > MAX_STEP_DESCRIPTION_LENGTH:
            result.fail(f"step_{i}_length", f"步骤 {i + 1} 描述过长（{len(desc)} > {MAX_STEP_DESCRIPTION_LENGTH}）")

    # Check 5: Module name exists
    module = tc.get("module") or ""
    if not module:
        result.fail("module_required", "用例模块不能为空")
    else:
        result.pass_check("module_required")

    # Check 6: Priority is valid — 支持中文（高/中/低）和英文（P0-P3）格式，允许拼接值（如 P0/P1/P2）
    priority = tc.get("priority") or "P2"
    # Split combined priorities like "P0/P1/P2" and check the first valid part
    pri_parts = [p.strip() for p in priority.replace(",", "/").split("/")]
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
) -> dict[str, Any]:
    """Validate a list of generated test cases.

    Args:
        test_cases: List of generated test case dicts
        functional_points: Optional list of functional points (for FP coverage check)

    Returns:
        Result dict with structure:
        {
            "passed": bool,
            "warnings": list[str],
            "cases": list[dict]  # Original cases with validation_result attached
        }
    """
    results: list[ValidationResult] = []
    valid_cases: list[tuple[dict[str, Any], ValidationResult]] = []
    invalid_cases: list[tuple[dict[str, Any], ValidationResult]] = []

    for tc in test_cases:
        vr = _validate_test_case(tc)
        results.append(vr)
        if vr.passed:
            valid_cases.append((tc, vr))
        else:
            invalid_cases.append((tc, vr))

    warnings: list[str] = []

    # Summary warnings
    if invalid_cases:
        warnings.append(f"{len(invalid_cases)}/{len(test_cases)} 个用例未通过校验")

    for _, vr in invalid_cases:
        warnings.extend(vr.warnings)

    # FP coverage check (if FPs provided)
    if functional_points:
        def _fp_title(fp):
            if not isinstance(fp, dict):
                return getattr(fp, "name", getattr(fp, "title", "")) or ""
            return fp.get("name", fp.get("title", "")) or ""
        fp_titles = {_fp_title(fp).strip() for fp in functional_points if _fp_title(fp)}
        covered_fps: set[str] = set()
        for tc, _ in valid_cases:
            # Check if TC references a FP title
            tc_title = getattr(tc, "title", tc.get("title", "") if isinstance(tc, dict) else "")
            for fp_title in fp_titles:
                if fp_title and (fp_title in tc_title or tc_title in fp_title):
                    covered_fps.add(fp_title)

        uncovered = fp_titles - covered_fps
        if uncovered:
            warnings.append(f"{len(uncovered)}/{len(fp_titles)} 个功能点未被覆盖: {', '.join(list(uncovered)[:5])}")

    return {
        "passed": len(test_cases) == 0 or len(invalid_cases) <= len(test_cases) // 2,
        "warnings": warnings,
        "valid_count": len(valid_cases),
        "invalid_count": len(invalid_cases),
        "valid_cases": [tc for tc, _ in valid_cases],
        "invalid_cases": [tc for tc, _ in invalid_cases],
    }
