"""验证码自动识别与填写（2026-10-14）：core/captcha.py + core/captcha_solver.py + 探索主循环集成。

覆盖：OCR 封装（data URL 解码 / 算术验证码 / 组件缺失降级）、识别-填写-校验流程
（ref 填写 / JS 回退 / 捕获失败）、探索主循环 solve_captcha 动作（成功日志 / 刷新传递 / 关闭开关）。
"""
import base64
import json
from types import SimpleNamespace

import pytest

from core import captcha_solver
from core.captcha import _capture_js, solve_captcha_via_browser
from core.chat import browse
from core.chat.browse import BrowseConfig, run_exploration

_IMG_1PX_PNG = (
    "data:image/png;base64,"
    + base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
        )
    ).decode()
)


# ----------------------------
# captcha_solver（OCR 封装）
# ----------------------------

def test_solver_cleans_text_and_strips_noise(monkeypatch):
    class FakeOcr:
        def classification(self, payload):
            assert payload  # 已解码的图片字节
            return " A1b2! "

    monkeypatch.setattr(captcha_solver, "_get_ocr", lambda beta=False: FakeOcr())
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert err == "" and text == "A1b2"


def test_solver_arithmetic_captcha_computed(monkeypatch):
    class FakeOcr:
        def classification(self, payload):
            return "3+4=?"

    monkeypatch.setattr(captcha_solver, "_get_ocr", lambda beta=False: FakeOcr())
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert err == "" and text == "7"
    assert captcha_solver.solve_arithmetic("6×7=?") == "42"
    assert captcha_solver.solve_arithmetic("9-4") == "5"
    assert captcha_solver.solve_arithmetic("ABCD") is None


def test_solver_prefer_len_picks_matching_result(monkeypatch):
    """给出输入框 maxlength 时，优先采用长度一致的结果（不同模型组合择优）。"""
    def _fake_get_ocr(beta=False):
        class _Ocr:
            def classification(self, payload):
                return "AB12X" if not beta else "AB12"  # 默认模型多识别出一个噪声字符

        return _Ocr()

    monkeypatch.setattr(captcha_solver, "_get_ocr", _fake_get_ocr)
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG, prefer_len=4)
    assert err == "" and text == "AB12"
    # 无长度约束：直接返回首个非空结果
    text2, err2 = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert err2 == "" and text2 == "AB12X"


def test_solver_all_empty_reports_error(monkeypatch):
    class FakeOcr:
        def classification(self, payload):
            return "  !!!  "

    monkeypatch.setattr(captcha_solver, "_get_ocr", lambda beta=False: FakeOcr())
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert text == "" and "为空" in err


def test_solver_unavailable_degrades(monkeypatch, caplog):
    def _boom(beta=False):
        raise ImportError("no ddddocr")

    monkeypatch.setattr(captcha_solver, "_get_ocr", _boom)
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert text == "" and "不可用" in err


def test_solver_invalid_data_url():
    text, err = captcha_solver.solve_text_captcha("%%%not-base64%%%")
    assert text == "" and err


# ----------------------------
# 假浏览器（区分 capture / fill(js) / verify 三种 evaluate 脚本）
# ----------------------------

class FakeCaptchaBrowser:
    mode = "server"
    label = "验证码假浏览器"

    def __init__(self, *, capture=None, verify=None, js_fill=None, ref_fill_ok=True, refill=None):
        self.capture = capture if capture is not None else {
            "ok": True, "img": _IMG_1PX_PNG, "has_input": True,
        }
        self.verify = verify if verify is not None else {"ok": True, "any_hit": True, "marked_found": True}
        self.js_fill = js_fill if js_fill is not None else {"ok": True}
        self.refill = refill if refill is not None else {"user": False, "pass": False}
        self.ref_fill_ok = ref_fill_ok
        self.actions: list[dict] = []
        self.evals: list[str] = []

    async def start(self): return None
    async def stop(self): return None
    async def snapshot(self): return "(snapshot)"
    async def page_facts(self): return {"url": "http://site.local/login", "title": "登录"}
    async def console(self, limit=20): return []
    async def screenshot(self, path): return path

    async def evaluate_json(self, fn):
        self.evals.append(fn)
        if "vt-refill" in fn:
            return dict(self.refill)
        if "window.location.href" in fn:
            return {"url": "http://site.local/login", "title": "登录"}
        if "toDataURL" in fn:
            return dict(self.capture)
        if "setter.call" in fn:
            return dict(self.js_fill)
        return dict(self.verify)

    async def act(self, action):
        self.actions.append(dict(action))
        if action.get("action") == "fill" and not self.ref_fill_ok:
            return {"success": False, "error": "fill failed"}
        return {"success": True}


