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

    monkeypatch.setattr(captcha_solver, "_get_ocr", lambda: FakeOcr())
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert err == "" and text == "A1b2"


def test_solver_arithmetic_captcha_computed(monkeypatch):
    class FakeOcr:
        def classification(self, payload):
            return "3+4=?"

    monkeypatch.setattr(captcha_solver, "_get_ocr", lambda: FakeOcr())
    text, err = captcha_solver.solve_text_captcha(_IMG_1PX_PNG)
    assert err == "" and text == "7"
    assert captcha_solver.solve_arithmetic("6×7=?") == "42"
    assert captcha_solver.solve_arithmetic("9-4") == "5"
    assert captcha_solver.solve_arithmetic("ABCD") is None


def test_solver_unavailable_degrades(monkeypatch, caplog):
    def _boom():
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

    def __init__(self, *, capture=None, verify=None, js_fill=None, ref_fill_ok=True):
        self.capture = capture if capture is not None else {
            "ok": True, "img": _IMG_1PX_PNG, "has_input": True,
        }
        self.verify = verify if verify is not None else {"ok": True, "any_hit": True, "marked_found": True}
        self.js_fill = js_fill if js_fill is not None else {"ok": True}
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


@pytest.mark.asyncio
async def test_solve_flow_ocr_error():
    browser = FakeCaptchaBrowser()
    result = await solve_captcha_via_browser(browser, input_ref="e9", solve_fn=lambda _: ("", "识别失败"))
    assert not result["ok"] and "识别失败" in result["error"]


def test_capture_js_refresh_flag():
    assert "true" in _capture_js(True) and ".click()" in _capture_js(True)
    assert "false" in _capture_js(False)


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
