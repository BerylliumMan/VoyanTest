# core/cdp_session.py
"""
CDP recording session orchestrator.

通过 Chrome DevTools Protocol (CDP) 监听浏览器交互事件，并将原始 CDP
事件转换为结构化的 RecordedEvent 列表，供 core/cdp_converter.py (T2)
进一步转换为可执行的测试步骤。

支持的 event_type:
  - navigation:   页面导航（Page.frameNavigated / Page.loadEventFired）
  - click:        元素点击（通过注入的 JS 监听器捕获）
  - input:        输入框文本输入（通过注入的 JS 监听器捕获）
  - select:       下拉框选择（change 事件，target.tagName === 'SELECT'）
  - screenshot:   主动截图
  - wait:         等待动作
  - assertion:    断言/验证动作

输入 page_or_cdp_url 可以是：
  - PlaywrightMCPManager 实例（使用其 call_tool 桥接到 MCP 的 CDP 工具）
  - playwright.async_api.Page 实例（使用 page.context.new_cdp_session）
  - CDP WebSocket URL 字符串（"ws://..."）
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 事件类型白名单（与 RecordedEvent.event_type 保持一致）
VALID_EVENT_TYPES: frozenset[str] = frozenset({
    "navigation",
    "click",
    "input",
    "select",
    "screenshot",
    "wait",
    "assertion",
})

# CDP method → event_type 映射（针对原生 CDP 事件）
_CDP_METHOD_TO_EVENT: dict[str, str] = {
    "Page.frameNavigated": "navigation",
    "Page.navigatedWithinDocument": "navigation",
    "Page.loadEventFired": "navigation",
    "Page.domContentEventFired": "navigation",
}

# 注入到页面中的 JS 监听器：把用户交互上报为 __cdp_recorder__ event，
# 由 Runtime.consoleAPICalled 接收并转换为 RecordedEvent。
_INJECT_RECORDER_SCRIPT = r"""
(function () {
  if (window.__cdp_recorder_installed__) return;
  window.__cdp_recorder_installed__ = true;

  function report(type, payload) {
    try {
      var detail = Object.assign({__recorder_type__: type}, payload || {});
      console.log("__CDP_RECORDER__:" + JSON.stringify(detail));
    } catch (e) { /* ignore serialization errors */ }
  }

  /**
   * 多层级元素选择器生成策略
   *
   * 五层优先级（从高到低）：
   *   L1 唯一标识符：id / data-testid / name / aria-label
   *   L2 组合属性：   tag.class / tag[type] / tag[aria-label]
   *   L3 结构上下文： parent > child (2-3层) / nth-of-type
   *   L4 文本匹配：   tag:has-text("精确") / tag:has-text("部分")
   *   L5 兜底：      tag
   *
   * 每一层生成 selector 后，能通过 querySelectorAll 验证的会验证唯一性，
   * :has-text() 是 Playwright 扩展，无法用 querySelectorAll 验证，但
   * 回放时 Playwright 原生支持。
   */

  // ---- CSS.escape 轻量 polyfill ----
  function _cssEscape(val) {
    if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(val);
    // 简单实现：对常见特殊字符转义，足够处理 id/class 值
    return ('' + val).replace(/[!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~]/g, '\\$&');
  }

  // Element UI / Plus 动态 id（每次打开页面都会变，禁止固化）
  function _isEphemeralId(id) {
    if (!id || typeof id !== 'string') return false;
    if (/^el-(popover|popper|tooltip|message|notification|dialog|drawer|select)-\d+/i.test(id)) {
      return true;
    }
    // 其它 el-xxx-数字 临时节点
    if (/^el-[a-z]+-\d+$/i.test(id)) return true;
    return false;
  }

  // ---- 判断 CSS 选择器是否唯一 ----
  function _isUnique(sel) {
    try { return document.querySelectorAll(sel).length === 1; }
    catch (e) { return false; }
  }

  // ---- 过滤有意义的 class 名（跳过自动生成 / 哈希 / 工具类） ----
  function _usefulClasses(el) {
    var all = [];
    try {
      // IE/老旧浏览器 classList 可能不存在
      if (!el.classList) return all;
      all = Array.prototype.slice.call(el.classList);
    } catch (e) { return all; }
    return all.filter(function (c) {
      if (typeof c !== 'string' || c.length < 2 || c.length > 36) return false;
      if (/^\d/.test(c)) return false;       // 以数字开头
      if (/^css-/.test(c)) return false;      // CSS Module
      if (/^_[a-zA-Z]/.test(c)) return false; // _private
      if (/[a-f0-9]{5,}/i.test(c)) return false; // 含长哈希片段
      return true;
    });
  }

  // ---- 返回 nth-of-type 描述符（如 "button:nth-of-type(2)"） ----
  function _nthOfType(el) {
    var tag = (el.tagName || '').toLowerCase();
    if (!tag) return '*';
    var parent = el.parentElement;
    if (!parent || !parent.children) return tag;
    var idx = 1;
    for (var i = 0; i < parent.children.length; i++) {
      if (parent.children[i] === el) break;
      if ((parent.children[i].tagName || '').toLowerCase() === tag) idx++;
    }
    return tag + ':nth-of-type(' + idx + ')';
  }

  // ---- 构建祖先链（最多 depth 层；稳定 id / 语义 class 可作锚点） ----
  function _parentChain(el, depth) {
    var parts = [];
    var cur = el;
    for (var d = 0; d < depth; d++) {
      parts.unshift(_nthOfType(cur));
      cur = cur.parentElement;
      if (!cur || cur === document.body) {
        break;
      }
      // 动态 el-popover-N 等不能当锚点
      if (cur.id && !_isEphemeralId(cur.id)) {
        parts.unshift('#' + _cssEscape(cur.id));
        break;
      }
      // popover/dropdown 用稳定 class 锚点，避免 #el-popover-数字
      var pClasses = _usefulClasses(cur);
      for (var pci = 0; pci < pClasses.length; pci++) {
        var pc = pClasses[pci];
        if (/^(el-popover|el-popper|el-select-dropdown|el-picker-panel|ant-select-dropdown|ant-popover)$/i.test(pc)) {
          var anchor = ((cur.tagName || '').toLowerCase() || 'div') + '.' + pc;
          var trial = [anchor].concat(parts).join(' > ');
          if (_isUnique(trial)) {
            parts.unshift(anchor);
            return parts.join(' > ');
          }
          // 即使不唯一，也优于动态 id；继续拼更深时再用
          parts.unshift('.' + pc);
          return parts.join(' > ');
        }
      }
    }
    return parts.join(' > ');
  }

  // 输入法拼音中间态（如 jing'zhou'shi'yuan），不应固化为最终 value
  function _looksLikeImePinyin(v) {
    if (!v || typeof v !== 'string') return false;
    return /^[a-z]+('[a-z]+)+$/i.test(v.trim());
  }

  // popover 内筛选框常无 placeholder：仅对 input/textarea 借用当前展开的「请选择…」
  function _borrowOpenSelectPlaceholder(el) {
    var tag = ((el && el.tagName) || '').toLowerCase();
    if (tag !== 'input' && tag !== 'textarea') return null;
    var cur = el;
    var inOverlay = false;
    while (cur && cur !== document.body) {
      var id = cur.id || '';
      if (_isEphemeralId(id)) { inOverlay = true; break; }
      var cls = _usefulClasses(cur);
      for (var i = 0; i < cls.length; i++) {
        if (/^(el-popover|el-popper|el-select-dropdown|ant-select-dropdown|ant-popover)$/i.test(cls[i])) {
          inOverlay = true;
          break;
        }
      }
      if (inOverlay) break;
      cur = cur.parentElement;
    }
    if (!inOverlay) return null;
    // 筛选框自身已有「输入关键词…」等 placeholder 时不要借用
    var own = el.getAttribute('placeholder');
    if (own) return null;
    var nodes = document.querySelectorAll('input[placeholder], textarea[placeholder]');
    var fallback = null;
    for (var n = 0; n < nodes.length; n++) {
      var t = nodes[n];
      if (t === el) continue;
      var ph = t.getAttribute('placeholder') || '';
      if (!ph || ph.indexOf('请选择') !== 0) continue;
      var wrap = t.closest
        ? t.closest('.el-select, .el-tree-select, .el-cascader, .ant-select')
        : null;
      if (wrap && (
          wrap.classList.contains('is-focus') ||
          wrap.classList.contains('is-active') ||
          wrap.classList.contains('is-open') ||
          (wrap.getAttribute('aria-expanded') === 'true')
      )) {
        return ph;
      }
      if (!fallback) fallback = ph;
    }
    return fallback;
  }

  function _isOptionLike(el) {
    if (!el || el.nodeType !== 1) return false;
    var role = (el.getAttribute('role') || '').toLowerCase();
    if (role === 'option' || role === 'treeitem' || role === 'menuitem') return true;
    try {
      if (el.classList) {
        if (el.classList.contains('el-tree-node__label') ||
            el.classList.contains('el-tree-node__content') ||
            el.classList.contains('el-select-dropdown__item') ||
            el.classList.contains('el-cascader-node') ||
            el.classList.contains('ant-select-item-option') ||
            el.classList.contains('ant-tree-node-content-wrapper')) {
          return true;
        }
      }
    } catch (e) {}
    return false;
  }

  function _optionText(el) {
    if (!el) return '';
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (t.length > 60) t = t.slice(0, 57) + '...';
    return t;
  }

  // ---- 主函数 ----
  function describe(el) {
    if (!el || el.nodeType !== 1) return null;

    var tag = (el.tagName || '').toLowerCase();
    if (!tag) return null;

    // 下拉/树选项：优先文本，禁止借用 placeholder 成 div[placeholder=…]
    if (_isOptionLike(el)) {
      var optText = _optionText(el);
      if (optText) {
        return tag + ':has-text("' + optText.replace(/"/g, '\\"') + '")';
      }
    }

    // ==================================================================
    // L1: 唯一标识符层（placeholder 优先于动态 id）
    // ==================================================================

    // placeholder 最稳；仅 input/textarea 可借用触发器文案
    var placeholder = el.getAttribute('placeholder');
    if (!placeholder && (tag === 'input' || tag === 'textarea')) {
      placeholder = _borrowOpenSelectPlaceholder(el);
    }
    if (placeholder && (tag === 'input' || tag === 'textarea')) {
      var phSel = tag + '[placeholder="' + placeholder.replace(/"/g, '\\"') + '"]';
      if (_isUnique(phSel) || placeholder.indexOf('请选择') === 0 || placeholder.indexOf('请输入') === 0 || placeholder.indexOf('输入') === 0) {
        return phSel;
      }
    }

    // 稳定 id；跳过 el-popover-N 等临时 id
    if (el.id && !_isEphemeralId(el.id)) {
      return '#' + _cssEscape(el.id);
    }

    var testId = (el.dataset && el.dataset.testid) || el.getAttribute('data-testid');
    if (testId) {
      return '[data-testid="' + testId.replace(/"/g, '\\"') + '"]';
    }

    var elName = el.getAttribute('name');
    if (elName) {
      var nameSel = tag + '[name="' + elName.replace(/"/g, '\\"') + '"]';
      if (_isUnique(nameSel)) return nameSel;
    }

    var ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) {
      var ariaSel = tag + '[aria-label="' + ariaLabel.replace(/"/g, '\\"') + '"]';
      if (_isUnique(ariaSel)) return ariaSel;
    }

    // ==================================================================
    // L2: 组合属性层
    // ==================================================================

    var classes = _usefulClasses(el);

    // 2a: 单 class
    for (var ci = 0; ci < classes.length; ci++) {
      var singleClsSel = tag + '.' + classes[ci];
      if (_isUnique(singleClsSel)) return singleClsSel;
    }

    // 2b: tag[type=xxx] — 适用于 input/button
    var elType = el.getAttribute('type');
    if (elType && (tag === 'input' || tag === 'button')) {
      var typeSel = tag + '[type="' + elType.replace(/"/g, '\\"') + '"]';
      if (_isUnique(typeSel)) return typeSel;
    }

    // 2b2: placeholder + type (when bare placeholder was not unique)
    if (placeholder && elType && (tag === 'input' || tag === 'textarea')) {
      var phTypeSel = tag + '[type="' + elType.replace(/"/g, '\\"') + '"]'
        + '[placeholder="' + placeholder.replace(/"/g, '\\"') + '"]';
      if (_isUnique(phTypeSel)) return phTypeSel;
    }

    // 2c: 双 class 组合（优先语义化的两个）
    if (classes.length >= 2) {
      for (var i = 0; i < Math.min(classes.length, 4); i++) {
        for (var j = i + 1; j < Math.min(classes.length, 4); j++) {
          var pairClsSel = tag + '.' + classes[i] + '.' + classes[j];
          if (_isUnique(pairClsSel)) return pairClsSel;
        }
      }
    }

    // 2d: aria-label 可能已在 L1 失败（不唯一），这里作为兜底组合尝试
    if (ariaLabel) {
      var ariaLblCombo = tag + '[aria-label="' + ariaLabel.replace(/"/g, '\\"') + '"]';
      if (_isUnique(ariaLblCombo)) return ariaLblCombo;
    }

    // ==================================================================
    // L3: 结构上下文层
    // ==================================================================

    // 3a: 若有 class，尝试 parent > tag.class 快速路径
    if (classes.length > 0) {
      var parent = el.parentElement;
      if (parent && parent !== document.body && parent.nodeType === 1) {
        var pTag = (parent.tagName || '').toLowerCase();
        if (pTag) {
          var childDesc = tag + '.' + classes[0];
          var pSel;
          if (parent.id && !_isEphemeralId(parent.id)) {
            pSel = '#' + _cssEscape(parent.id);
          } else {
            pSel = _nthOfType(parent);
          }
          var pcSel = pSel + ' > ' + childDesc;
          if (_isUnique(pcSel)) return pcSel;
          if (classes.length >= 2) {
            var childDesc2 = tag + '.' + classes[0] + '.' + classes[1];
            var pcSel2 = pSel + ' > ' + childDesc2;
            if (_isUnique(pcSel2)) return pcSel2;
          }
        }
      }
    }

    // 3b: 递增祖先链 2→3 层（不会用动态 el-popover id 作锚点）
    for (var depth = 2; depth <= 3; depth++) {
      var chain = _parentChain(el, depth);
      if (chain && chain.indexOf('#el-') === -1 && _isUnique(chain)) return chain;
    }

    // ==================================================================
    // L4: 文本层（Playwright :has-text() 扩展选择器）
    // ==================================================================

    var text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text && text.length > 0) {
      var maxLen = 50;
      var shortText = text.length > maxLen ? text.slice(0, maxLen - 3) + '...' : text;
      var escaped = shortText.replace(/"/g, '\\"');
      return tag + ':has-text("' + escaped + '")';
    }

    // ==================================================================
    // L5: 兜底层 — placeholder 仍优于裸 tag（避免固化 selector:"input"）
    // ==================================================================

    if (placeholder) {
      return tag + '[placeholder="' + placeholder.replace(/"/g, '\\"') + '"]';
    }

    return tag;
  }

  function _elMeta(el) {
    if (!el || el.nodeType !== 1) return {};
    var meta = {tag: (el.tagName || '').toLowerCase()};
    var ph = el.getAttribute('placeholder');
    if (!ph && (meta.tag === 'input' || meta.tag === 'textarea')) {
      ph = _borrowOpenSelectPlaceholder(el);
    }
    if (ph) meta.placeholder = ph;
    var role = el.getAttribute('role');
    if (role) meta.role = role;
    var aria = el.getAttribute('aria-label');
    if (aria) meta.name = aria;
    var txt = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    if (txt) meta.text = txt;
    return meta;
  }

  var _input_timers = {};
  var _input_latest = {};
  var _composing = false;
  var _lastInputSig = {}; // selector -> last reported value
  var _skipClickUntil = 0;

  function _reportInput(el, selector, value, meta) {
    if (value == null) return;
    var v = String(value);
    if (!v.trim()) return; // 空输入不上报
    if (_looksLikeImePinyin(v)) return;
    if (_lastInputSig[selector] === v) return; // 去重：同 selector+value
    _lastInputSig[selector] = v;
    var payload = Object.assign({selector: selector, value: v}, meta || {});
    report("input", payload);
  }

  function _resolveClickTarget(el) {
    var target = el;
    while (target && target !== document.body) {
      if (_isOptionLike(target)) return target;
      var tTag = (target.tagName || "").toLowerCase();
      var tText = (target.textContent || "").trim();
      if (["button", "a", "input", "select", "textarea", "label", "li"].indexOf(tTag) !== -1 ||
          (target.getAttribute("role") || "").indexOf("button") !== -1 ||
          (target.getAttribute("role") || "").indexOf("combobox") !== -1 ||
          (target.getAttribute("role") || "").indexOf("option") !== -1 ||
          (target.getAttribute("role") || "").indexOf("treeitem") !== -1 ||
          target.getAttribute("onclick") ||
          (tTag === "input" && target.getAttribute("placeholder"))) {
        break;
      }
      if (["img", "span", "svg", "i", "em", "b", "strong"].indexOf(tTag) !== -1) {
        // span 可能是树节点文案，若父级是 option-like 则继续上找
        target = target.parentElement;
        continue;
      }
      if (tText.length > 0 && tText.length < 80) {
        break;
      }
      target = target.parentElement;
    }
    if (target === document.body || !target) target = el;
    return target;
  }

  function _emitClick(target) {
    var clickPayload = Object.assign(
      {selector: describe(target)},
      _elMeta(target)
    );
    report("click", clickPayload);
  }

  window.__CDP_RECORDER_FLUSH__ = function () {
    var keys = Object.keys(_input_timers);
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (_input_timers[k]) {
        clearTimeout(_input_timers[k]);
        delete _input_timers[k];
      }
      var pending = _input_latest[k];
      if (pending) {
        _reportInput(null, pending.selector, pending.value, pending.meta || {});
        delete _input_latest[k];
      }
    }
  };

  document.addEventListener("compositionstart", function () {
    _composing = true;
  }, true);

  document.addEventListener("compositionend", function (ev) {
    _composing = false;
    var el = ev.target;
    if (!el) return;
    var tag = (el.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea") return;
    var key = describe(el) || tag;
    var meta = _elMeta(el);
    _input_latest[key] = {selector: key, value: el.value, meta: meta};
    if (_input_timers[key]) clearTimeout(_input_timers[key]);
    _input_timers[key] = setTimeout(function () {
      _reportInput(el, key, el.value, meta);
      delete _input_timers[key];
      delete _input_latest[key];
    }, 200);
  }, true);

  // 下拉选项在 mousedown 时记录（click 时 popover 往往已关闭，会点到触发器）
  document.addEventListener("mousedown", function (ev) {
    var raw = ev.target;
    var target = _resolveClickTarget(raw);
    if (!_isOptionLike(target) && !_isOptionLike(raw)) {
      // 短文本树节点：父链上找 option-like
      var cur = raw;
      var found = null;
      while (cur && cur !== document.body) {
        if (_isOptionLike(cur)) { found = cur; break; }
        cur = cur.parentElement;
      }
      if (!found) return;
      target = found;
    }
    _emitClick(target);
    _skipClickUntil = Date.now() + 800;
  }, true);

  document.addEventListener("click", function (ev) {
    if (Date.now() < _skipClickUntil) return;
    var target = _resolveClickTarget(ev.target);
    _emitClick(target);
  }, true);

  document.addEventListener("input", function (ev) {
    var el = ev.target;
    if (!el) return;
    var tag = (el.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea") return;
    var key = describe(el) || tag;
    var meta = _elMeta(el);
    _input_latest[key] = {selector: key, value: el.value, meta: meta};
    if (_composing || ev.isComposing) return;
    if (_looksLikeImePinyin(el.value)) return;
    if (_input_timers[key]) clearTimeout(_input_timers[key]);
    _input_timers[key] = setTimeout(function() {
      _reportInput(el, key, el.value, meta);
      delete _input_timers[key];
      delete _input_latest[key];
    }, 500);
  }, true);

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (!el) return;
    var tag = (el.tagName || "").toLowerCase();
    if (tag === "select") {
      report("select", Object.assign(
        {selector: describe(el), value: el.value},
        _elMeta(el)
      ));
    }
    if (tag === "input" || tag === "textarea") {
      var key = describe(el) || tag;
      if (_input_timers[key]) {
        clearTimeout(_input_timers[key]);
        delete _input_timers[key];
      }
      delete _input_latest[key];
      _reportInput(el, key, el.value, _elMeta(el));
    }
  }, true);
})();
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RecordedEvent:
    """A single recorded browser event captured via CDP."""

    event_type: str
    timestamp: float
    selector: Optional[str] = None
    value: Optional[str] = None
    url: str = ""
    screenshot: Optional[str] = None
    page_title: str = ""
    # Element metadata from the injected recorder (optional; older events omit these)
    tag: Optional[str] = None
    placeholder: Optional[str] = None
    role: Optional[str] = None
    text: Optional[str] = None
    name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation (omit empty optionals)."""
        raw = asdict(self)
        return {
            k: v
            for k, v in raw.items()
            if v is not None and v != ""
        }

    def is_valid(self) -> bool:
        """Return True if the event_type is recognised."""
        return self.event_type in VALID_EVENT_TYPES


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class CDPRecordingSession:
    """Connects to a Playwright CDP endpoint and records user interactions.

    Lifecycle:
        session = CDPRecordingSession(session_id="abc")
        await session.start_recording(page_or_cdp_url=...)
        ...
        await session.stop_recording()
        events = session.collect_events()
    """

    def __init__(self, session_id: str) -> None:
        self._session_id: str = session_id
        self._events: list[RecordedEvent] = []
        self._recording: bool = False
        self._start_time: Optional[float] = None

        # CDP wiring state (private; not part of the public interface)
        self._cdp_session: Any = None
        self._cdp_url: Optional[str] = None
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._msg_counter: int = 0
        self._attached_manager: Any = None  # PlaywrightMCPManager reference
        self._last_page_url: str = ""
        self._last_page_title: str = ""
        self.last_event_at: float = 0.0

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return max(0.0, time.time() - self._start_time)

    @property
    def events_count(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_recording(self, page_or_cdp_url: Any) -> bool:
        """Start recording browser events from a CDP-capable source.

        Parameters
        ----------
        page_or_cdp_url : Any
            One of:
              - ``playwright.async_api.Page`` instance
              - ``str`` CDP WebSocket URL (``ws://...``)
              - ``PlaywrightMCPManager`` instance (uses MCP ``browser_cdp_session`` tool)

        Returns
        -------
        bool
            True if recording was successfully started, False otherwise.
        """
        if self._recording:
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: start_recording called "
                f"while already recording; ignoring."
            )
            return False

        try:
            ok = await self._attach_cdp(page_or_cdp_url)
            if not ok:
                logger.error(
                    f"CDPRecordingSession[{self._session_id}]: failed to attach CDP."
                )
                return False

            await self._enable_domains()
            await self._install_page_recorder()

            self._recording = True
            self._start_time = time.time()
            logger.info(
                f"CDPRecordingSession[{self._session_id}]: recording started."
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 录制启动涉及 Playwright/CDP/asyncio，任一失败都需清理
            logger.error(
                f"CDPRecordingSession[{self._session_id}]: start_recording failed: {exc}",
                exc_info=True,
            )
            await self._safe_detach()
            self._recording = False
            self._start_time = None
            return False

    async def stop_recording(self) -> bool:
        """Stop recording, release the CDP session, and reset state."""
        if not self._recording:
            logger.debug(
                f"CDPRecordingSession[{self._session_id}]: stop_recording called "
                f"when not recording; no-op."
            )
            return True

        # Flush pending debounced input events before tearing down CDP
        try:
            await self._flush_pending_inputs()
        except Exception as exc:
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: flush inputs failed: {exc}"
            )

        try:
            await self._disable_domains()
        except Exception as exc:  # noqa: BLE001 - 停止录制时关闭域是 best-effort
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: disable domains failed: {exc}"
            )

        try:
            await self._safe_detach()
        except Exception as exc:
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: detach failed: {exc}"
            )

        self._recording = False
        logger.info(
            f"CDPRecordingSession[{self._session_id}]: recording stopped "
            f"({self.events_count} events captured over "
            f"{self.elapsed_seconds:.1f}s)."
        )
        return True

    async def _flush_pending_inputs(self) -> None:
        """Ask the page to flush debounced input timers via injected API."""
        if self._ws is None:
            return
        await self._send_cdp(
            "Runtime.evaluate",
            {
                "expression": (
                    "typeof window.__CDP_RECORDER_FLUSH__ === 'function' "
                    "&& window.__CDP_RECORDER_FLUSH__()"
                ),
                "returnByValue": True,
            },
        )
        # Allow console/Runtime events from flush to be ingested
        await asyncio.sleep(0.15)

    def collect_events(self) -> list[RecordedEvent]:
        """Return a copy of the recorded events and clear the internal buffer.

        Using a copy-then-clear pattern prevents the caller from mutating the
        session's internal state.
        """
        events = list(self._events)
        self._events.clear()
        return events

    def get_events(self) -> list[RecordedEvent]:
        """Return a copy of the recorded events without clearing the internal buffer.

        Unlike :meth:`collect_events`, this method does **not** clear ``_events``,
        so the same events remain available for subsequent calls (e.g. by the
        convert endpoint after the events endpoint has already read them).
        """
        return list(self._events)

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def record_event(self, event_data: dict[str, Any]) -> None:
        """Append a new RecordedEvent built from a raw CDP-style event dict.

        This is the internal ingestion point: domain listeners (Page, Runtime,
        custom console) call this with a normalised dict. The session itself
        is responsible for translating CDP payloads into this shape.

        Expected keys in ``event_data`` (all optional except event_type):
          - event_type: str  (required, must be in VALID_EVENT_TYPES)
          - selector:   str
          - value:      str
          - url:        str
          - screenshot: str  (base64-encoded image data, if any)
          - page_title: str
          - tag / placeholder / role / text / name: element metadata from JS recorder
        Extra keys are ignored. Missing optional keys fall back to defaults.
        """
        if not isinstance(event_data, dict):
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: record_event received "
                f"non-dict payload: {type(event_data).__name__}"
            )
            return

        event_type = str(event_data.get("event_type") or "")
        if event_type not in VALID_EVENT_TYPES:
            logger.debug(
                f"CDPRecordingSession[{self._session_id}]: ignoring event "
                f"with unknown type: {event_type!r}"
            )
            return

        event = RecordedEvent(
            event_type=event_type,
            timestamp=float(event_data.get("timestamp") or time.time()),
            selector=_opt_str(event_data.get("selector")),
            value=(
                None
                if event_data.get("value") is None
                else str(event_data.get("value"))
            ),
            url=str(event_data.get("url") or self._last_page_url or ""),
            screenshot=event_data.get("screenshot"),
            page_title=str(event_data.get("page_title") or self._last_page_title or ""),
            tag=_opt_str(event_data.get("tag")),
            placeholder=_opt_str(event_data.get("placeholder")),
            role=_opt_str(event_data.get("role")),
            text=_opt_str(event_data.get("text")),
            name=_opt_str(event_data.get("name")),
        )
        self._events.append(event)
        self.last_event_at = time.time()
        logger.debug(
            f"CDPRecordingSession[{self._session_id}]: recorded {event.event_type} "
            f"selector={event.selector!r} placeholder={event.placeholder!r} "
            f"value={event.value!r}"
        )

    # ------------------------------------------------------------------
    # CDP attachment (private)
    # ------------------------------------------------------------------

    async def _attach_cdp(self, target: Any) -> bool:
        """Attach to a CDP source. Returns True on success."""
        if target is None:
            logger.error("CDPRecordingSession: start_recording target is None")
            return False

        # 1) PlaywrightMCPManager (use MCP tool to obtain a CDP endpoint)
        if hasattr(target, "call_tool") and hasattr(target, "session"):
            self._attached_manager = target
            try:
                result = await target.call_tool("browser_cdp_session", {})
                cdp_url = self._extract_cdp_url(result)
                if not cdp_url:
                    logger.error(
                        "PlaywrightMCPManager did not return a CDP URL via "
                        "browser_cdp_session tool."
                    )
                    return False
                self._cdp_url = cdp_url
                await self._open_websocket(cdp_url)
                return True
            except Exception as exc:  # noqa: BLE001 - PlaywrightMCP tool call 可能抛任何 MCP 错误
                logger.error(
                    f"Failed to obtain CDP URL from PlaywrightMCPManager: {exc}",
                    exc_info=True,
                )
                return False

        # 2) A raw CDP WebSocket URL string
        if isinstance(target, str):
            self._cdp_url = target
            try:
                await self._open_websocket(target)
                return True
            except Exception as exc:  # noqa: BLE001 - WebSocket connect 失败可能为 ConnectionError / OSError / InvalidHandshake
                logger.error(
                    f"Failed to connect to CDP WebSocket {target}: {exc}",
                    exc_info=True,
                )
                return False

        # 3) A Playwright Page (use new_cdp_session)
        if hasattr(target, "context") and hasattr(target, "evaluate"):
            try:
                cdp = await target.context.new_cdp_session(target)
                self._cdp_session = cdp
                cdp.on("Page.frameNavigated", self._on_page_frame_navigated)
                cdp.on("Page.loadEventFired", self._on_page_load_event_fired)
                cdp.on(
                    "Runtime.consoleAPICalled",
                    self._on_runtime_console_api_called,
                )
                # Capture the initial URL/title so first events are anchored
                try:
                    self._last_page_url = target.url or ""
                    self._last_page_title = await target.title() or ""
                except Exception:  # noqa: BLE001 - 首次 URL/title 抓取失败时静默回退
                    logger.debug(
                        f"CDPRecordingSession[{self._session_id}]: "
                        f"failed to capture initial URL/title"
                    )
                return True
            except Exception as exc:  # noqa: BLE001 - Playwright Page CDP 会话创建可能抛任何错误
                logger.error(
                    f"Failed to create CDP session from Playwright Page: {exc}",
                    exc_info=True,
                )
                return False

        logger.error(
            f"CDPRecordingSession: unsupported start_recording target: "
            f"{type(target).__name__}"
        )
        return False

    async def _open_websocket(self, cdp_url: str) -> None:
        """Open a websocket to a raw CDP URL and start a reader task."""
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "websockets package is required to connect to a raw CDP URL"
            ) from exc

        self._ws = await websockets.connect(cdp_url, max_size=32 * 1024 * 1024)
        self._reader_task = asyncio.create_task(
            self._read_ws_loop(), name=f"cdp-reader-{self._session_id}"
        )

    async def _read_ws_loop(self) -> None:
        """Read messages from the raw CDP websocket and dispatch to handlers."""
        if self._ws is None:
            return
        try:
            async for message in self._ws:
                try:
                    payload = _json.loads(message)
                except (ValueError, TypeError):
                    continue
                await self._dispatch_cdp_message(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - WS reader 循环结束后的清理异常
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: ws reader loop ended: {exc}"
            )

    async def _dispatch_cdp_message(self, payload: dict[str, Any]) -> None:
        """Route a parsed CDP message to the correct internal handler."""
        method = payload.get("method") or ""
        params = payload.get("params") or {}

        if method == "Page.frameNavigated":
            self._on_page_frame_navigated(params)
        elif method == "Page.loadEventFired":
            self._on_page_load_event_fired(params)
        elif method == "Runtime.consoleAPICalled":
            self._on_runtime_console_api_called(params)
        # Other methods are ignored intentionally

    @staticmethod
    def _extract_cdp_url(tool_result: dict[str, Any]) -> Optional[str]:
        """Best-effort extraction of a CDP URL from an MCP tool result."""
        if not isinstance(tool_result, dict):
            return None
        text = tool_result.get("text") or ""
        if isinstance(text, str) and text.startswith("ws"):
            return text.strip()
        for key in ("url", "wsUrl", "webSocketDebuggerUrl", "cdp_url"):
            val = tool_result.get(key)
            if isinstance(val, str) and val.startswith("ws"):
                return val
        return None

    # ------------------------------------------------------------------
    # CDP domain management (private)
    # ------------------------------------------------------------------

    async def _enable_domains(self) -> None:
        """Enable the CDP domains we listen on."""
        for domain in ("Page", "Runtime", "Network"):
            try:
                await self._send_cdp(f"{domain}.enable", {})
            except Exception as exc:  # noqa: BLE001 - 单个域启用失败不阻塞其他域
                logger.warning(
                    f"CDPRecordingSession[{self._session_id}]: failed to enable "
                    f"{domain}: {exc}"
                )

    async def _disable_domains(self) -> None:
        """Disable the CDP domains we previously enabled."""
        for domain in ("Page", "Runtime", "Network"):
            try:
                await self._send_cdp(f"{domain}.disable", {})
            except Exception as exc:  # noqa: BLE001 - 关闭域的 best-effort
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: disable {domain} "
                    f"failed (ignored): {exc}"
                )

    async def _install_page_recorder(self) -> None:
        """Install the JS recorder script on every new document and the current page."""
        try:
            await self._send_cdp(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _INJECT_RECORDER_SCRIPT},
            )
            # Also inject into the current page immediately (addScriptToEvaluateOnNewDocument
            # only applies to future navigations).
            await self._send_cdp(
                "Runtime.evaluate",
                {"expression": _INJECT_RECORDER_SCRIPT, "awaitPromise": False},
            )
        except Exception as exc:  # noqa: BLE001 - 注入 recorder 脚本失败时 recording 仍可继续
            logger.warning(
                f"CDPRecordingSession[{self._session_id}]: failed to install "
                f"page recorder script: {exc}"
            )

    async def _send_cdp(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a CDP command via whichever transport is active."""
        if self._cdp_session is not None:
            result = await self._cdp_session.send(method, params)
            return result if isinstance(result, dict) else {}

        if self._ws is not None:
            msg_id = self._next_msg_id()
            await self._ws.send(_json.dumps({"id": msg_id, "method": method, "params": params}))
            # For fire-and-forget methods we don't block on a response here;
            # responses are matched by id in _dispatch_cdp_message if needed.
            return {}

        raise RuntimeError("CDPRecordingSession: no active CDP transport")

    def _next_msg_id(self) -> int:
        """Allocate a monotonically increasing CDP message id."""
        self._msg_counter += 1
        return self._msg_counter

    async def _safe_detach(self) -> None:
        """Best-effort cleanup of CDP transport resources."""
        if self._reader_task is not None:
            try:
                self._reader_task.cancel()
                await asyncio.gather(self._reader_task, return_exceptions=True)
            except Exception as exc:  # noqa: BLE001 - 资源清理路径：task 取消本身可能抛 CancelledError
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: reader task "
                    f"cancel error (ignored): {exc}"
                )
            self._reader_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001 - WebSocket close 失败属于清理阶段
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: ws close "
                    f"error (ignored): {exc}"
                )
            self._ws = None

        if self._cdp_session is not None:
            try:
                detach = getattr(self._cdp_session, "detach", None)
                if callable(detach):
                    result = detach()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001 - Playwright detach 失败属于清理阶段
                logger.debug(
                    f"CDPRecordingSession[{self._session_id}]: cdp detach "
                    f"error (ignored): {exc}"
                )
            self._cdp_session = None

        self._cdp_url = None
        self._attached_manager = None

    # ------------------------------------------------------------------
    # CDP event handlers (private)
    # ------------------------------------------------------------------

    def _on_page_frame_navigated(self, params: dict[str, Any]) -> None:
        """Handle Page.frameNavigated → record a navigation event."""
        try:
            frame = params.get("frame") or {}
            url = frame.get("url") or self._last_page_url
            self._last_page_url = url or ""
        except (AttributeError, TypeError):  # 防御：参数结构可能与预期不一致
            url = self._last_page_url
        self.record_event({
            "event_type": "navigation",
            "value": url,
            "url": url,
        })

    def _on_page_load_event_fired(self, params: dict[str, Any]) -> None:
        """Handle Page.loadEventFired → record a navigation event (load)."""
        self.record_event({
            "event_type": "navigation",
            "value": self._last_page_url,
            "url": self._last_page_url,
        })

    def _on_runtime_console_api_called(
        self, params: dict[str, Any]
    ) -> None:
        """Handle Runtime.consoleAPICalled → record user-interaction events
        emitted by the injected JS recorder (printed as ``__CDP_RECORDER__:...``).
        """
        try:
            args = params.get("args") or []
            for arg in args:
                value = arg.get("value")
                if not isinstance(value, str):
                    continue
                if not value.startswith("__CDP_RECORDER__:"):
                    continue
                payload = _json.loads(value[len("__CDP_RECORDER__:"):])
                rec_type = payload.pop("__recorder_type__", None)
                if rec_type not in VALID_EVENT_TYPES:
                    continue
                payload.setdefault("url", self._last_page_url)
                payload.setdefault("page_title", self._last_page_title)
                payload["event_type"] = rec_type
                self.record_event(payload)
        except (_json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.debug(
                f"CDPRecordingSession[{self._session_id}]: failed to parse "
                f"recorder console payload: {exc}"
            )
