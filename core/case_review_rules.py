# core/case_review_rules.py
# 041 US2：用例评审的确定性规则（不调用模型）。
# 输入为草稿/已导入用例的通用 dict；输出 score/issues，供质量服务与测试使用。
from __future__ import annotations

import re
from typing import Any, Iterable

_AMBIGUOUS = re.compile(r"适当|相关|等等|根据需要|尽可能|合理|正确|一些|若干|合适")
_NEGATIVE = re.compile(r"错误|失败|无效|为空|不存在|边界|超长|非法|异常|拒绝|未授权|401|403|404|500")
_WEAK_ASSERT = re.compile(r"^(页面)?(可见|显示成功|成功|正常|加载完成)[。.]?$")
_ASSERT_TYPES = {"status_code", "jsonpath", "header", "body_contains", "body_regex", "response_time", "jsonschema", "expression"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _steps(case: dict) -> list[str]:
    steps = case.get("steps")
    if isinstance(steps, list):
        out = []
        for s in steps:
            if isinstance(s, dict):
                out.append(_text(s.get("description") or s.get("text") or s.get("name")))
            else:
                out.append(_text(s))
        return [s for s in out if s]
    raw = _text(case.get("test_steps"))
    if not raw:
        return []
    lines = [re.sub(r"^\s*\d+[.、)]\s*", "", ln).strip() for ln in raw.splitlines()]
    return [ln for ln in lines if ln]


def _assertions(case: dict) -> list[dict]:
    out = []
    for a in case.get("assertions") or []:
        if isinstance(a, dict):
            out.append(a)
    for s in case.get("steps") or []:
        if isinstance(s, dict):
            for a in s.get("assertions") or []:
                if isinstance(a, dict):
                    out.append(a)
    return out


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", _text(text)).lower()


def _bigrams(text: str) -> set[str]:
    s = normalize_text(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similarity(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / max(1, len(ba | bb))


def _issue(kind: str, severity: str, detail: str, suggestion: str) -> dict:
    return {"type": kind, "severity": severity, "detail": detail, "suggestion": suggestion}


def _assertion_strength(case: dict, steps: list[str]) -> tuple[int, list[dict]]:
    assertions = _assertions(case)
    issues: list[dict] = []
    if not assertions:
        joined = " ".join(steps) + " " + _text(case.get("expected_result") or case.get("expected"))
        if _WEAK_ASSERT.search(joined) or not _text(case.get("expected_result") or case.get("expected")):
            return 0, [_issue(
                "weak_assertion", "high",
                "没有可判定的断言/预期结果，只有页面可见或空预期",
                "增加具体断言：状态码、业务字段值、页面关键文案或数量",
            )]
        return 8, [_issue(
            "weak_assertion", "medium",
            "预期结果存在但没有结构化断言",
            "把预期结果转成可判定断言（文本/字段/状态码）",
        )]
    strong = 0
    for a in assertions:
        typ = _text(a.get("type"))
        expected = _text(a.get("expected") or a.get("value"))
        if typ in _ASSERT_TYPES and expected:
            strong += 2 if typ in {"jsonpath", "jsonschema", "body_contains", "body_regex", "expression"} else 1
    if strong <= 1:
        issues.append(_issue(
            "weak_assertion", "high",
            "断言仅校验状态码或断言值过于笼统",
            "增加业务字段/关键文案/结构断言，而不只是 200",
        ))
    elif strong <= 3:
        issues.append(_issue(
            "weak_assertion", "medium",
            "断言覆盖偏少",
            "补充关键字段与负向断言",
        ))
    return min(30, strong * 6), issues


def _duplicate(case: dict, similar: Iterable[dict]) -> tuple[bool, bool, str]:
    name = _text(case.get("name") or case.get("title"))
    steps = _steps(case)
    sig = normalize_text(name + "|" + "|".join(steps))
    near = False
    for other in similar or []:
        oname = _text(other.get("name") or other.get("title"))
        osteps = _steps(other)
        osig = normalize_text(oname + "|" + "|".join(osteps))
        if sig and sig == osig:
            return True, True, oname
        ratio = _similarity(name, oname)
        step_ratio = _similarity("|".join(steps), "|".join(osteps))
        if ratio >= 0.9 and (not steps or step_ratio >= 0.8):
            return True, True, oname
        if ratio >= 0.75 or step_ratio >= 0.85:
            near = True
    return False, near, ""


def evaluate_case(
    case: dict,
    *,
    similar_cases: Iterable[dict] | None = None,
    requirements: str | None = None,
) -> dict:
    """评估单条用例；返回 {score, issues, summary}。"""
    name = _text(case.get("name") or case.get("title")) or "未命名"
    steps = _steps(case)
    issues: list[dict] = []

    if not steps:
        issues.append(_issue("other", "high", "没有步骤", "补充可执行步骤"))
    elif len(steps) == 1 and _text(case.get("expected_result") or case.get("expected")):
        issues.append(_issue("other", "low", "只有单步用例", "确认是否需要补充前置/校验步骤"))

    ambiguous = [s for s in steps if _AMBIGUOUS.search(s)]
    if ambiguous:
        issues.append(_issue(
            "ambiguous", "high",
            f"步骤含模糊表述：{ambiguous[0][:80]}",
            "改写为具体元素、值或可验证结果，避免“适当/相关/等等”",
        ))

    assertion_score, assertion_issues = _assertion_strength(case, steps)
    issues.extend(assertion_issues)

    duplicate, near, dup_name = _duplicate(case, similar_cases or [])
    if duplicate:
        issues.append(_issue(
            "duplicate", "high",
            f"与已有用例重复：{dup_name or '同标题/同步骤'}",
            "合并或删除重复用例，保留覆盖更完整的一条",
        ))
    elif near:
        issues.append(_issue(
            "duplicate", "medium",
            "与已有用例高度相似",
            "检查是否可合并，补充差异化的断言或数据",
        ))

    joined = " ".join(steps) + " " + _text(case.get("expected_result") or case.get("expected"))
    has_negative = bool(_NEGATIVE.search(joined))
    if steps and not has_negative:
        issues.append(_issue(
            "missing_negative", "medium",
            "只有正向路径，缺少负向/边界场景",
            "补充空值/非法值/越权/边界等至少一个负向用例或步骤",
        ))

    trace_score = 10
    if requirements:
        rb = _bigrams(requirements)
        cb = _bigrams(name + " " + " ".join(steps))
        if rb and cb:
            overlap = len(rb & cb) / max(1, len(rb))
            if overlap < 0.03:
                trace_score = 0
                issues.append(_issue(
                    "untraceable", "low",
                    "未能在需求/功能点中找到对应依据",
                    "补充需求编号或明确该用例覆盖的功能点",
                ))

    executable = 25
    if not steps:
        executable = 0
    elif ambiguous:
        executable = 10
    coverage = 20 if has_negative else 10
    if not steps:
        coverage = 0
    duplicate_score = 0 if duplicate else (7 if near else 15)
    score = max(0, min(100, assertion_score + executable + coverage + duplicate_score + trace_score))

    high = sum(1 for i in issues if i["severity"] == "high")
    summary = (
        f"评分 {score}；{len(issues)} 个问题"
        + (f"，其中 {high} 个高优先级" if high else "")
    )
    return {"score": score, "issues": issues, "summary": summary}
