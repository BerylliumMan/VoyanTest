"""037 T032：验证码找图兜底（整页截图交模型读图）。

场景：验证码不是 <img>/<canvas>（如 div + CSS 背景图 / SVG），现有找图 JS 报
「未找到验证码图片」→ 回退：整页截图 → page_solve_fn（模型读整页图找验证码）→
填入输入框 → 校验成功。无 page_solve_fn 时保持既有失败路径（如实报错）。
"""
from __future__ import annotations

import pytest

from core.captcha import solve_captcha_via_browser


class _FakeBrowser:
    """按 JS 内容分流：找图失败 → 整页截图兜底。"""

    def __init__(self, *, capture_ok: bool):
        self.capture_ok = capture_ok
        self.acted: list[dict] = []
        self.shots: list[str] = []

    async def evaluate_json(self, fn: str):
        if "const CAPRE" in fn or "const imgs" in fn:
            # 找图 JS（capture）；vt-imgopt 标记 = image_optional 模式（只标记输入框）
            if "vt-imgopt" in fn:
                return {"ok": True, "img": "", "has_input": True, "maxlength": 4}
            if self.capture_ok:
                return {"ok": True, "img": "data:image/png;base64,AAAA",
                        "has_input": True, "maxlength": 4}
            return {"ok": False, "error": "未找到验证码图片（img/canvas）"}
        if "any_hit" in fn:  # verify JS
            return {"ok": True}
        if "写入失败" in fn:  # fill JS
            return {"ok": True}
        return {"ok": True}

    async def act(self, action: dict):
        self.acted.append(dict(action))
        return {"success": True}

    async def screenshot(self, path: str):
        self.shots.append(path)
        # 写一个假 png（frame_to_data_url 读取用）
        with open(path, "wb") as f:
            f.write(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
                b"\x00\x00\x00\x03\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82"
            )
        return path


@pytest.mark.asyncio
async def test_page_fallback_when_image_not_found():
    b = _FakeBrowser(capture_ok=False)
    page_calls = []

    async def page_solver(data_url: str):
        page_calls.append(data_url)
        return "F7K2", ""

    res = await solve_captcha_via_browser(
        b, input_ref=None, refresh=False,
        solve_fn=lambda messages: ("", "不应走到小图识别"),
        page_solve_fn=page_solver,
        username="admin", password="x",
    )
    assert res.get("ok") is True, res
    assert page_calls, "整页兜底应被调用"
    assert b.shots, "应拍摄整页截图"
    assert res.get("source") == "page"
    assert "F7K2" not in str(res.get("note") or "")  # 内容不入 note（只记长度）


@pytest.mark.asyncio
async def test_page_fallback_not_used_when_capture_ok():
    b = _FakeBrowser(capture_ok=True)
    page_calls = []

    async def page_solver(data_url: str):
        page_calls.append(data_url)
        return "ZZZZ", ""

    res = await solve_captcha_via_browser(
        b, input_ref="e9", refresh=False,
        solve_fn=lambda messages: ("A1B2", ""),
        page_solve_fn=page_solver,
        username="", password="",
    )
    assert res.get("ok") is True
    assert not page_calls, "找图成功时不应触发整页兜底"
    assert res.get("source") != "page"


@pytest.mark.asyncio
async def test_no_page_fallback_keeps_failure():
    b = _FakeBrowser(capture_ok=False)
    res = await solve_captcha_via_browser(
        b, input_ref=None, refresh=False,
        solve_fn=lambda messages: ("", "x"),
        username="", password="",
    )
    assert res.get("ok") is False
    assert "未找到验证码图片" in str(res.get("error") or "")


@pytest.mark.asyncio
async def test_page_solver_failure_reports_honestly():
    b = _FakeBrowser(capture_ok=False)

    async def page_solver(data_url: str):
        return "", "模型未能从整页截图中读出验证码"

    res = await solve_captcha_via_browser(
        b, input_ref=None, refresh=False,
        solve_fn=lambda messages: ("", "x"),
        page_solve_fn=page_solver,
        username="", password="",
    )
    assert res.get("ok") is False
    assert "整页截图" in str(res.get("error") or "") or "读出验证码" in str(res.get("error") or "")
