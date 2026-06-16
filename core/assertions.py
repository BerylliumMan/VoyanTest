# core/assertions.py
"""
步骤断言验证引擎。

提供 5 种断言类型，通过 Playwright MCP 的 browser_evaluate / browser_snapshot
工具验证浏览器状态，不引入 requests/httpx 等额外依赖。

断言类型：
- url_contains:  当前页面 URL 包含指定子串
- text_exists:   页面 DOM 快照中存在指定文本
- element_visible: 指定 CSS 选择器对应的元素可见
- input_value:   输入框 value 匹配预期（格式 "selector=expected"）
- element_count: 匹配选择器的元素数量（格式 "selector=count"）

同时提供自然语言预期结果的结构化解析。
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 断言类型 → 处理函数名映射
# ---------------------------------------------------------------------------
ASSERTION_TYPE_MAP: dict[str, str] = {
    'url_contains': '_assert_url_contains',
    'text_exists': '_assert_text_exists',
    'element_visible': '_assert_element_visible',
    'input_value': '_assert_input_value',
    'element_count': '_assert_element_count',
}


# ===================================================================
# 5 种断言处理函数（每个均返回统一结构体）
# ===================================================================

async def _assert_url_contains(mcp_manager: Any, value: str) -> dict[str, Any]:
    """断言当前页面 URL 包含 value 子串。

    通过 browser_evaluate 执行 window.location.href 获取当前 URL。
    """
    try:
        result = await mcp_manager.call_tool(
            "browser_evaluate",
            {"expression": "window.location.href"},
        )
        if not result.get('success', False):
            return {
                'passed': False,
                'type': 'url_contains',
                'expected': value,
                'actual': None,
                'error': result.get('text') or result.get('error', '获取 URL 失败'),
            }
        current_url = result['text'].strip()
        passed = value in current_url
        return {
            'passed': passed,
            'type': 'url_contains',
            'expected': value,
            'actual': current_url,
        }
    except Exception as exc:
        logger.warning("_assert_url_contains 异常: %s", exc)
        return {
            'passed': False,
            'type': 'url_contains',
            'expected': value,
            'actual': None,
            'error': str(exc),
        }


async def _assert_text_exists(mcp_manager: Any, value: str) -> dict[str, Any]:
    """断言页面 DOM 无障碍快照中存在指定文本。

    通过 browser_snapshot 获取页面文本快照后做子串匹配。
    """
    try:
        result = await mcp_manager.call_tool("browser_snapshot", {})
        if not result.get('success', False):
            return {
                'passed': False,
                'type': 'text_exists',
                'expected': value,
                'actual': None,
                'error': result.get('text') or result.get('error', '获取页面快照失败'),
            }
        snapshot_text = result['text']
        passed = value in snapshot_text
        actual = f"text {'found' if passed else 'not found'} in snapshot"
        return {
            'passed': passed,
            'type': 'text_exists',
            'expected': value,
            'actual': actual,
        }
    except Exception as exc:
        logger.warning("_assert_text_exists 异常: %s", exc)
        return {
            'passed': False,
            'type': 'text_exists',
            'expected': value,
            'actual': None,
            'error': str(exc),
        }


async def _assert_element_visible(mcp_manager: Any, value: str) -> dict[str, Any]:
    """断言指定 CSS 选择器对应的元素可见。

    通过 browser_evaluate 执行 JS 检测元素的存在性、display 和 visibility 状态。
    """
    selector = value.strip()
    js_code = (
        "(function() {{"
        "  const el = document.querySelector({sel_js});"
        "  if (!el) return 'not_found';"
        "  const style = window.getComputedStyle(el);"
        "  const visible = el.offsetParent !== null && "
        "    style.display !== 'none' && "
        "    style.visibility !== 'hidden';"
        "  return visible ? 'visible' : 'hidden';"
        "}})()"
    ).format(sel_js=_json.dumps(selector))

    try:
        result = await mcp_manager.call_tool(
            "browser_evaluate", {"expression": js_code}
        )
        if not result.get('success', False):
            return {
                'passed': False,
                'type': 'element_visible',
                'expected': f"'{selector}' visible",
                'actual': None,
                'error': result.get('text') or result.get('error', '检测元素可见性失败'),
            }
        state = result['text'].strip()
        passed = state == 'visible'
        return {
            'passed': passed,
            'type': 'element_visible',
            'expected': f"element '{selector}' visible",
            'actual': f"element '{selector}' {state}",
        }
    except Exception as exc:
        logger.warning("_assert_element_visible 异常: %s", exc)
        return {
            'passed': False,
            'type': 'element_visible',
            'expected': value,
            'actual': None,
            'error': str(exc),
        }


async def _assert_input_value(mcp_manager: Any, value: str) -> dict[str, Any]:
    """断言输入框的值与预期一致。

    value 格式为 "selector=expected_value"，使用最后一个 '=' 分割，
    以兼容属性选择器如 input[name=email]。
    """
    # ---- 先解析 ----
    try:
        parts = value.rsplit('=', 1)
        if len(parts) != 2:
            return {
                'passed': False,
                'type': 'input_value',
                'expected': value,
                'actual': None,
                'error': f"无效的 input_value 格式: '{value}'，期望 'selector=expected'",
            }
        selector, expected_val = parts[0].strip(), parts[1].strip()
    except Exception as exc:
        return {
            'passed': False,
            'type': 'input_value',
            'expected': value,
            'actual': None,
            'error': f"解析 input_value 失败: {exc}",
        }

    # ---- 通过 browser_evaluate 获取值 ----
    js_code = (
        "(function() {{"
        "  const el = document.querySelector({sel_js});"
        "  if (!el) return '__ELEMENT_NOT_FOUND__';"
        "  return el.value !== undefined ? el.value : (el.textContent || '');"
        "}})()"
    ).format(sel_js=_json.dumps(selector))

    try:
        result = await mcp_manager.call_tool(
            "browser_evaluate", {"expression": js_code}
        )
        if not result.get('success', False):
            return {
                'passed': False,
                'type': 'input_value',
                'expected': expected_val,
                'actual': None,
                'error': result.get('text') or result.get('error', '获取输入框值失败'),
            }
        actual_val = result['text'].strip()
        if actual_val == '__ELEMENT_NOT_FOUND__':
            return {
                'passed': False,
                'type': 'input_value',
                'expected': expected_val,
                'actual': None,
                'error': f"未找到元素: '{selector}'",
            }
        passed = (actual_val == expected_val)
        return {
            'passed': passed,
            'type': 'input_value',
            'expected': expected_val,
            'actual': actual_val,
        }
    except Exception as exc:
        logger.warning("_assert_input_value 异常: %s", exc)
        return {
            'passed': False,
            'type': 'input_value',
            'expected': expected_val,
            'actual': None,
            'error': str(exc),
        }


async def _assert_element_count(mcp_manager: Any, value: str) -> dict[str, Any]:
    """断言匹配选择器的元素数量与预期相等。

    value 格式为 "selector=count"，使用最后一个 '=' 分割。
    """
    # ---- 先解析 ----
    parts = value.rsplit('=', 1)
    if len(parts) != 2:
        return {
            'passed': False,
            'type': 'element_count',
            'expected': value,
            'actual': None,
            'error': f"无效的 element_count 格式: '{value}'，期望 'selector=count'",
        }
    try:
        selector, count_str = parts[0].strip(), parts[1].strip()
        expected_count = int(count_str)
    except ValueError:
        return {
            'passed': False,
            'type': 'element_count',
            'expected': value,
            'actual': None,
            'error': f"无效的计数值: '{parts[1].strip()}'",
        }
    except Exception as exc:
        return {
            'passed': False,
            'type': 'element_count',
            'expected': value,
            'actual': None,
            'error': f"解析 element_count 失败: {exc}",
        }

    # ---- 通过 browser_evaluate 计数 ----
    js_code = "document.querySelectorAll({sel_js}).length".format(
        sel_js=_json.dumps(selector)
    )
    try:
        result = await mcp_manager.call_tool(
            "browser_evaluate", {"expression": js_code}
        )
        if not result.get('success', False):
            return {
                'passed': False,
                'type': 'element_count',
                'expected': str(expected_count),
                'actual': None,
                'error': result.get('text') or result.get('error', '获取元素数量失败'),
            }
        actual_count = int(result['text'].strip())
        passed = (actual_count == expected_count)
        return {
            'passed': passed,
            'type': 'element_count',
            'expected': str(expected_count),
            'actual': str(actual_count),
        }
    except Exception as exc:
        logger.warning("_assert_element_count 异常: %s", exc)
        return {
            'passed': False,
            'type': 'element_count',
            'expected': str(expected_count),
            'actual': None,
            'error': str(exc),
        }


# ===================================================================
# 断言执行引擎
# ===================================================================

async def execute_assertions(
    mcp_manager: Any,
    assertions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """执行一组断言，返回统一结构的结果列表。

    参数
    ----
    mcp_manager : 具有 async call_tool(name, args) 方法的 MCP 管理器
    assertions : 断言字典列表，如 [{"type":"url_contains","value":"/dashboard"}]

    返回
    ----
    list[dict]  - 每个元素包含 passed / type / expected / actual / error 等字段
    """
    results: list[dict[str, Any]] = []

    for idx, assertion in enumerate(assertions):
        assert_type = assertion.get('type', '')
        value = assertion.get('value', '')

        # 未知类型 → 直接失败
        handler_name = ASSERTION_TYPE_MAP.get(assert_type)
        if handler_name is None:
            results.append({
                'passed': False,
                'error': f"Unknown assertion type: {assert_type}",
            })
            logger.warning("断言 #%d 类型未知: %s", idx, assert_type)
            continue

        # 查找并调用处理函数
        handler = globals().get(handler_name)
        if handler is None:
            results.append({
                'passed': False,
                'error': f"Handler not found for type: {assert_type}",
            })
            logger.error("断言 #%d 处理函数缺失: %s", idx, handler_name)
            continue

        try:
            result = await handler(mcp_manager, value)
            results.append(result)
        except Exception as exc:
            logger.exception("断言 #%d (%s) 执行异常", idx, assert_type)
            results.append({
                'passed': False,
                'error': str(exc),
            })

    return results


# ===================================================================
# 自然语言预期结果解析
# ===================================================================

def parse_expected_result(expected_text: str) -> list[dict[str, str]]:
    """将自然语言预期结果解析为结构化断言列表。

    支持中文/英文关键词，按中英文标点（逗号、分号、换行）分割多条条件。
    未匹配任何模式时，整段文本作为 text_exists 断言回退。

    示例
    ----
    >>> parse_expected_result("页面URL包含 /dashboard，页面包含'欢迎'")
    [{'type': 'url_contains', 'value': '/dashboard'},
     {'type': 'text_exists', 'value': '欢迎'}]
    """
    if not expected_text or not expected_text.strip():
        return []

    # 按中英文标点拆分多个条件
    parts = re.split(r'[，,；;。\n]+', expected_text)
    parts = [p.strip() for p in parts if p.strip()]

    if not parts:
        parts = [expected_text.strip()]

    assertions: list[dict[str, str]] = []
    for part in parts:
        parsed = _parse_single_assertion(part)
        if parsed is not None:
            assertions.append(parsed)

    # 无匹配时整段作为 text_exists 回退
    if not assertions:
        assertions.append({
            'type': 'text_exists',
            'value': expected_text.strip(),
        })

    return assertions


def _parse_single_assertion(text: str) -> dict[str, str] | None:
    """解析单条预期条件为断言字典。

    按优先级依次尝试 6 种模式，首次匹配即返回。
    """
    text_lower = text.lower()

    # ---- 1) URL 包含 / 跳转 ----
    # "页面URL包含 /dashboard" / "跳转到 /login"
    url_m = re.search(
        r'(?:URL|url|跳转).*?包含\s*[：:]?\s*',
        text,
    )
    if url_m:
        after = text[url_m.end():].strip().strip('\'"')
        if after:
            return {'type': 'url_contains', 'value': after}

    # 更宽松: "URL /dashboard" / "跳转 /dashboard"
    url_simple = re.search(r'(?:URL|url|跳转)\s*[：:]\s*(\S+)', text_lower)
    if url_simple:
        return {'type': 'url_contains', 'value': url_simple.group(1).strip().strip('\'"')}

    # ---- 2) 元素数量 / 个数 / 计数 ----
    # "#items 数量 5" / ".card 个数为 3"
    count_m = re.search(
        r'([#.]\S+)'
        + r'\s*(?:数量|个数|计数)'
        + r'\s*[为是]?\s*[：:=]?\s*'
        + r'(\d+)',
        text,
    )
    if count_m:
        return {
            'type': 'element_count',
            'value': f"{count_m.group(1).strip()}={count_m.group(2)}",
        }

    # ---- 3) 可见元素（带 # 或 .） ----
    # "#submit 可见" / "可见 #submit" / "元素 #submit 存在"
    el_vis = re.search(
        r'(?:([#.]\S+)\s*(?:可见|存在))'
        + r'|'
        + r'(?:(?:可见|存在)\s*([#.]\S+))',
        text,
    )
    if el_vis:
        sel = (el_vis.group(1) or el_vis.group(2)).strip()
        return {'type': 'element_visible', 'value': sel}

    # ---- 4) 输入框值 ----
    # "#email 的值是 test@test.com" / "输入框 #name 内容为 张三"
    input_m = re.search(
        r'([#.]\S+|input\[[^\]]+\])'
        + r'\s*(?:的)?'
        + r'\s*(?:值|内容|value)'
        + r'\s*[为是：:=]+\s*'
        + r'(.+?)$',
        text,
        re.IGNORECASE,
    )
    if input_m:
        sel = input_m.group(1).strip()
        exp_val = input_m.group(2).strip().strip('\'"')
        if exp_val:
            return {'type': 'input_value', 'value': f"{sel}={exp_val}"}

    # ---- 5) 文本存在 / 包含 ----
    # "包含文字'欢迎'" / "页面存在'成功'" / "显示文本'提交成功'"
    text_m = re.search(
        r'(?:存在|包含|显示)'
        + r'\s*(?:文字|文本)?'
        + r'\s*[：:]?\s*'
        + r'[\'""](.+?)[\'""]',
        text,
    )
    if text_m:
        return {'type': 'text_exists', 'value': text_m.group(1).strip()}

    # 不含引号的宽松匹配: "包含 欢迎光临" / "显示 操作成功"
    text_loose = re.search(
        r'(?:存在|包含|显示)'
        + r'\s*(?:文字|文本)?'
        + r'\s*[：:]?\s*'
        + r'([^，,；;。\n#.]+)',
        text,
    )
    if text_loose:
        val = text_loose.group(1).strip()
        # 避免把选择器误判为文本
        if val and not val.startswith('#') and not val.startswith('.'):
            return {'type': 'text_exists', 'value': val}

    # ---- 6) 启发式回退 ----
    # 整个文本像 URL 路径 → url_contains
    if text.startswith('/') or text_lower.startswith('http'):
        return {'type': 'url_contains', 'value': text}

    # 整个文本像 CSS 选择器 → element_visible
    if _looks_like_selector(text):
        return {'type': 'element_visible', 'value': text.strip()}

    return None


def _looks_like_selector(text: str) -> bool:
    """判断一段文本是否看起来像 CSS 选择器。"""
    stripped = text.strip()
    if not stripped:
        return False
    # 以 # 或 . 开头显然是选择器
    if stripped.startswith('#') or stripped.startswith('.'):
        return True
    # 含属性选择器方括号
    if '[' in stripped and ']' in stripped:
        return True
    return False


# ===================================================================
# 快速验证（直接运行此文件）
# ===================================================================

if __name__ == '__main__':
    async def _test():
        """使用 AsyncMock 对 5 种断言做快速冒烟验证。"""
        from unittest.mock import AsyncMock

        print("=" * 60)
        print("  assertions.py 快速验证")
        print("=" * 60)

        mcp = AsyncMock()

        # --- 1. url_contains (成功) ---
        mcp.call_tool.return_value = {
            "success": True,
            "text": "https://example.com/dashboard",
        }
        r1 = await _assert_url_contains(mcp, "/dashboard")
        print(f"\n[1] _assert_url_contains (passed=True):  {r1['passed']}")

        # --- 2. text_exists (成功) ---
        mcp.call_tool.return_value = {
            "success": True,
            "text": "欢迎回来\n用户中心\n退出登录",
        }
        r2 = await _assert_text_exists(mcp, "用户中心")
        print(f"[2] _assert_text_exists (passed=True):    {r2['passed']}")

        # --- 3. element_visible (成功) ---
        mcp.call_tool.return_value = {
            "success": True,
            "text": "visible",
        }
        r3 = await _assert_element_visible(mcp, "#submit-btn")
        print(f"[3] _assert_element_visible (passed=True): {r3['passed']}")

        # --- 4. input_value (成功) ---
        mcp.call_tool.return_value = {
            "success": True,
            "text": "test@example.com",
        }
        r4 = await _assert_input_value(mcp, "#email=test@example.com")
        print(f"[4] _assert_input_value (passed=True):    {r4['passed']}")

        # --- 5. element_count (成功) ---
        mcp.call_tool.return_value = {
            "success": True,
            "text": "5",
        }
        r5 = await _assert_element_count(mcp, ".card=5")
        print(f"[5] _assert_element_count (passed=True):  {r5['passed']}")

        # --- 6. execute_assertions 批量路由 ---
        assertions = [
            {"type": "url_contains", "value": "/dashboard"},
            {"type": "text_exists", "value": "hello"},
            {"type": "unknown_type", "value": "xxx"},
        ]
        # 为 url_contains 和 text_exists 设置不同的 mock side_effect
        call_results = [
            {"success": True, "text": "https://example.com/dashboard"},  # url_contains
            {"success": True, "text": "hello world"},                     # text_exists
        ]
        call_index = 0

        async def _side_effect(_tool_name: object, _args: object) -> dict[str, Any]:
            nonlocal call_index
            if call_index < len(call_results):
                res = call_results[call_index]
                call_index += 1
            else:
                res = {"success": True, "text": ""}
            return res

        mcp.call_tool = AsyncMock(side_effect=_side_effect)
        results = await execute_assertions(mcp, assertions)
        passed_count = sum(1 for r in results if r.get('passed'))
        msg6 = (
            f"\n[6] execute_assertions: {len(results)} 个断言, "
            + f"passed={passed_count}, failed={len(results) - passed_count}"
        )
        print(msg6)
        for i, r in enumerate(results):
            msg_item = (
                f"    #{i} type={r.get('type', '?')} "
                + f"passed={r.get('passed')} error={r.get('error', '-')}"
            )
            print(msg_item)

        # --- 7. parse_expected_result 自然语言解析 ---
        print("\n[7] parse_expected_result 自然语言解析:")
        cases = [
            ("页面URL包含 /dashboard，页面包含'欢迎'",
             [{'type': 'url_contains', 'value': '/dashboard'},
              {'type': 'text_exists', 'value': '欢迎'}]),
            ("#items 数量 5", [{'type': 'element_count', 'value': '#items=5'}]),
            ("#email 的值是 test@test.com", [{'type': 'input_value', 'value': '#email=test@test.com'}]),
            ("#submit 可见", [{'type': 'element_visible', 'value': '#submit'}]),
            ("直接给一段没有关键字的描述", [{'type': 'text_exists', 'value': '直接给一段没有关键字的描述'}]),
        ]
        for raw, expected in cases:
            parsed = parse_expected_result(raw)
            match = "✓" if parsed == expected else f"✗ (got {parsed})"
            print(f"    {match}  '{raw[:40]}...'")

        print("\n" + "=" * 60)
        print("  全部验证完成")
        print("=" * 60)

    asyncio.run(_test())
