# core/step_executor.py
"""
Step execution logic extracted from runner.py.

Contains:
  - _URL_CHARS constant
  - _sanitize_step() — insert spaces between URLs and adjacent CJK characters
  - _capture_screenshot() — take a screenshot on failure via MCP
  - execute_step_mcp() — execute a single NL test step via Playwright MCP
"""

import asyncio
import logging
import os
import re
import time
from typing import Any

from core.verification_strategy import VERIFICATION_STRATEGY as strategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL / step sanitising
# ---------------------------------------------------------------------------

_URL_CHARS = r'a-zA-Z0-9._~:/?#\[\]@!$&\'()*+,;%=<>-'


def _sanitize_step(desc: str) -> str:
    """Insert space between URL and adjacent Chinese characters."""
    desc = re.sub(r'(https?://[' + _URL_CHARS + r']+)([一-鿿])', r'\1 \2', desc)
    desc = re.sub(r'([一-鿿])(https?://)', r'\1 \2', desc)
    return desc


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------


async def _capture_screenshot(
    mcp_manager, screenshot_dir: str | None, step_number: int, result: dict,
) -> None:
    """Take a screenshot on failure and store path in result."""
    if not screenshot_dir or not mcp_manager:
        return
    try:
        os.makedirs(screenshot_dir, exist_ok=True)
        ss_path = os.path.join(screenshot_dir, f"step_{step_number}.png")
        saved = await mcp_manager.take_screenshot(ss_path)
        if saved:
            result['screenshot_path'] = saved
    except (OSError, RuntimeError) as exc:
        # OSError: 写文件失败；RuntimeError: MCP / Playwright 截图调用失败
        logger.warning("Failed to capture screenshot: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Tiered verification helpers (Level 0 & Level 1)
# ---------------------------------------------------------------------------


async def _level0_verify(mcp_manager, tool_call) -> bool:
    """Level 0: cheap deterministic checks before involving LLM.

    goto → compare browser URL against tool_call.value
    fill → check if any input/textarea/select holds the expected value
    """
    action = tool_call.action
    try:
        if action == 'goto':
            url = tool_call.value or ''
            if not url:
                return False
            result = await mcp_manager.call_tool('browser_evaluate', {
                'function': 'window.location.href',
            })
            if result.get('success'):
                current = result.get('text', '')
                return url.rstrip('/') in current or current.rstrip('/') in url
        elif action == 'fill':
            value = tool_call.value or ''
            if not value:
                return False
            escaped = value.replace('\\', '\\\\').replace("'", "\\'")
            result = await mcp_manager.call_tool('browser_evaluate', {
                'function': (
                    f"Array.from(document.querySelectorAll('input,textarea,select'))"
                    f".some(el => el.value === '{escaped}')"
                ),
            })
            if result.get('success'):
                return result.get('text', '').strip().lower() == 'true'
    except Exception as exc:
        logger.debug("Level 0 验证异常 (非致命): %s", exc)
    return False


def _condition_to_js(condition) -> str:
    """Translate a VerificationCondition into a browser_evaluate JS expression."""
    check = condition.check
    target = condition.target.replace('\\', '\\\\').replace("'", "\\'")
    if check == 'text_contains':
        return f"document.body.innerText.includes('{target}')"
    if check == 'text_matches':
        return f"new RegExp('{target}').test(document.body.innerText)"
    if check == 'url_contains':
        return f"window.location.href.includes('{target}')"
    if check == 'url_matches':
        return f"new RegExp('{target}').test(window.location.href)"
    if check == 'element_visible':
        return (
            f"(function(){{var el=document.querySelector('{target}');"
            f"return el!==null&&window.getComputedStyle(el).display!=='none'}})()"
        )
    if check == 'element_count':
        parts = target.split('|', 1)
        sel = parts[0].replace("'", "\\'")
        expected = parts[1] if len(parts) > 1 else '1'
        if expected.startswith('>='):
            n = int(expected[2:])
            return f"document.querySelectorAll('{sel}').length>={n}"
        if expected.startswith('<='):
            n = int(expected[2:])
            return f"document.querySelectorAll('{sel}').length<={n}"
        if expected.startswith('>'):
            n = int(expected[1:])
            return f"document.querySelectorAll('{sel}').length>{n}"
        if expected.startswith('<'):
            n = int(expected[1:])
            return f"document.querySelectorAll('{sel}').length<{n}"
        return f"document.querySelectorAll('{sel}').length==={expected}"
    if check == 'page_title':
        return f"document.title.includes('{target}')"
    if check == 'js_expression':
        return target
    return 'false'


async def _level1_verify(mcp_manager, conditions: list) -> bool:
    """Level 1: evaluate each VerificationCondition via browser_evaluate."""
    if not conditions:
        return False
    for condition in conditions:
        try:
            js = _condition_to_js(condition)
            result = await mcp_manager.call_tool('browser_evaluate', {'function': js})
            if not result.get('success'):
                return False
            text = result.get('text', '').strip().lower()
            if text in ('false', 'null', 'undefined', ''):
                return False
        except Exception as exc:
            logger.debug("Level 1 条件评估失败: %s", exc)
            return False
    return True


# ---------------------------------------------------------------------------
# Step execution (MCP-based)
# ---------------------------------------------------------------------------


async def execute_step_mcp(
    step: dict,
    mcp_manager,
    llm_client,
    *,
    model: str | None = None,
    step_timeout_ms: int = 120000,
    screenshot_dir: str | None = None,
) -> dict:
    """Execute a single NL test step via Playwright MCP.

    1. Take accessibility snapshot (browser_snapshot)
    2. LLM generates tool call from step description + snapshot + expected result
    3. Execute tool call via MCP
    4. Return structured result
    """
    step_number = step['step_order']
    desc = _sanitize_step(step['description'])
    expected_result = step.get('expected_result')
    t_start = time.monotonic()

    result: dict[str, Any] = {
        'step_number': step_number,
        'original_description': desc,
        'success': False,
        'thinking': '',
        'action': '',
        'next_goal': '',
        'error': None,
        'screenshot_path': None,
        'duration_ms': 0,
    }

    try:
        # 1. Get accessibility snapshot via MCP
        snapshot = await mcp_manager.get_dom_snapshot()

        # 2. LLM generates tool call
        from core.llm_wrapper import generate_tool_call

        try:
            tool_call = await asyncio.wait_for(
                generate_tool_call(desc, snapshot, expected_result=expected_result, client=llm_client, model=model),
                timeout=100,
            )
        except asyncio.TimeoutError:
            tool_call = None

        if tool_call is None:
            result['error'] = 'LLM 生成操作指令超时'
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
            return result

        result['thinking'] = tool_call.thinking or f"Execute: {tool_call.action}"
        result['action'] = (
            f"{tool_call.action}"
            + (f"({tool_call.selector})" if tool_call.selector else "")
            + (f" = {tool_call.value}" if tool_call.value else "")
        )
        result['next_goal'] = tool_call.next_goal or ''

        if tool_call.action == 'error':
            result['error'] = f"LLM 无法确定操作: {tool_call.value}"
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
            return result

        # 3. Execute via MCP
        try:
            exec_result = await asyncio.wait_for(
                mcp_manager.execute_tool_call(tool_call.model_dump()),
                timeout=step_timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            exec_result = {
                'success': False,
                'error': f'Step execution timeout after {step_timeout_ms}ms',
            }

        result['success'] = exec_result['success']
        if not exec_result['success']:
            result['error'] = exec_result.get('error', '未知错误')
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
        elif expected_result:
            # 3层级联验证：Level 0 确定性检查 → Level 1 结构化条件 → Level 2 LLM 比对
            mcp_error = exec_result.get('error')
            action_type = tool_call.action

            if strategy.should_verify(action_type, mcp_error):
                verified = False

                # Level 0: 确定性廉价检查（goto URL 比对 / fill 值比对）
                try:
                    level0_pass = await _level0_verify(mcp_manager, tool_call)
                    if level0_pass:
                        verified = True
                        result['verification'] = 'Level 0: deterministic check passed'
                except Exception as exc:
                    logger.debug("Level 0 verification error (non-fatal): %s", exc)

                # Level 1: LLM 生成结构化验证条件 → 浏览器确定性评估
                if not verified:
                    try:
                        from core.llm_wrapper import generate_verification_conditions
                        conditions = await asyncio.wait_for(
                            generate_verification_conditions(expected_result, client=llm_client, model=model),
                            timeout=15,
                        )
                        if conditions:
                            level1_pass = await _level1_verify(mcp_manager, conditions)
                            if level1_pass:
                                verified = True
                                result['verification'] = 'Level 1: structured conditions passed'
                    except asyncio.TimeoutError:
                        logger.debug("Level 1 验证条件生成超时")
                    except Exception as exc:
                        logger.debug("Level 1 验证跳过: %s", exc)

                # Level 2: 完整 LLM 比对 —— 仅当 Level 0+1 均未通过时回退
                if not verified:
                    try:
                        post_snapshot = await mcp_manager.get_dom_snapshot()
                        from core.llm_wrapper import verify_expected_result
                        verification = await asyncio.wait_for(
                            verify_expected_result(expected_result, post_snapshot, step_description=desc, client=llm_client, model=model),
                            timeout=30,
                        )
                        if not verification.passed:
                            result['success'] = False
                            result['error'] = f"预期结果验证失败: {verification.reason}"
                            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
                        else:
                            result['verification'] = verification.reason
                    except asyncio.TimeoutError:
                        logger.warning("步骤 %s 预期结果验证超时", step_number)
                    except Exception as exc:
                        logger.warning("步骤 %s 预期结果验证异常: %s", step_number, exc, exc_info=True)
            # else: should_verify 返回 False —— 信任 MCP 执行结果，跳过验证

    except Exception as exc:
        result['error'] = f'步骤执行异常: {exc}'
        logger.warning("步骤 %s 异常: %s", step_number, exc, exc_info=True)

    result['duration_ms'] = (time.monotonic() - t_start) * 1000
    return result