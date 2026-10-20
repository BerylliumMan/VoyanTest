"""037 T029：探索链坐标/鼠标操作（click_xy / move_mouse / drag_xy / mouse_down / mouse_up）。

覆盖：白名单放行、分发透传（value 坐标格式校验，不进 ref 校验）、动作标签、
非法坐标如实记失败、护栏（无名称反解拦截——信任模型 Q2）。
"""
from __future__ import annotations

import json

import pytest


def test_whitelist_contains_coordinate_actions():
    from core.chat.browse import ACTION_WHITELIST

    for a in ("click_xy", "move_mouse", "drag_xy", "mouse_down", "mouse_up"):
        assert a in ACTION_WHITELIST, a


def test_action_label_coordinates():
    from core.chat.browse import _action_label

    assert "120" in _action_label("click_xy", {"value": "120,340"}, [])
    assert "120" in _action_label("move_mouse", {"value": "120,340"}, [])
    assert "拖" in _action_label("drag_xy", {"value": "10,20,300,20"}, [])
    assert "按住" in _action_label("mouse_down", {"value": "left"}, [])
    assert "松开" in _action_label("mouse_up", {"value": "left"}, [])


class _FakeBrowser:
    """run_exploration 需要的浏览器接口（服务端语义）。"""

    label = "fake"

    def __init__(self, *, shot_size=(1, 1), viewport=(1152, 648)):
        self.acted: list[dict] = []
        self.shot_size = shot_size
        self.viewport = viewport

    async def start(self):
        return None

    async def stop(self):
        return None

    async def snapshot(self):
        return "- button \"OK\" [ref=e1]"

    async def page_facts(self):
        return {"url": "http://x/", "title": "t"}

    async def evaluate_json(self, fn: str):
        if "innerWidth" in fn:
            return list(self.viewport)
        return {"ok": True}

    async def act(self, action):
        self.acted.append(dict(action))
        return {"success": True}

    async def screenshot(self, path: str):
        try:
            from PIL import Image

            Image.new("RGB", tuple(self.shot_size), (255, 255, 255)).save(path)
        except Exception:
            open(path, "wb").write(b"\x89PNG\r\n\x1a\n")
        return path


def _decisions_llm(items):
    """把决策 dict 列表包成 llm(messages)->str（JSON 字符串，与真实模型一致）。"""
    queue = [dict(i) for i in items]

    async def _llm(messages, **kw):
        if not queue:
            return json.dumps({"thinking": "done", "action": "done", "summary": "ok"})
        return json.dumps(queue.pop(0), ensure_ascii=False)

    return _llm


@pytest.mark.asyncio
async def test_click_xy_dispatch_passthrough():
    """click_xy 分发：value 透传 browser.act，不经 ref 校验。"""
    from core.chat.browse import BrowseConfig, run_exploration

    browser = _FakeBrowser()
    cfg = BrowseConfig(url="http://x/", goal="点选验证码", max_steps=2, timeout_s=60,
                       vision=False, captcha_enabled=False)
    llm = _decisions_llm([
        {"thinking": "点选验证码", "action": "click_xy", "value": "120,340"},
    ])
    report = await run_exploration(cfg, llm=llm, browser=browser)
    acted = [a for a in browser.acted if a.get("action") == "click_xy"]
    assert acted and acted[0].get("value") == "120,340"
    steps = [s for s in (report.get("steps") or []) if s.get("action") == "click_xy"]
    assert steps and steps[0]["ok"] is True


@pytest.mark.asyncio
async def test_drag_xy_and_mouse_pair_passthrough():
    from core.chat.browse import BrowseConfig, run_exploration

    browser = _FakeBrowser()
    cfg = BrowseConfig(url="http://x/", goal="滑块", max_steps=3, timeout_s=60,
                       vision=False, captcha_enabled=False)
    llm = _decisions_llm([
        {"thinking": "按住", "action": "mouse_down", "value": "left"},
        {"thinking": "拖拽", "action": "drag_xy", "value": "10,20,300,20"},
        {"thinking": "松开", "action": "mouse_up", "value": "left"},
    ])
    report = await run_exploration(cfg, llm=llm, browser=browser)
    kinds = [a.get("action") for a in browser.acted]
    assert "mouse_down" in kinds and "drag_xy" in kinds and "mouse_up" in kinds
    drag = next(a for a in browser.acted if a.get("action") == "drag_xy")
    assert drag.get("value") == "10,20,300,20"
    assert report.get("ok") is True


