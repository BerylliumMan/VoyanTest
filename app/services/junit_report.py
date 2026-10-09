"""033（US2）：批次 → JUnit XML 序列化（stdlib，零依赖）。

契约见 ``specs/033-api-test-maturity/contracts/api-maturity.md`` §C2：
- ``testsuite`` = 每个用例；``testcase`` = 每条断言（无断言回退"步骤名"）；
- 断言失败 → ``<failure type message>``；脚本/执行错误 → ``<error>``；
- 运行级错误（未被 error testcase 代表时）→ ``<system-err>``；
- XML 1.0 非法字符清洗；``time`` 保留 3 位小数。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

# XML 1.0 非法字符（控制符；保留 \t \n \r）
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(value: Any) -> str:
    return _ILLEGAL_XML_RE.sub("", "" if value is None else str(value))


def _fmt_time(seconds: Any) -> str:
    try:
        return f"{max(0.0, float(seconds)):.3f}"
    except (TypeError, ValueError):
        return "0.000"


def _step_testcase_name(step: dict, n: Any) -> str:
    name = str(step.get("name") or "").strip()
    if name:
        return name
    desc = str(step.get("description") or "").strip().splitlines()
    if desc and desc[0]:
        return desc[0]
    return f"步骤 {n}" if n is not None else "步骤"


def build_junit_xml(batch: dict, runs: list[dict]) -> str:
    """把批次详情（含每 run 的 steps/assertions）序列化为 JUnit XML 字符串。"""
    batch = batch or {}
    runs = runs or []

    root = ET.Element("testsuites")
    root.set("name", _clean(batch.get("name") or f"批次 #{batch.get('id')}"))

    total_tests = total_failures = total_errors = 0
    total_time = 0.0

    for run in runs:
        if not isinstance(run, dict):
            continue
        suite_name = str(
            run.get("case_name") or run.get("display_name") or run.get("run_id") or "用例"
        )
        suite = ET.SubElement(root, "testsuite")
        suite.set("name", _clean(suite_name))
        start_time = run.get("start_time")
        if start_time:
            suite.set("timestamp", _clean(start_time))

        suite_tests = suite_failures = suite_errors = 0
        has_error_testcase = False
        steps = run.get("steps") or []
        run_failed = str(run.get("status") or "") in ("failed", "error")

        for step in steps:
            if not isinstance(step, dict):
                continue
            n = step.get("step_number")
            assertions = step.get("assertions") or []
            if assertions:
                for a in assertions:
                    if not isinstance(a, dict):
                        continue
                    tc_name = str(a.get("name") or "").strip() or (
                        f"步骤 {n}·{a.get('type') or '断言'}" if n is not None else "断言"
                    )
                    tc = ET.SubElement(suite, "testcase")
                    tc.set("name", _clean(tc_name))
                    tc.set("classname", _clean(suite_name))
                    suite_tests += 1
                    if not a.get("passed"):
                        failure = ET.SubElement(tc, "failure")
                        failure.set("type", str(a.get("type") or "assertion"))
                        failure.set("message", _clean(a.get("error") or "断言失败"))
                        failure.text = _clean(
                            f"expected: {a.get('expected')}\nactual: {a.get('actual')}"
                        )
                        suite_failures += 1
            elif (step.get("success") is False and step.get("error")) or (
                run_failed
                and (step.get("success") is False or step.get("level") == "error")
            ):
                # 执行失败步骤：report.json 的 error 字段，或失败批次中 run_logs 回退口径
                # （description 承载信息）。通过批次的 error 级日志不映射为 <error>（CI 信号完整）。
                message = step.get("error") or step.get("description") or "执行失败"
                tc = ET.SubElement(suite, "testcase")
                tc.set("name", _clean(_step_testcase_name(step, n)))
                tc.set("classname", _clean(suite_name))
                error = ET.SubElement(tc, "error")
                error.set("message", _clean(message))
                suite_tests += 1
                suite_errors += 1
                has_error_testcase = True
            else:
                tc = ET.SubElement(suite, "testcase")
                tc.set("name", _clean(_step_testcase_name(step, n)))
                tc.set("classname", _clean(suite_name))
                suite_tests += 1

        run_error = run.get("error")
        if run_error and not steps:
            # 无步骤可映射时的运行级错误 → 单个 <error> testcase
            tc = ET.SubElement(suite, "testcase")
            tc.set("name", "执行错误")
            tc.set("classname", _clean(suite_name))
            error = ET.SubElement(tc, "error")
            error.set("message", _clean(run_error))
            suite_tests += 1
            suite_errors += 1
            has_error_testcase = True
        elif run_error and not has_error_testcase:
            se = ET.SubElement(suite, "system-err")
            se.text = _clean(run_error)

        duration = run.get("duration")
        suite_time = _fmt_time(duration)
        try:
            total_time += max(0.0, float(duration or 0.0))
        except (TypeError, ValueError):
            pass
        suite.set("tests", str(suite_tests))
        suite.set("failures", str(suite_failures))
        suite.set("errors", str(suite_errors))
        suite.set("time", suite_time)

        total_tests += suite_tests
        total_failures += suite_failures
        total_errors += suite_errors

    root.set("tests", str(total_tests))
    root.set("failures", str(total_failures))
    root.set("errors", str(total_errors))
    root.set("time", "0" if not runs else _fmt_time(total_time))
    return ET.tostring(root, encoding="unicode")
