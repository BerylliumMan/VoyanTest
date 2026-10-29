from core.case_review_rules import evaluate_case


def test_weak_assertion_detected():
    r = evaluate_case({"name": "登录", "steps": ["输入账号", "点击登录"], "expected_result": "页面可见"})
    assert any(i["type"] == "weak_assertion" for i in r["issues"])
    assert r["score"] < 80


def test_ambiguous_step_detected():
    r = evaluate_case({"name": "操作", "steps": ["适当填写相关信息"], "expected_result": "成功"})
    assert any(i["type"] == "ambiguous" for i in r["issues"])


def test_duplicate_detected():
    c = {"name": "登录成功", "steps": ["打开登录页", "输入 admin", "点击登录"], "expected_result": "进入首页"}
    r = evaluate_case(c, similar_cases=[{"name": "登录成功", "steps": ["打开登录页", "输入 admin", "点击登录"]}])
    assert any(i["type"] == "duplicate" for i in r["issues"])


def test_negative_missing_detected():
    r = evaluate_case({"name": "登录", "steps": ["输入 admin", "点击登录"], "expected_result": "进入首页"})
    assert any(i["type"] == "missing_negative" for i in r["issues"])


def test_good_case_scores_high():
    r = evaluate_case({
        "name": "登录失败提示",
        "steps": ["输入错误密码", "点击登录", "断言页面包含【用户名或密码错误】"],
        "expected_result": "页面包含【用户名或密码错误】",
        "assertions": [{"type": "body_contains", "condition": "contains", "expected": "用户名或密码错误"}],
    })
    assert r["score"] >= 70