def _solve_fn(text):
    return "A1B2", ""


# ----------------------------
# solve_captcha_via_browser 流程
# ----------------------------

@pytest.mark.asyncio
async def test_solve_flow_fills_via_ref(monkeypatch):
    browser = FakeCaptchaBrowser()
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=_solve_fn)
    assert result["ok"] and result["text_len"] == 4
    fills = [a for a in browser.actions if a.get("action") == "fill"]
    assert fills and fills[0]["selector"] == "e9" and fills[0]["value"] == "A1B2"
    assert any("toDataURL" in fn for fn in browser.evals)  # 捕获
    assert any("data-vt-cap" in fn and "setter.call" not in fn and "any_hit" in fn for fn in browser.evals)  # 校验


@pytest.mark.asyncio
async def test_solve_flow_falls_back_to_js_fill():
    browser = FakeCaptchaBrowser(ref_fill_ok=False, verify={"ok": False, "any_hit": False})
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=_solve_fn)
    assert result["ok"]
    assert any("setter.call" in fn for fn in browser.evals)  # JS 回退写入发生


@pytest.mark.asyncio
async def test_solve_flow_capture_error():
    browser = FakeCaptchaBrowser(capture={"ok": False, "error": "未找到验证码图片（img/canvas）"})
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=_solve_fn)
    assert not result["ok"] and "未找到验证码图片" in result["error"]
    assert not browser.actions  # 不产生任何填写动作
    # 图片未出现时刷新重试一次（2 次捕获尝试）
    assert sum(1 for fn in browser.evals if "toDataURL" in fn) == 2


@pytest.mark.asyncio
async def test_solve_flow_capture_retry_recovers():
    """首次抓不到图片（懒加载/竞态），刷新后第二次抓到 → 仍然成功。"""
    class RetryBrowser(FakeCaptchaBrowser):
        def __init__(self):
            super().__init__()
            self.captures = 0

        async def evaluate_json(self, fn):
            if "toDataURL" in fn:
                self.captures += 1
                self.evals.append(fn)
                if self.captures == 1:
                    return {"ok": False, "error": "未找到验证码图片（img/canvas）"}
                return {"ok": True, "img": _IMG_1PX_PNG, "has_input": True, "maxlength": 4}
            return await super().evaluate_json(fn)

    browser = RetryBrowser()
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=_solve_fn)
    assert result["ok"] and browser.captures == 2


@pytest.mark.asyncio
async def test_solve_flow_refills_empty_credentials(monkeypatch):
    """登录重试前补回被站点清空的账号/密码（实测缺陷：重试提交时 user 为空）。"""
    browser = FakeCaptchaBrowser(refill={"user": True, "pass": True})
    result = await solve_captcha_via_browser(
        browser, input_ref="e9", solve_fn=_solve_fn, username="admin", password="admin123",
    )
    assert result["ok"] and "已自动补回" in result["note"]
    assert any("vt-refill" in fn for fn in browser.evals)
    # 未提供凭据时不注入 refill 脚本
    browser2 = FakeCaptchaBrowser()
    await solve_captcha_via_browser(browser2, input_ref="e9", solve_fn=_solve_fn)
    assert not any("vt-refill" in fn for fn in browser2.evals)


