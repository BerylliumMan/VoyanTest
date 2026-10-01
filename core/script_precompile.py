"""编译先行：把「用例步骤」直接编译成可执行的 Playwright 脚本（无运行日志也能编）。

与 ``script_synthesize``（跑成功后从 journal 合成）不同，这里**没有运行记录**：
完全从步骤描述 + 具体值构造脚本，用于「首跑即脚本」——成功则落库为固化脚本，
下次直接回放；不成功由上层回落到 OTA。

覆盖的步骤句式（与本平台 AI 生成用例的「可执行风格」对齐）：

    打开/访问/导航至/前往/进入 XXX        → goto(base_url 或描述里的 URL)
    在【X】输入框输入「V」/ 填写【X】为「V」  → get_by_label/placeholder/textbox 链 .fill(V)
    点击/单击【X】                        → button/link/text 链 .click()
    在【X】中选择「V」/ 选择【X】中的「V」    → label/combobox/select 链 .select_option(label=V)
    勾选/取消勾选【X】                     → .check() / .uncheck()
    按键 ESC / 按 Enter / 按下 F5          → page.keyboard.press(...)
    等待【X】/ 等待 X 出现                  → get_by_text(X).wait_for(state='visible')
    悬停【X】                             → .hover()
    滚动到【X】                            → .scroll_into_view_if_needed()
    断言页面包含/显示/出现【X】             → expect(get_by_text(X).first).to_be_visible()
    断言页面标题为【X】                     → expect(page).to_have_title(re.compile(...))

无法映射的步骤会被如实记录（返回 unmapped 列表），由上层决定「LLM 兜底」还是「回落 OTA」。
**不放水**：只要有任何一步没映射（尤其是断言步骤），就不是一个完整脚本。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.script_templates import (
    DEFAULT_ACTION_TIMEOUT_MS,
    DEFAULT_NAVIGATION_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

# ── 步骤句式 ──────────────────────────────────────────────────────────────

_RE_FILL = re.compile(
    r"【(?P<target>[^】]+)】[^【】]*?(?:输入|填写|填入|键入)\s*[「『\"'](?P<value>[^」』\"']+)[」』\"']"
)
_RE_FILL_ALT = re.compile(
    r"在?【(?P<target>[^】]+)】[^【】]*?(?:输入|填写|填入)[^「『\"'【]*$"
)
_RE_SELECT = re.compile(
    r"在?【(?P<target>[^】]+)】[^【】]*?(?:选择|选中|选取)\s*[「『\"'](?P<choice>[^」』\"']+)[」』\"']"
)
_RE_SELECT_ALT = re.compile(
    r"选择\s*【(?P<target>[^】]+)】[^【】]*?[「『\"'](?P<choice>[^」』\"']+)[」』\"']"
)
_RE_CHECK = re.compile(r"(?:勾选|选中)【(?P<target>[^】]+)】")
_RE_UNCHECK = re.compile(r"取消(?:勾选|选中)【(?P<target>[^】]+)】")
_RE_HOVER = re.compile(r"悬停(?:在|到)?【(?P<target>[^】]+)】")
_RE_SCROLL = re.compile(r"滚动(?:到|至)【(?P<target>[^】]+)】")
_RE_CLICK = re.compile(r"(?:点击|单击|按一下)【(?P<target>[^】]+)】")
_RE_WAIT_TEXT = re.compile(r"等待[^【】]*?【(?P<text>[^】]+)】")
_RE_KEY = re.compile(
    r"(?:按键|按下|按)\s*(?P<key>ESC|Esc|Enter|回车|空格|Space|Tab|F5|Ctrl\+[A-Za-z])"
)
_RE_ASSERT_TITLE = re.compile(r"断言[^【】]*?标题(?:为|是)【(?P<text>[^】]+)】")
_RE_ASSERT_CONTAINS = re.compile(
    r"断言[^【】]*?(?:包含|显示|出现|看到)【(?P<text>[^】]+)】"
)
_RE_ASSERT_TEXT = re.compile(r"断言[^【】]*?(?:为|是)【(?P<text>[^】]+)】")
_RE_OPEN = re.compile(r"^(?:打开|访问|导航至|前往|进入|跳转到?)(?P<rest>.+)$")
_RE_URL = re.compile(r"https?://[^\s，。；;）)】」'\"']+")

_KEY_MAP = {
    "esc": "Escape",
    "enter": "Enter",
    "回车": "Enter",
    "空格": "Space",
    "space": "Space",
    "tab": "Tab",
    "f5": "F5",
}

_OPEN_HINTS = ("打开", "访问", "导航", "前往", "进入", "跳转")


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _text(step: dict[str, Any]) -> str:
    return str(step.get("description") or step.get("original_description") or "").strip()


def _order(step: dict[str, Any], index: int) -> int:
    return int(step.get("step_order") or step.get("step_number") or index + 1)


def _input_locator(target: str) -> str:
    """输入框：label → placeholder → textbox 角色，链式兜底。"""
    return (
        f"page.get_by_label({_q(target)})"
        f".or_(page.get_by_placeholder({_q(target)}))"
        f".or_(page.get_by_role('textbox', name={_q(target)})).first"
    )


def _click_locator(target: str) -> str:
    """可点目标：button → link → 精确文本，链式兜底。"""
    return (
        f"page.get_by_role('button', name={_q(target)})"
        f".or_(page.get_by_role('link', name={_q(target)}))"
        f".or_(page.get_by_text({_q(target)}, exact=True)).first"
    )


def _select_locator(target: str) -> str:
    """下拉：label → combobox 角色 → 页面第一个 select（兜底）。"""
    return (
        f"page.get_by_label({_q(target)})"
        f".or_(page.get_by_role('combobox', name={_q(target)}))"
        f".or_(page.locator('select').first).first"
    )


def _emit_goto(url: str) -> list[str]:
    return [
        f"    await page.goto({_q(url)}, wait_until='domcontentloaded')",
    ]


def _resolve_open_url(description: str, base_url: str | None) -> str | None:
    m = _RE_URL.search(description)
    if m:
        return m.group(0)
    base = (base_url or "").strip()
    return base or None


def build_script_from_steps(
    *,
    case_id: int,
    case_name: str = "",
    steps: list[dict[str, Any]] | None,
    base_url: str | None = None,
) -> tuple[str | None, list[int]]:
    """把步骤编译成脚本。

    Returns:
        (script, unmapped_orders)：脚本为 None 表示完全无法编译（调用方应走 LLM/OTA）。
        ``unmapped_orders`` 是"没被任何模板覆盖"的步骤号——上层据此决定是否 LLM 兜底。
    """
    steps = list(steps or [])
    if not steps:
        return None, []

    body: list[str] = []
    unmapped: list[int] = []
    emitted_goto = False
    has_assertion = False

    for index, step in enumerate(steps):
        order = _order(step, index)
        desc = _text(step)
        if not desc:
            unmapped.append(order)
            continue

        # 打开/导航（只在尚无 goto 时发；重复打开同址不重复导航）
        open_match = _RE_OPEN.match(desc)
        if open_match:
            url = _resolve_open_url(desc, base_url)
            if url and not emitted_goto:
                body.append(f"    # {order}. {desc}")
                body.extend(_emit_goto(url))
                emitted_goto = True
            elif url and emitted_goto:
                body.append(f"    # {order}. {desc}（已在上方导航）")
            else:
                unmapped.append(order)
            continue

        # 断言：包含/显示/出现
        m = _RE_ASSERT_TITLE.search(desc)
        if m:
            text = m.group("text")
            body.append(f"    # {order}. {desc}")
            body.append(
                f"    await expect(page).to_have_title(re.compile(re.escape({_q(text)}), re.I))"
            )
            has_assertion = True
            continue

        m = _RE_ASSERT_CONTAINS.search(desc)
        if m:
            text = m.group("text")
            body.append(f"    # {order}. {desc}")
            body.append(
                f"    await expect(page.get_by_text({_q(text)}).first).to_be_visible(timeout=15000)"
            )
            has_assertion = True
            continue

        # 断言：…为【X】（单括号、且不是"标题为"）——首括号多为判断对象而非文本，交给 LLM
        if _RE_ASSERT_TEXT.search(desc):
            unmapped.append(order)
            continue

        # 输入
        m = _RE_FILL.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(
                f"    await {_input_locator(m.group('target'))}.fill({_q(m.group('value'))})"
            )
            continue

        # 选择
        m = _RE_SELECT.search(desc) or _RE_SELECT_ALT.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(
                f"    await {_select_locator(m.group('target'))}"
                f".select_option(label={_q(m.group('choice'))})"
            )
            continue

        # 勾选 / 取消勾选
        m = _RE_UNCHECK.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(f"    await {_click_locator(m.group('target'))}.uncheck()")
            continue
        m = _RE_CHECK.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(f"    await {_click_locator(m.group('target'))}.check()")
            continue

        # 悬停 / 滚动
        m = _RE_HOVER.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(f"    await {_click_locator(m.group('target'))}.hover()")
            continue
        m = _RE_SCROLL.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(
                f"    await page.get_by_text({_q(m.group('target'))}).first"
                f".scroll_into_view_if_needed()"
            )
            continue

        # 点击
        m = _RE_CLICK.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(f"    await {_click_locator(m.group('target'))}.click()")
            continue

        # 等待
        m = _RE_WAIT_TEXT.search(desc)
        if m:
            body.append(f"    # {order}. {desc}")
            body.append(
                f"    await page.get_by_text({_q(m.group('text'))}).first"
                f".wait_for(state='visible', timeout=15000)"
            )
            continue

        # 按键
        m = _RE_KEY.search(desc)
        if m:
            key_raw = m.group("key")
            if key_raw.lower().startswith("ctrl+"):
                key = "Control+" + key_raw.split("+", 1)[1].upper()
            else:
                key = _KEY_MAP.get(key_raw.lower(), key_raw)
            body.append(f"    # {order}. {desc}")
            body.append(f"    await page.keyboard.press({_q(key)})")
            continue

        # 填充 ALT（"在【X】输入框输入…" 但值没带引号时：不做，交给 LLM）
        if _RE_FILL_ALT.search(desc):
            unmapped.append(order)
            continue

        unmapped.append(order)

    if not body:
        return None, unmapped

    # 首个动作不是导航时，补一个隐式 goto（脚本自包含；reuse 会话下同样安全）
    if base_url and not emitted_goto:
        probe = " ".join(_text(s) for s in steps[:2])
        if not any(h in probe for h in _OPEN_HINTS):
            body = [f"    # 0. 打开首页（隐式）", *_emit_goto(base_url.strip()), *body]

    if not has_assertion:
        # 没有任何断言 = 不是完整验收脚本，交给上面的调用方处理
        logger.info(
            "precompile: case=%s 无断言步骤，标记不完整（unmapped=%s）",
            case_id, unmapped,
        )

    header = [
        "import re",
        "from playwright.async_api import expect",
        "",
        f"# 编译先行生成（VoyanTest · 步骤直译）case_id={int(case_id)}",
        f"# case_name={_q(case_name or '')}",
        "",
        f"async def test_case_{int(case_id)}(page) -> None:",
        f"    page.set_default_timeout({DEFAULT_ACTION_TIMEOUT_MS})",
        f"    page.set_default_navigation_timeout({DEFAULT_NAVIGATION_TIMEOUT_MS})",
        "",
    ]
    script = "\n".join([*header, *body, ""])
    return script, unmapped


_RE_BRACKET_VALUES = re.compile(r"[「『\"']([^」』\"']{1,})[」』\"']")
_RE_BRACKET_TARGETS = re.compile(r"【([^】]{1,})】")


def extract_step_literals(steps: list[dict[str, Any]] | None) -> list[str]:
    """编译先行的"必须原样出现"字面值：所有「值」与【目标/期望文本】。

    与 prompt 里的 HARD RULE **同源**（要求与校验必须一致）：值（用户名/密码/选项/期望文本）
    原样出现在脚本里；断言判断句里的对象（如"购物车图标徽标数字"）也一并要求，宁多不缺。
    """
    out: list[str] = []
    seen: set[str] = set()
    for step in steps or []:
        desc = str(step.get("description") or step.get("original_description") or "")
        for m in _RE_BRACKET_VALUES.finditer(desc):
            v = m.group(1).strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        for m in _RE_BRACKET_TARGETS.finditer(desc):
            v = m.group(1).strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def missing_step_literals(script: str, steps: list[dict[str, Any]] | None) -> list[str]:
    """脚本里缺失的必需字面值（空列表 = 通过）。"""
    text = script or ""
    return [lit for lit in extract_step_literals(steps) if lit not in text]


def parse_step_intent(description: str) -> dict[str, Any] | None:
    """把一步描述解析成"机械动作意图"（供确定性自动成组 / 编译先行共用）。

    Returns 形如 {"kind": ..., "target": ..., "value": ...}；无法机械解析时返回 None。
    """
    desc = (description or "").strip()
    if not desc:
        return None
    # 打开/导航：显式 URL 或"入口页"（登录/首页/主页）可机械解析为 goto；
    # 其余（如"打开商品列表页"这类需要已登录路径的）交给 LLM，不猜。
    m = _RE_OPEN.match(desc)
    if m:
        url_m = _RE_URL.search(desc)
        if url_m:
            return {"kind": "open", "url": url_m.group(0), "entry": True}
        rest = m.group("rest")
        if any(h in rest for h in ("登录", "首页", "主页", "入口", "首屏")):
            return {"kind": "open", "url": None, "entry": True}
        return None

    m = _RE_ASSERT_TITLE.search(desc)
    if m:
        return {"kind": "assert_title", "text": m.group("text")}
    m = _RE_ASSERT_CONTAINS.search(desc)
    if m:
        return {"kind": "assert_contains", "text": m.group("text")}
    if _RE_ASSERT_TEXT.search(desc):
        return None  # "…为【1】" 这类判断句机械表达不了

    m = _RE_FILL.search(desc)
    if m:
        return {"kind": "fill", "target": m.group("target"), "value": m.group("value")}
    m = _RE_SELECT.search(desc) or _RE_SELECT_ALT.search(desc)
    if m:
        return {"kind": "select", "target": m.group("target"), "value": m.group("choice")}
    m = _RE_UNCHECK.search(desc)
    if m:
        return {"kind": "uncheck", "target": m.group("target")}
    m = _RE_CHECK.search(desc)
    if m:
        return {"kind": "check", "target": m.group("target")}
    m = _RE_HOVER.search(desc)
    if m:
        return {"kind": "hover", "target": m.group("target")}
    m = _RE_SCROLL.search(desc)
    if m:
        return {"kind": "scroll", "target": m.group("target")}
    m = _RE_CLICK.search(desc)
    if m:
        return {"kind": "click", "target": m.group("target")}
    m = _RE_WAIT_TEXT.search(desc)
    if m:
        return {"kind": "wait", "text": m.group("text")}
    m = _RE_KEY.search(desc)
    if m:
        key_raw = m.group("key")
        if key_raw.lower().startswith("ctrl+"):
            key = "Control+" + key_raw.split("+", 1)[1].upper()
        else:
            key = _KEY_MAP.get(key_raw.lower(), key_raw)
        return {"kind": "press_key", "key": key}
    return None


# ── 断言可判定性（生成器校验器 与 运行时覆盖门 **同源**）────────────────────

_RE_EXPECT_BRACKET = re.compile(r"[【「\"']([^】」\"']{1,})[】」\"']")
_VAGUE_EXPECT = re.compile(r"^(可见|正常|正确|成功|完成|无误|可用|加载完成|存在)$")


def extract_expected_text(description: str, parsed_result: str | None = None) -> str:
    """从断言步骤描述中抽取期望文本：「断言页面包含【X】」→ X。

    与 ``AgentBridge._assertion_expectation`` 完全一致（同一口径）：
    ① 优先取【「" ' 包裹的原文（最可靠）；
    ② 否则剥掉"断言/页面包含"等前缀，取前 40 字；
    ③ 描述给不出时用预期结果字段。
    """
    text = str(description or "")
    m = _RE_EXPECT_BRACKET.search(text)
    if m:
        return m.group(1).strip()
    t = re.sub(r"^\s*(断言|验证|检查|确认)", "", text).strip()
    t = re.sub(r"^(页面|列表)?(包含|显示|出现|存在|变成|为|是)", "", t).strip()
    if t:
        return t[:40]
    fallback = str(parsed_result or "").strip()
    return fallback[:40]


def is_checkable_assertion(
    description: str,
    parsed_result: str | None = None,
    value: str | None = None,
) -> bool:
    """断言是否**机器可判定**：期望内容必须能在页面上查找。

    通过条件（满足其一）：
      ① 描述/预期结果里有【】包裹、且内容具体（非"可见/正常/完成"这类状态词）；
      ② 结构化步骤的 ``value`` 具体（非空、非状态词）——它就是期望查找内容。
    拒绝：「断言购物车图标可见」「断言商品列表正常显示」等没有可查找内容的断言
    （运行时覆盖门永远无法确认，会把用例拖死——515 的真实教训）。
    """
    text = str(description or "")
    m = _RE_EXPECT_BRACKET.search(text) or _RE_EXPECT_BRACKET.search(str(parsed_result or ""))
    if m:
        bracket_value = m.group(1).strip()
        if bracket_value and not _VAGUE_EXPECT.match(bracket_value):
            return True
    candidate = str(value if value is not None else "").strip()
    if candidate and not _VAGUE_EXPECT.match(candidate):
        return True
    return False
