"""US2 JUnit 序列化（批次→XML）——033-api-test-maturity。

TDD：先于实现编写（预期红：app.services.junit_report 尚不存在）。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.services.junit_report import build_junit_xml


def _batch(**over):
    meta = {
        "id": 421,
        "name": "接口用例批次",
        "status": "passed",
        "started_at": "2026-10-09T10:00:00+00:00",
        "finished_at": "2026-10-09T10:00:05+00:00",
    }
    meta.update(over)
    return meta


def _run(**over):
    run = {
        "run_id": 340,
        "case_name": "订单正常流",
        "status": "passed",
        "duration": 1.234,
        "start_time": "2026-10-09T10:00:01+00:00",
        "error": None,
        "steps": [],
    }
    run.update(over)
    return run


def _step(**over):
    step = {
        "step_number": 1,
        "name": "请求订单列表",
        "description": "GET https://x/orders → 200",
        "success": True,
        "status": "passed",
        "error": None,
        "assertions": [],
    }
    step.update(over)
    return step


def _assertion(**over):
    a = {
        "name": "",
        "type": "jsonpath",
        "condition": "equals",
        "expected": "1",
        "actual": "1",
        "passed": True,
        "error": "",
    }
    a.update(over)
    return a


def _parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)  # 非法 XML 会直接抛错（同时充当合法性校验）


# ── 结构映射 ────────────────────────────────────────────────────────────────

def test_batch_with_assertions_maps_testsuite_per_case_and_testcase_per_assertion():
    runs = [
        _run(case_name="登录正常流", steps=[
            _step(assertions=[_assertion(name="状态码为200", type="status_code")]),
        ]),
        _run(case_name="订单边界", run_id=341, steps=[
            _step(assertions=[
                _assertion(name="total>=1", passed=True),
                _assertion(name="total<=10", passed=True),
            ]),
        ]),
    ]
    xml = build_junit_xml(_batch(), runs)
    root = _parse(xml)
    assert root.tag == "testsuites"
    assert root.get("tests") == "3"
    assert root.get("failures") == "0"
    assert root.get("errors") == "0"
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["登录正常流", "订单边界"]
    first = suites[0]
    assert first.get("tests") == "1"
    tc = first.find("testcase")
    assert tc.get("name") == "状态码为200"
    assert tc.get("classname") == "登录正常流"


def test_failed_assertion_becomes_failure_element():
    runs = [_run(steps=[_step(success=False, status="failed", assertions=[
        _assertion(name="total=1", type="jsonpath", passed=False,
                   expected="1", actual="2", error="期望 1 实际 2"),
    ])])]
    xml = build_junit_xml(_batch(status="failed"), runs)
    root = _parse(xml)
    assert root.get("failures") == "1"
    tc = root.find("testsuite/testcase")
    f = tc.find("failure")
    assert f is not None
    assert f.get("type") == "jsonpath"
    assert "期望 1 实际 2" in (f.get("message") or "")
    assert "expected" in (f.text or "") and "actual" in (f.text or "")


def test_script_error_step_becomes_error_element():
    runs = [_run(status="failed", steps=[
        _step(success=False, status="failed", error="脚本异常: ZeroDivisionError: x",
              assertions=[]),
    ])]
    xml = build_junit_xml(_batch(status="failed"), runs)
    root = _parse(xml)
    assert root.get("errors") == "1"
    e = root.find("testsuite/testcase/error")
    assert e is not None and "ZeroDivisionError" in (e.get("message") or "")


def test_step_without_assertions_falls_back_to_step_testcase():
    runs = [_run(steps=[_step(name="请求订单列表", assertions=[])])]
    root = _parse(build_junit_xml(_batch(), runs))
    tc = root.find("testsuite/testcase")
    assert tc is not None
    assert tc.get("name") == "请求订单列表"
    assert root.get("tests") == "1"


def test_run_log_style_failing_step_becomes_error():
    """UI/客户端执行路径（run_logs 回退口径）：无 assertions、description 承载信息。"""
    runs = [_run(status="failed", steps=[
        {"step_number": 1, "success": True, "level": "info", "description": "goto http://x"},
        {"step_number": 2, "success": False, "level": "error", "description": "assert_text 失败：未找到"},
    ])]
    root = _parse(build_junit_xml(_batch(status="failed"), runs))
    assert root.get("tests") == "2"
    assert root.get("errors") == "1"
    first, second = root.findall("testsuite/testcase")
    assert first.get("name") == "goto http://x"
    e = second.find("error")
    assert e is not None and "未找到" in (e.get("message") or "")


def test_error_level_log_on_passed_run_stays_plain():
    """通过批次中的 error 级日志（如回放失败后回退 OTA）不映射为 <error>。"""
    runs = [_run(status="passed", steps=[
        {"step_number": 1, "success": False, "level": "error", "description": "脚本回放失败: 回退 OTA"},
        {"step_number": 2, "success": True, "level": "info", "description": "click e1"},
    ])]
    root = _parse(build_junit_xml(_batch(), runs))
    assert root.get("errors") == "0"
    assert root.get("tests") == "2"


def test_run_level_error_without_steps_becomes_error_testcase():
    runs = [_run(status="error", error="传输失败: ConnectError", steps=[])]
    root = _parse(build_junit_xml(_batch(status="failed"), runs))
    e = root.find("testsuite/testcase/error")
    assert e is not None and "ConnectError" in (e.get("message") or "")


def test_empty_batch_is_legal_and_zeroed():
    root = _parse(build_junit_xml(_batch(status="passed"), []))
    assert root.tag == "testsuites"
    assert root.get("tests") == "0" and root.get("failures") == "0" and root.get("errors") == "0"
    assert root.get("time") == "0"


def test_duration_and_timestamp_are_serialized():
    runs = [_run(duration=1.234567, start_time="2026-10-09T10:00:01+00:00", steps=[_step(assertions=[_assertion()])])]
    root = _parse(build_junit_xml(_batch(), runs))
    suite = root.find("testsuite")
    assert suite.get("time") == "1.235"  # 3 位小数
    assert suite.get("timestamp") == "2026-10-09T10:00:01+00:00"


def test_invalid_xml_chars_are_cleaned():
    bad = "断言失败: \x00\x08坏字符"  # 控制字符会让 XML 非法
    runs = [_run(steps=[_step(success=False, status="failed", assertions=[
        _assertion(name="x", passed=False, error=bad),
    ])])]
    xml = build_junit_xml(_batch(status="failed"), runs)
    root = _parse(xml)  # 不抛错 = 已清洗
    msg = root.find("testsuite/testcase/failure").get("message") or ""
    assert "坏字符" in msg and "\x00" not in msg


def test_run_error_added_as_system_err_when_no_error_testcase():
    runs = [_run(status="failed", error="批次中断", steps=[
        _step(success=False, status="failed", error=None, assertions=[
            _assertion(passed=False, error="断言失败"),
        ]),
    ])]
    root = _parse(build_junit_xml(_batch(status="failed"), runs))
    se = root.find("testsuite/system-err")
    assert se is not None and "批次中断" in (se.text or "")


def test_suite_counts_are_consistent():
    runs = [
        _run(case_name="A", steps=[
            _step(assertions=[_assertion(passed=True)]),
            _step(assertions=[_assertion(passed=False, error="x")]),
        ]),
        _run(case_name="B", run_id=2, steps=[
            _step(success=False, status="failed", error="执行炸了", assertions=[]),
        ]),
    ]
    root = _parse(build_junit_xml(_batch(status="failed"), runs))
    assert root.get("tests") == "3"
    assert root.get("failures") == "1"
    assert root.get("errors") == "1"
    a, b = root.findall("testsuite")
    assert (a.get("tests"), a.get("failures"), a.get("errors")) == ("2", "1", "0")
    assert (b.get("tests"), b.get("failures"), b.get("errors")) == ("1", "0", "1")