def test_refill_js_embeds_values_safely():
    from core.captcha import _refill_js

    username, password = "ad'min", "p\\w\"1"
    js = _refill_js(username, password)
    assert "vt-refill" in js and "password" in js
    # 值经 json.dumps 安全嵌入（引号/反斜杠转义，不破坏 JS 字符串）
    assert json.dumps(username) in js and json.dumps(password) in js


@pytest.mark.asyncio
async def test_solve_flow_clamps_to_maxlength():
    """识别结果长于输入框 maxlength 时截断（并如实标注）。"""
    browser = FakeCaptchaBrowser(capture={"ok": True, "img": _IMG_1PX_PNG, "has_input": True, "maxlength": 4})
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=lambda _: ("ABCDE", ""))
    assert result["ok"] and result["text_len"] == 4
    fills = [a for a in browser.actions if a.get("action") == "fill"]
    assert fills[0]["value"] == "ABCD"
    assert "截断" in result["note"]


@pytest.mark.asyncio
async def test_solve_flow_ocr_error():
    browser = FakeCaptchaBrowser()
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=lambda _: ("", "识别失败"))
    assert not result["ok"] and "识别失败" in result["error"]


def test_capture_js_refresh_flag():
    assert "true" in _capture_js(True) and ".click()" in _capture_js(True)
    assert "false" in _capture_js(False)


def test_capture_js_extended_keywords_and_label_scan():
    """实测加固回归：扩展关键词（imgCode/yzm/authcode/kaptcha）与输入框邻近标签文本扫描。"""
    js = _capture_js(False)
    for kw in ("imgcode", "authcode", "kaptcha", "yzm"):
        assert kw in js, kw
    assert "nearText" in js and "closest('label')" in js
    assert "maxlength" in js and "naturalWidth" in js


# ----------------------------
# 探索主循环集成
# ----------------------------

def _candidate(ref, role="textbox", name="验证码"):
    return SimpleNamespace(ref=ref, role=role, name=name, text="",
                           visible=True, enabled=True, obscured=False)


@pytest.fixture
def patch_candidates(monkeypatch):
    def _apply(cands):
        monkeypatch.setattr(
            "core.locator_candidates.extract_candidates", lambda snapshot, **kw: tuple(cands)
        )
        monkeypatch.setattr(
            "core.locator_candidates.actionable_candidates", lambda cs: tuple(cs)
        )
    return _apply


async def _llm_script(decisions):
    queue = list(decisions)

    async def _llm(messages):
        return json.dumps(queue.pop(0), ensure_ascii=False)

    return _llm


async def _run(monkeypatch, browser, decisions, **cfg_kw):
    monkeypatch.setattr(
        "core.captcha_solver.solve_text_captcha", lambda data_url: ("A1B2", "")
    )
    llm = await _llm_script(decisions)
    base = dict(url="http://site.local", goal="登录", username="admin", password="x")
    base.update(cfg_kw)
    return await run_exploration(BrowseConfig(**base), llm=llm, browser=browser)


@pytest.mark.asyncio
async def test_loop_solve_captcha_action(monkeypatch, patch_candidates):
    patch_candidates([_candidate("e9")])
    browser = FakeCaptchaBrowser()
    report = await _run(monkeypatch, browser, [
        {"thinking": "识别验证码", "action": "solve_captcha", "ref": "e9"},
        {"thinking": "完成", "action": "done", "summary": "登录成功", "features": ["首页"]},
    ])
    steps = [s for s in report["steps"] if s["action"] == "solve_captcha"]
    assert steps and steps[0]["ok"] and "已填写" in (steps[0]["value"] or "")
    assert "验证码" in (steps[0]["note"] or "")
    assert report["stopped_reason"] == "done"


