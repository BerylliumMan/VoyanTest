# core/captcha.py
"""验证码自动识别与填写（浏览器无关层）——服务端无头 / 客户端可见浏览器通用。

流程（2026-10-14，对话浏览器探索登录）：
1. 页面内 JS 定位验证码图片（img/canvas：按 captcha 关键词 + 与验证码输入框的距离打分），
   可选先点击图片刷新；将图片转 data URL（canvas 绘制，跨域时回退 fetch）；
2. 服务端 OCR（core/captcha_solver，ddddocr）识别文本/算术结果；
3. 填写：优先模型给出的验证码输入框 ref；失败/无标记时回退 JS 原生 setter 写入；
4. 校验：读取标记输入框的值确认写入成功；失败如实返回错误（不伪造成功）。

安全：验证码为一次性人机校验码，仅用于当次表单提交；日志/卡片只记录位数，不记录内容。
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_CAPTCHA_KEY_RE = "验证码|captcha|verif|vcode|checkcode|安全码|校验码"

_CAPTURE_JS_TMPL = """async () => {
  const CAPRE = /(__CAPRE__)/i;
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const attrs = (el) => [el.placeholder, el.name, el.id, el.getAttribute('aria-label'), el.title].filter(Boolean).join(' ');
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
  const capInput = inputs.find((i) => CAPRE.test(attrs(i)));
  const pickImg = () => {
    const els = Array.from(document.querySelectorAll('img,canvas')).filter(vis);
    let best = null; let bestS = -1;
    for (const el of els) {
      let s = 0;
      const at = [el.src, el.id, el.className, el.alt, el.getAttribute('aria-label'), el.title].filter(Boolean).join(' ');
      if (CAPRE.test(String(at))) s += 100;
      const r = el.getBoundingClientRect();
      if (r.width >= 40 && r.width <= 300 && r.height >= 16 && r.height <= 120) s += 25;
      if (el.tagName === 'IMG' && el.naturalWidth && el.naturalWidth <= 320) s += 10;
      if (capInput) {
        const a = capInput.getBoundingClientRect();
        const dx = Math.abs((a.left + a.width / 2) - (r.left + r.width / 2));
        const dy = Math.abs((a.top + a.height / 2) - (r.top + r.height / 2));
        s += Math.max(0, 70 - dx / 6 - dy / 6);
      }
      if (s > bestS) { bestS = s; best = el; }
    }
    return bestS >= 45 ? best : null;
  };
  if (__REFRESH__) {
    const cur = pickImg();
    if (cur) { try { cur.click(); } catch (e) {} await new Promise((r) => setTimeout(r, 1200)); }
  }
  const img = pickImg();
  if (!img) return { ok: false, error: '未找到验证码图片（img/canvas）' };
  let dataUrl = '';
  if (img.tagName === 'CANVAS') {
    try { dataUrl = img.toDataURL('image/png'); } catch (e) { dataUrl = ''; }
  } else {
    try {
      const c = document.createElement('canvas');
      c.width = img.naturalWidth || img.width;
      c.height = img.naturalHeight || img.height;
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      dataUrl = c.toDataURL('image/png');
    } catch (e) {
      try {
        const resp = await fetch(img.src, { credentials: 'include' });
        const blob = await resp.blob();
        dataUrl = await new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result || '')); fr.readAsDataURL(blob); });
      } catch (e2) { dataUrl = ''; }
    }
  }
  if (!dataUrl) return { ok: false, error: '无法读取验证码图片（跨域受限）' };
  if (capInput) {
    capInput.setAttribute('data-vt-cap', '1');
    capInput.setAttribute('autocomplete', 'off');
    try {
      if (capInput.value) {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(capInput, '');
        capInput.dispatchEvent(new Event('input', { bubbles: true }));
      }
    } catch (e) {}
  }
  return { ok: true, img: dataUrl, has_input: !!capInput };
}"""

_VERIFY_JS_TMPL = """() => {
  const marked = document.querySelector('[data-vt-cap="1"]');
  const inputs = Array.from(document.querySelectorAll('input'));
  const hit = inputs.find((i) => i.value === __VALUE__);
  return { ok: !!marked && marked.value === __VALUE__, any_hit: !!hit, marked_found: !!marked };
}"""

_JS_FILL_TMPL = """() => {
  const el = document.querySelector('[data-vt-cap="1"]');
  if (!el) return { ok: false, error: '未找到验证码输入框' };
  try {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(el, __VALUE__);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.focus();
    return { ok: el.value === __VALUE__ };
  } catch (e) { return { ok: false, error: '写入失败' }; }
}"""


def _capture_js(refresh: bool) -> str:
    return (
        _CAPTURE_JS_TMPL
        .replace("__CAPRE__", _CAPTCHA_KEY_RE)
        .replace("__REFRESH__", "true" if refresh else "false")
    )


def _fill_js(value: str) -> str:
    return _JS_FILL_TMPL.replace("__VALUE__", json.dumps(value, ensure_ascii=False))


def _verify_js(value: str) -> str:
    return _VERIFY_JS_TMPL.replace("__VALUE__", json.dumps(value, ensure_ascii=False))


async def solve_captcha_via_browser(
    browser,
    *,
    input_ref: Optional[str] = None,
    refresh: bool = False,
    solve_fn: Optional[Callable[[str], tuple[str, str]]] = None,
) -> dict:
    """识别并填写验证码；返回 ``{"ok": bool, "note"/"error": str, "text_len": int}``。

    ``browser`` 需实现 ``evaluate_json(fn)`` 与 ``act(action)``（服务端/客户端通用）。
    ``solve_fn`` 可注入（测试用）；默认走 ddddocr。
    """
    if solve_fn is None:
        from core.captcha_solver import solve_text_captcha as solve_fn  # type: ignore[no-redef]

    cap = await browser.evaluate_json(_capture_js(refresh))
    if not isinstance(cap, dict) or not cap.get("ok"):
        err = (cap or {}).get("error") if isinstance(cap, dict) else None
        return {"ok": False, "error": str(err or "验证码捕获失败")}

    text, err = solve_fn(str(cap.get("img") or ""))
    if err or not text:
        return {"ok": False, "error": err or "验证码识别失败"}

    has_input = bool(cap.get("has_input"))
    filled = False

    # 1) 优先用模型给出的输入框 ref（快照语义最准确）
    if input_ref:
        try:
            res = await browser.act({"action": "fill", "selector": input_ref, "value": text})
            filled = bool(res.get("success"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("captcha fill via ref failed: %s", exc)
            filled = False

    # 2) 校验 / 回退：写入 JS 标记的验证码输入框
    if has_input:
        verified = await browser.evaluate_json(_verify_js(text))
        if not (isinstance(verified, dict) and verified.get("ok")):
            try:
                js_fill = await browser.evaluate_json(_fill_js(text))
            except Exception:
                js_fill = None
            if isinstance(js_fill, dict) and js_fill.get("ok"):
                filled = True
            elif not filled:
                return {"ok": False, "error": "验证码已识别但填写失败"}
        else:
            filled = True

    if not filled and not has_input:
        return {"ok": False, "error": "验证码已识别但未找到输入框（可先点击验证码输入框后重试）"}

    return {"ok": True, "note": f"已识别并自动填写验证码（{len(text)} 位）", "text_len": len(text)}