@pytest.mark.asyncio
async def test_click_xy_invalid_value_records_failure():
    from core.chat.browse import BrowseConfig, run_exploration

    browser = _FakeBrowser()
    cfg = BrowseConfig(url="http://x/", goal="坏坐标", max_steps=2, timeout_s=60,
                       vision=False, captcha_enabled=False)
    llm = _decisions_llm([
        {"thinking": "坏坐标", "action": "click_xy", "value": "abc"},
    ])
    report = await run_exploration(cfg, llm=llm, browser=browser)
    # 坏坐标不派发（act 未被调用）；journal 如实记失败
    assert not [a for a in browser.acted if a.get("action") == "click_xy"]
    steps = [s for s in (report.get("steps") or []) if s.get("action") == "click_xy"]
    assert steps and steps[0]["ok"] is False
    assert "坐标" in (steps[0].get("error") or "")


@pytest.mark.asyncio
async def test_click_xy_scaled_to_page_coords():
    """037 US4 修复：模型读压缩截图的像素坐标 → 按缩放换算为页面坐标后派发。

    截图 576x324（压缩后） vs 视口 1152x648 → scale=2.0；
    模型给出 "100,50"（图像像素）→ 实际派发 "200,100"（页面坐标）。
    """
    from core.chat.browse import BrowseConfig, run_exploration

    browser = _FakeBrowser(shot_size=(576, 324), viewport=(1152, 648))
    cfg = BrowseConfig(url="http://x/", goal="缩放换算", max_steps=2, timeout_s=60,
                       vision=True, captcha_enabled=False)
    llm = _decisions_llm([
        {"thinking": "点坐标", "action": "click_xy", "value": "100,50"},
    ])
    report = await run_exploration(cfg, llm=llm, browser=browser)
    acted = [a for a in browser.acted if a.get("action") == "click_xy"]
    assert acted and acted[0].get("value") == "200,100", acted
    assert report.get("ok") is True


@pytest.mark.asyncio
async def test_drag_xy_scaled_to_page_coords():
    from core.chat.browse import BrowseConfig, run_exploration

    browser = _FakeBrowser(shot_size=(576, 324), viewport=(1152, 648))
    cfg = BrowseConfig(url="http://x/", goal="缩放拖拽", max_steps=2, timeout_s=60,
                       vision=True, captcha_enabled=False)
    llm = _decisions_llm([
        {"thinking": "拖", "action": "drag_xy", "value": "10,20,300,20"},
    ])
    report = await run_exploration(cfg, llm=llm, browser=browser)
    drag = next(a for a in browser.acted if a.get("action") == "drag_xy")
    assert drag.get("value") == "20,40,600,40", drag


@pytest.mark.asyncio
async def test_common_path_actions_do_not_crash():
    """既有 bug 回归：scroll/goto 等公共路径动作在步骤记录处引用 click_text_used
    会 NameError（"cannot access local variable"）——统一前置初始化后必须正常。"""
    from core.chat.browse import BrowseConfig, run_exploration

    browser = _FakeBrowser()
    cfg = BrowseConfig(url="http://x/", goal="公共路径", max_steps=4, timeout_s=60,
                       vision=False, captcha_enabled=False)
    llm = _decisions_llm([
        {"thinking": "滚动", "action": "scroll", "value": "down"},
        {"thinking": "后退", "action": "navigate_back"},
        {"thinking": "等待", "action": "wait", "value": "1"},
    ])
    report = await run_exploration(cfg, llm=llm, browser=browser)
    kinds = [a.get("action") for a in browser.acted]
    assert "scroll" in kinds and "navigate_back" in kinds
    assert report.get("stopped_reason") in ("done", "max_steps", "timeout"), report
    assert "cannot access local variable" not in str(report.get("error") or "")
