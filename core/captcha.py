# core/captcha.py
"""验证码自动识别与填写（浏览器无关层）——服务端无头 / 客户端可见浏览器通用。

流程（2026-10-14 首版；2026-10-19 改为模型读图）：
1. 页面内 JS 定位验证码图片（img/canvas：按 captcha 关键词 + 与验证码输入框的距离打分），
   可选先点击图片刷新；将图片转 data URL（canvas 绘制，跨域时回退 fetch）；
2. 读图分析（ZCode 方式）：验证码图片直接交给多模态大模型（`make_model_captcha_solver`），
   读出字符或计算算术结果（无本地 OCR 组件）；
3. 填写：优先模型给出的验证码输入框 ref；失败/无标记时回退 JS 原生 setter 写入；
4. 校验：读取标记输入框的值确认写入成功；失败如实返回错误（不伪造成功）。

安全：验证码为一次性人机校验码，仅用于当次表单提交；日志/卡片只记录位数，不记录内容。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 识别验证码输入框 / 图片的关键词（2026-10-14 实测加固：补齐国内系统常见命名——
# imgCode / yzm / authCode / kaptcha / randCode / seccode 等此前不命中导致「未找到验证码图片」）
_CAPTCHA_KEY_RE = (
    "验证码|图片码|captcha|kaptcha|verif|vcode|checkcode|check_code|authcode|auth_code|"
    "imgcode|img_code|randcode|rand_code|randimg|rand_img|seccode|sec_code|safe_?code|"
    "安全码|校验码|yzm|verify_img|verifyimage|code_img|codeimg"
)

_CAPTURE_JS_TMPL = """async () => {
  const CAPRE = /(__CAPRE__)/i;
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const attrs = (el) => [el.placeholder, el.name, el.id, el.getAttribute('aria-label'), el.title].filter(Boolean).join(' ');
  const nearText = (el) => {
    let s = '';
    try { const lab = el.closest('label'); if (lab) s += ' ' + (lab.innerText || ''); } catch (e) {}
    try {
      let p = el.parentElement; let hops = 0;
      while (p && hops < 2 && s.length < 160) { s += ' ' + (p.innerText || ''); p = p.parentElement; hops++; }
    } catch (e) {}
    return s;
  };
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
  const capInput = inputs.find((i) => CAPRE.test(attrs(i)) || CAPRE.test(nearText(i)));
  const imgs = Array.from(document.querySelectorAll('img,canvas')).filter(vis);
  const scored = [];
  for (const el of imgs) {
    let s = 0;
    const at = [el.src, el.id, el.className, el.alt, el.getAttribute('aria-label'), el.title, el.name].filter(Boolean).join(' ');
    const kw = CAPRE.test(String(at));
    if (kw) s += 100;
    const r = el.getBoundingClientRect();
    const sizeOk = r.width >= 40 && r.width <= 320 && r.height >= 14 && r.height <= 120;
    if (sizeOk) s += 25;
    if (el.tagName === 'IMG' && el.naturalWidth && el.naturalWidth <= 360) s += 10;
    if (capInput) {
      const a = capInput.getBoundingClientRect();
      const dx = Math.abs((a.left + a.width / 2) - (r.left + r.width / 2));
      const dy = Math.abs((a.top + a.height / 2) - (r.top + r.height / 2));
      s += Math.max(0, 80 - dx / 5 - dy / 5);
    }
    scored.push({ el, s, kw, sizeOk });
  }
  scored.sort((a, b) => b.s - a.s);
  let pickInfo = scored.length ? scored[0] : null;
  // 选择规则：关键词命中 > 输入框邻近（尺寸合理） > 全页仅一张合理小图
  if (!(pickInfo && (pickInfo.kw || (capInput && pickInfo.sizeOk)))) {
    const smalls = scored.filter((c) => c.sizeOk);
    pickInfo = !capInput && smalls.length === 1 ? smalls[0] : null;
  }
  if (__REFRESH__) {
    const cur = pickInfo && pickInfo.el;
    if (cur) { try { cur.click(); } catch (e) {} await new Promise((r) => setTimeout(r, 1200)); }
  }
  const img = pickInfo && pickInfo.el;
  if (!img) {
    if (__IMAGE_OPTIONAL__) {
      let ml0 = 0;
      if (capInput) {
        ml0 = parseInt(capInput.getAttribute('maxlength') || '0', 10) || 0;
        capInput.setAttribute('data-vt-cap', '1');
        capInput.setAttribute('autocomplete', 'off');
      }
      return { ok: true, img: '', has_input: !!capInput, maxlength: ml0 };
    }
    return { ok: false, error: '未找到验证码图片（img/canvas）' };
  }
  // 图片未加载完成时等待（上限 1.5s）
  if (img.tagName === 'IMG' && (!img.complete || !img.naturalWidth)) {
    const t0 = Date.now();
    while ((!img.complete || !img.naturalWidth) && Date.now() - t0 < 1500) {
      await new Promise((r) => setTimeout(r, 120));
    }
    if (!img.naturalWidth) return { ok: false, error: '验证码图片未加载完成（naturalWidth=0）' };
  }
  let dataUrl = '';
  if (img.tagName === 'CANVAS') {
    try { dataUrl = img.toDataURL('image/png'); } catch (e) { dataUrl = ''; }
  } else if (String(img.src || '').startsWith('data:')) {
    dataUrl = img.src;
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
  let maxlength = 0;
  if (capInput) {
    maxlength = parseInt(capInput.getAttribute('maxlength') || '0', 10) || 0;
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
  return { ok: true, img: dataUrl, has_input: !!capInput, maxlength: maxlength };
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

# 引擎侧确定性补填（vt-refill）：登录重试前把「被清空的」账号/密码框补回来——
# 实测缺陷：验证码错误重试时部分站点会重置表单，模型只改验证码直接提交 → 账号为空登录必败。
# 仅在字段为空时补填；只在页面存在 password 框时启用（登录页信号），不猜测其他表单。
_REFILL_JS_TMPL = """() => { /* vt-refill */
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const setVal = (el, v) => {
    try {
      const st = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      st.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return el.value === v;
    } catch (e) { return false; }
  };
  const out = { user: false, pass: false };
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
  const pw = inputs.find((i) => (i.type || '').toLowerCase() === 'password');
  if (!pw) return out;
  const pwd = __PASSWORD__;
  const usr = __USERNAME__;
  if (pwd && !pw.value) out.pass = setVal(pw, pwd);
  if (usr) {
    const idx = inputs.indexOf(pw);
    const cands = inputs.slice(Math.max(0, idx - 2), idx);
    const u = cands.find((i) => !i.value && ['', 'text', 'email', 'tel', 'search'].includes((i.type || '').toLowerCase()));
    if (u) out.user = setVal(u, usr);
  }
  return out;
}"""


def _capture_js(refresh: bool, image_optional: bool = False) -> str:
    return (
        _CAPTURE_JS_TMPL
        .replace("__CAPRE__", _CAPTCHA_KEY_RE)
        .replace("__REFRESH__", "true" if refresh else "false")
        .replace("__IMAGE_OPTIONAL__", "true /*vt-imgopt*/" if image_optional else "false")
    )


def _fill_js(value: str) -> str:
    return _JS_FILL_TMPL.replace("__VALUE__", json.dumps(value, ensure_ascii=False))


def _verify_js(value: str) -> str:
    return _VERIFY_JS_TMPL.replace("__VALUE__", json.dumps(value, ensure_ascii=False))


def _refill_js(username: str, password: str) -> str:
    return (
        _REFILL_JS_TMPL
        .replace("__USERNAME__", json.dumps(username or "", ensure_ascii=False))
        .replace("__PASSWORD__", json.dumps(password or "", ensure_ascii=False))
    )


# ----------------------------
# 模型读图分析（ZCode 方式，2026-10-19）：验证码图片直接交给多模态大模型。
# 不保留本地 OCR 组件；识别失败由探索流程如实报告并刷新重试。
# ----------------------------

_CAPTCHA_PAGE_VISION_PROMPT = (
    "这是一张网页整页截图。页面中有一个验证码图片/区域（通常在登录表单附近；可能是 "
    "img/canvas/div 背景图/SVG 等任意形式），内容可能是数字、字母或算术题。\n"
    "请在整页截图中找到验证码区域并读出内容：若是算术题（如 3+5=?）只回复计算结果（如 8）；"
    "若是字符只回复字符本身（区分大小写）。\n只回复验证码内容本身，不要任何其他文字、解释或标点。"
)

_CAPTCHA_VISION_PROMPT = (
    "这是一张验证码图片。请分析图片内容，并只回复需要填入输入框的答案：\n"
    "1) 若图中是算术题（如「4+7=?」），只回复计算结果（纯数字，如 11）；\n"
    "2) 若图中是字符验证码，只回复图中字符（区分大小写，不要多余字符）；\n"
    "只回复答案本身，不要解释、不要标点、不要引号。"
)


def _parse_model_captcha_reply(raw: str, prefer_len: int | None = None) -> str:
    """从模型回复中提取验证码答案（模型按提示只回复答案时直接命中；其余为兜底解析）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    # 1) 整体即答案（容忍首尾标点/空白；须为纯 ASCII 字母数字）
    core = s.strip(" \t\r\n。．.，,：:；;！!？?\"'“”‘’（）()")
    compact = core.replace(" ", "")
    if compact and compact.isascii() and compact.isalnum() and len(compact) <= 12:
        if prefer_len and len(compact) > prefer_len:
            compact = compact[:prefer_len]
        return compact
    # 2) 模型复述了算术式（含 = 或 ?）→ 兜底计算
    if "=" in s or "?" in s or "？" in s:
        m = re.search(r"(\d{1,3})\s*([+\-×xX*])\s*(\d{1,3})", s)
        if m:
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            if op == "+":
                return str(a + b)
            if op == "-":
                return str(a - b)
            return str(a * b)
    # 3) 取最后一个长度≥2 的字母数字片段（跳过解释词/计数）
    tokens = re.findall(r"[A-Za-z0-9]+", s)
    if not tokens:
        return ""
    cands = [t for t in tokens if len(t) >= 2] or tokens
    value = cands[-1]
    if len(value) > 12:
        return ""
    if prefer_len and len(value) > prefer_len:
        value = value[:prefer_len]
    return value


def make_model_captcha_solver(llm, *, page_mode: bool = False):
    """构造「模型读图」验证码识别器（ZCode 方式）。

    ``llm(messages) -> str``：多模态模型调用（messages 支持 image_url content part）。
    ``page_mode=True``（037 US5）：整页截图模式——提示词改为「在整页截图中找验证码并读出」，
    供找图失败兜底路径使用（``page_solve_fn``）。
    无本地 OCR 兜底：模型调用失败或未读出答案时如实返回错误（由探索流程刷新重试）。
    """
    _prompt = _CAPTCHA_PAGE_VISION_PROMPT if page_mode else _CAPTCHA_VISION_PROMPT

    async def _solve(img_data_url: str, *, prefer_len: int | None = None) -> tuple[str, str]:
        hint = f"\n（提示：输入框最多 {int(prefer_len)} 个字符）" if prefer_len else ""
        try:
            raw = await llm([
                {"role": "system", "content": "你是验证码识别助手，只回复要填入输入框的答案本身。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _prompt + hint},
                        {"type": "image_url", "image_url": {"url": img_data_url}},
                    ],
                },
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning("captcha model solve failed: %s", str(exc)[:200])
            return "", f"模型验证码分析失败（{type(exc).__name__}）"
        value = _parse_model_captcha_reply(str(raw or ""), prefer_len)
        if value:
            return value, ""
        return "", "模型未能从验证码图片中读出答案"

    return _solve


async def solve_captcha_via_browser(
    browser,
    *,
    input_ref: Optional[str] = None,
    refresh: bool = False,
    solve_fn: Optional[Callable[..., tuple[str, str]]] = None,
    page_solve_fn: Optional[Callable[[str], tuple[str, str]]] = None,
    username: str = "",
    password: str = "",
) -> dict:
    """识别并填写验证码；返回 ``{"ok": bool, "note"/"error": str, "text_len": int}``。

    ``page_solve_fn``（037 US5）：找图失败（非常规验证码：CSS 背景图/SVG/div 装扮）时的
    整页截图兜底识别器——整页截图交模型找验证码并读出答案（data URL 入参）。

    ``browser`` 需实现 ``evaluate_json(fn)`` 与 ``act(action)``（服务端/客户端通用）。
    ``solve_fn``：验证码识别器（ZCode 方式=模型读图，``make_model_captcha_solver``；异步函数亦可）。
    ``username``/``password``：登录重试前确定性补填「被站点清空的」账号/密码框（仅空字段）。
    """
    if solve_fn is None:
        return {"ok": False, "error": "验证码识别器未配置（需传入模型读图识别器）"}

    accepts_prefer_len = False
    try:
        import inspect

        accepts_prefer_len = "prefer_len" in inspect.signature(solve_fn).parameters
    except (TypeError, ValueError):
        accepts_prefer_len = False

    refilled = {"user": False, "pass": False}

    async def _refill() -> None:
        if not username and not password:
            return
        try:
            r = await browser.evaluate_json(_refill_js(username, password))
        except Exception:
            return
        if isinstance(r, dict):
            refilled["user"] = refilled["user"] or bool(r.get("user"))
            refilled["pass"] = refilled["pass"] or bool(r.get("pass"))

    async def _capture(with_refresh: bool, image_optional: bool = False) -> dict:
        await _refill()  # 重试/首次都先补回空白的账号密码（实测：站点重试会重置表单）
        cap = await browser.evaluate_json(_capture_js(with_refresh, image_optional))
        return cap if isinstance(cap, dict) else {}

    async def _page_fallback() -> Optional[dict]:
        """037 US5：整页截图 → 模型找验证码读答案 → 填入（find 失败场景专用）。"""
        import inspect as _inspect
        import os as _os
        import tempfile as _tf

        # 1) 标记输入框（找图失败时仍可定位输入框：image_optional 模式）
        cap0 = await _capture(False, image_optional=True)
        has_input = bool(cap0.get("has_input")) if cap0.get("ok") else False
        try:
            ml0 = int(cap0.get("maxlength") or 0)
        except (TypeError, ValueError):
            ml0 = 0
        # 2) 整页截图 → data URL
        path = _os.path.join(_tf.gettempdir(), f"vt_cap_page_{_os.getpid()}.png")
        try:
            shot = await browser.screenshot(path)
        except Exception:
            shot = None
        if not shot:
            return {"ok": False, "error": "整页截图失败（无法读取验证码）"}
        try:
            from core.vision_frame import frame_to_data_url

            data_url = frame_to_data_url(str(shot), max_width=1152) or ""
        except Exception:
            data_url = ""
        if not data_url:
            return {"ok": False, "error": "整页截图处理失败（无法读取验证码）"}
        # 3) 模型读整页图
        try:
            out = page_solve_fn(data_url)
            if _inspect.isawaitable(out):
                out = await out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"整页截图兜底识别失败（{type(exc).__name__}）"}
        text, err = (out if isinstance(out, tuple) else ("", "识别器返回异常"))
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": f"整页截图兜底：{err or '模型未能从整页截图中读出验证码'}"}
        if ml0 and len(text) > ml0:
            text = text[:ml0]
        # 4) 填入（input_ref 优先 → JS 校验/回退）
        filled = False
        if input_ref:
            try:
                res = await browser.act({"action": "fill", "selector": input_ref, "value": text})
                filled = bool(res.get("success"))
            except Exception:
                filled = False
        if has_input:
            try:
                verified = await browser.evaluate_json(_verify_js(text))
            except Exception:
                verified = None
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
        return {
            "ok": True,
            "note": f"已识别并自动填写验证码（{len(text)} 位，整页截图兜底）",
            "text_len": len(text),
            "source": "page",
        }

    async def _solve(img_data_url: str, prefer: int | None) -> tuple[str, str]:
        import inspect as _inspect

        if prefer and accepts_prefer_len:
            out = solve_fn(img_data_url, prefer_len=prefer)
        else:
            out = solve_fn(img_data_url)
        if _inspect.isawaitable(out):  # 异步识别器（如模型读图）需等待
            out = await out
        return out

    cap = await _capture(refresh)
    last_err = str(cap.get("error") or "验证码捕获失败")

    # 图片未出现（懒加载/接口竞态）：刷新一次再抓（上限 2 次尝试）
    if not cap.get("ok") and "未找到验证码图片" in last_err:
        import asyncio as _aio

        await _aio.sleep(1.0)
        cap = await _capture(True)
        last_err = str(cap.get("error") or last_err)

    if not cap.get("ok"):
        # 037 US5：找图失败（非常规验证码）→ 整页截图交模型读图兜底
        if page_solve_fn is not None and "未找到验证码图片" in last_err:
            fb = await _page_fallback()
            if fb is not None:
                return fb
        return {"ok": False, "error": last_err}

    try:
        prefer_len = int(cap.get("maxlength") or 0) or None
    except (TypeError, ValueError):
        prefer_len = None

    text, err = await _solve(str(cap.get("img") or ""), prefer_len)

    # 识别为空/异常：刷新验证码重试一次（上限 2 次尝试）
    if err or not text:
        import asyncio as _aio

        cap2 = await _capture(True)
        if cap2.get("ok"):
            try:
                prefer_len2 = int(cap2.get("maxlength") or 0) or prefer_len
            except (TypeError, ValueError):
                prefer_len2 = prefer_len
            text2, err2 = await _solve(str(cap2.get("img") or ""), prefer_len2)
            if text2 and not err2:
                cap, text, err = cap2, text2, ""
            else:
                err = err2 or err
        if err or not text:
            return {"ok": False, "error": err or "验证码识别失败"}

    # 长度约束：识别结果长于输入框 maxlength 时截断（噪声粘连导致的常见超标）
    clamped = False
    try:
        ml = int(cap.get("maxlength") or 0)
    except (TypeError, ValueError):
        ml = 0
    if ml and len(text) > ml:
        text = text[:ml]
        clamped = True

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

    note = f"已识别并自动填写验证码（{len(text)} 位）"
    if refilled["user"] or refilled["pass"]:
        who = "账号和密码" if (refilled["user"] and refilled["pass"]) else ("账号" if refilled["user"] else "密码")
        note += f"（{who}输入框为空，已自动补回）"
    if clamped:
        note += "（识别结果超过输入框长度上限，已截断）"
    return {"ok": True, "note": note, "text_len": len(text)}