@pytest.mark.asyncio
async def test_loop_solve_captcha_refresh_passthrough(monkeypatch, patch_candidates):
    patch_candidates([_candidate("e9")])
    browser = FakeCaptchaBrowser()
    await _run(monkeypatch, browser, [
        {"thinking": "验证码错误，刷新重试", "action": "solve_captcha", "ref": "e9", "refresh": True},
        {"thinking": "完成", "action": "done", "summary": "ok"},
    ])
    capture_fns = [fn for fn in browser.evals if "toDataURL" in fn]
    assert capture_fns and "true" in capture_fns[0] and ".click()" in capture_fns[0]


@pytest.mark.asyncio
async def test_loop_solve_captcha_failure_reason_in_step(monkeypatch, patch_candidates):
    """识别失败时步骤文案带失败原因（实测加固：此前只有「失败」无从定位）。"""
    patch_candidates([_candidate("e9")])

    monkeypatch.setattr(
        "core.captcha_solver.solve_text_captcha",
        lambda data_url: ("", "未找到验证码图片（img/canvas）"),
    )
    browser = FakeCaptchaBrowser(capture={"ok": False, "error": "未找到验证码图片（img/canvas）"})
    events: list[dict] = []

    async def _on_event(evt):
        events.append(evt)

    llm = await _llm_script([
        {"thinking": "识别验证码", "action": "solve_captcha", "ref": "e9"},
        {"thinking": "完成", "action": "done", "summary": "ok"},
    ])
    base = dict(url="http://site.local", goal="登录", username="admin", password="x")
    report = await run_exploration(
        BrowseConfig(**base), llm=llm, browser=browser, on_event=_on_event,
    )
    step_events = [e for e in events if e.get("kind") == "step" and "验证码" in str(e.get("label"))]
    assert step_events, "未发出验证码步骤事件"
    assert "失败：未找到验证码图片" in step_events[-1]["label"]
    failed = [s for s in report["steps"] if s["action"] == "solve_captcha" and not s["ok"]]
    assert failed and "未找到验证码图片" in (failed[0]["error"] or "")


@pytest.mark.asyncio
async def test_loop_prompt_includes_page_text_excerpt(monkeypatch, patch_candidates):
    """页面文本节选进入轮次提示（toast/校验/数据文案可被模型如实记录）。"""
    patch_candidates([_candidate("e9")])
    seen_prompts: list[str] = []

    browser = FakeCaptchaBrowser()

    async def snap():
        return (
            "### Snapshot\n- heading \"工作台\" [ref=e3]\n- text: 保存成功\n"
            "- cell \"客户2号\" [ref=e9]\n- generic [ref=e10]: ERR_X\n- button \"保存\" [ref=e11]"
        )

    browser.snapshot = snap  # type: ignore[assignment]

    async def _llm(messages):
        seen_prompts.append(messages[1]["content"])
        return json.dumps({"thinking": "完成", "action": "done", "summary": "ok"}, ensure_ascii=False)

    base = dict(url="http://site.local", goal="登录", username="admin", password="x")
    await run_exploration(BrowseConfig(**base), llm=_llm, browser=browser)
    assert seen_prompts, "llm 未被调用"
    p = seen_prompts[-1]
    assert "页面文本（节选" in p
    assert "保存成功" in p and "工作台" in p and "客户2号" in p
    # 交互元素（按钮）不进入文本节选（由候选列表承载）
    assert "保存\" [ref=e11]" not in p


@pytest.mark.asyncio
async def test_loop_solve_captcha_disabled(monkeypatch, patch_candidates):
    patch_candidates([_candidate("e9")])
    browser = FakeCaptchaBrowser()
    report = await _run(monkeypatch, browser, [
        {"thinking": "识别验证码", "action": "solve_captcha", "ref": "e9"},
        {"thinking": "完成", "action": "done", "summary": "skip"},
    ], captcha_enabled=False)
    steps = [s for s in report["steps"] if s["action"] == "solve_captcha"]
    assert steps and not steps[0]["ok"] and "已关闭" in steps[0]["error"]
    assert all(a.get("action") == "goto" for a in browser.actions)  # 除打开起始页外无任何动作
