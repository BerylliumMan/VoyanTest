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
            result['error'] = 'LLM tool call generation timed out'
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
            result['error'] = f"LLM could not determine action: {tool_call.value}"
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
            result['error'] = exec_result.get('error', 'Unknown error')
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
        elif expected_result:
            # Verify expected result against post-execution page state
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
                logger.warning("Step %s verification timed out", step_number)
            except Exception as exc:  # noqa: BLE001 - 断言失败只记录 warning，不中断步骤流程
                logger.warning("Step %s verification failed: %s", step_number, exc, exc_info=True)

    except Exception as exc:  # noqa: BLE001 - 步骤执行涉及 MCP / LLM / asyncio / DOM，需统一兜底
        result['error'] = str(exc)
        logger.warning("Step %s exception: %s", step_number, exc, exc_info=True)

    result['duration_ms'] = (time.monotonic() - t_start) * 1000
    return result