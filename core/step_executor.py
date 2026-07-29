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
from pathlib import Path
import re
import time
from typing import Any

from core.verification_strategy import VERIFICATION_STRATEGY as strategy

logger = logging.getLogger(__name__)

# Hybrid mode C: settle UI (toast/dropdown) before snapshot re-observe
HYBRID_SETTLE_SECONDS = 0.8

# ---------------------------------------------------------------------------
# URL / step sanitising
# ---------------------------------------------------------------------------

_URL_CHARS = r'a-zA-Z0-9._~:/?#\[\]@!$&\'()*+,;%=<>-'


def _sanitize_step(desc: str) -> str:
    """Insert space between URL and adjacent Chinese characters."""
    desc = re.sub(r'(https?://[' + _URL_CHARS + r']+)([一-鿿])', r'\1 \2', desc)
    desc = re.sub(r'([一-鿿])(https?://)', r'\1 \2', desc)
    return desc


# 「单位下拉框选择【汉东省院】」/「在【单位】下拉中选择【汉东省院】」
_DROPDOWN_SELECT_RE = re.compile(
    r"(?:"
    r"(?:在)?【(?P<label1>[^】]{1,40})】(?:的)?(?:下拉框|下拉菜单|下拉|选择器)\s*(?:中)?\s*(?:选择|选)\s*【(?P<option1>[^】]+)】"
    r"|"
    r"(?P<label2>[^【\n]{1,40}?)(?:下拉框|下拉菜单|下拉)\s*(?:中)?\s*(?:选择|选)\s*【(?P<option2>[^】]+)】"
    r"|"
    r"(?:选择|选中)\s*【(?P<option3>[^】]+)】"
    r")"
)


def parse_dropdown_select(desc: str) -> tuple[str | None, str | None]:
    """Extract (field_label, option_text) from a dropdown-select NL step."""
    m = _DROPDOWN_SELECT_RE.search((desc or "").strip())
    if not m:
        return None, None
    label = (m.groupdict().get("label1") or m.groupdict().get("label2") or "").strip()
    option = (
        m.groupdict().get("option1")
        or m.groupdict().get("option2")
        or m.groupdict().get("option3")
        or ""
    ).strip()
    if not option:
        return None, None
    # Strip trailing control-type words from label
    label = re.sub(r"(?:下拉框|下拉菜单|下拉|选择器)$", "", label).strip(" ：:，,")
    return (label or None), option


def normalize_step_description(desc: str) -> str:
    """Rewrite ambiguous control phrasing so LLM does not getByText('…下拉框')."""
    desc = _sanitize_step(desc or "")
    label, option = parse_dropdown_select(desc)
    if option and label:
        return (
            f"在标签或字段名为「{label}」的下拉框/组合框中选择选项「{option}」。"
            f"不要查找页面文案「下拉框」或「{label}下拉框」；"
            f"应匹配字段「{label}」或选项「{option}」。"
        )
    if option and not label:
        return (
            f"在当前已展开的下拉列表中点击选项「{option}」。"
            f"不要查找文案「下拉框」。"
        )
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
            # 报告/前端使用 /reports/... 相对路径
            result['screenshot_path'] = Path(saved).as_posix()
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


async def _llm_tool_and_run(
    *,
    desc: str,
    snapshot: str,
    expected_result: str | None,
    mcp_manager,
    llm_client,
    model: str | None,
    system_prompt_override: str | None,
    step_timeout_ms: int,
):
    """Generate one tool call from NL+snapshot and execute it via MCP."""
    from core.llm_wrapper import generate_tool_call

    try:
        tool_call = await asyncio.wait_for(
            generate_tool_call(
                desc,
                snapshot,
                expected_result=expected_result,
                client=llm_client,
                model=model,
                system_prompt=system_prompt_override,
            ),
            timeout=100,
        )
    except asyncio.TimeoutError:
        return None, {'success': False, 'error': 'LLM 生成操作指令超时'}

    if tool_call is None:
        return None, {'success': False, 'error': 'LLM 生成操作指令超时'}

    if tool_call.action == 'error':
        return tool_call, {
            'success': False,
            'error': f"LLM 无法确定操作: {tool_call.value}",
        }

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
    return tool_call, exec_result


async def _llm_tool_and_run_with_relocate(
    *,
    desc: str,
    snapshot: str,
    expected_result: str | None,
    mcp_manager,
    llm_client,
    model: str | None,
    system_prompt_override: str | None,
    step_timeout_ms: int,
):
    """Run `_llm_tool_and_run`; on failure, settle + refresh snapshot + retry once.

    Triggers when LLM returns action=error or MCP execution fails.
    Returns (tool_call, exec_result, relocate_attempted).
    """
    tool_call, exec_result = await _llm_tool_and_run(
        desc=desc,
        snapshot=snapshot,
        expected_result=expected_result,
        mcp_manager=mcp_manager,
        llm_client=llm_client,
        model=model,
        system_prompt_override=system_prompt_override,
        step_timeout_ms=step_timeout_ms,
    )
    if exec_result.get('success'):
        return tool_call, exec_result, False

    logger.info(
        "Hybrid relocate: first attempt failed (%s); refreshing snapshot",
        (exec_result.get('error') or '')[:120],
    )
    await asyncio.sleep(HYBRID_SETTLE_SECONDS)
    try:
        snapshot = await mcp_manager.get_dom_snapshot()
    except Exception as exc:
        logger.warning("Hybrid relocate: snapshot refresh failed: %s", exc, exc_info=True)
        return tool_call, exec_result, True

    tool_call2, exec_result2 = await _llm_tool_and_run(
        desc=desc,
        snapshot=snapshot,
        expected_result=expected_result,
        mcp_manager=mcp_manager,
        llm_client=llm_client,
        model=model,
        system_prompt_override=system_prompt_override,
        step_timeout_ms=step_timeout_ms,
    )
    return tool_call2, exec_result2, True


def _format_action(tool_call) -> str:
    if tool_call is None:
        return ''
    return (
        f"{tool_call.action}"
        + (f"({tool_call.selector})" if tool_call.selector else "")
        + (f" = {tool_call.value}" if tool_call.value else "")
    )


async def execute_step_mcp(
    step: dict,
    mcp_manager,
    llm_client,
    *,
    model: str | None = None,
    step_timeout_ms: int = 120000,
    screenshot_dir: str | None = None,
    system_prompt_override: str | None = None,
) -> dict:
    """Execute a single NL test step via Playwright MCP.

    Dropdown steps like「单位下拉框选择【汉东省院】」are normalized and may run as
    open-then-select (two MCP actions) when the option is not yet visible.

    When ``step['learned_locator']`` is set and locator memory is enabled, try
    direct MCP replay (skip LLM). Expected-result failure invalidates cache.
    """
    from core.locator_memory import (
        bump_hit_count,
        extract_from_snapshot,
        is_learnable_action,
        try_replay_mcp,
    )

    step_number = step['step_order']
    raw_desc = _sanitize_step(step.get('description') or '')
    desc = normalize_step_description(raw_desc)
    expected_result = step.get('expected_result')
    label, option = parse_dropdown_select(raw_desc)
    t_start = time.monotonic()

    result: dict[str, Any] = {
        'step_number': step_number,
        'original_description': raw_desc,
        'success': False,
        'thinking': '',
        'action': '',
        'next_goal': '',
        'error': None,
        'screenshot_path': None,
        'duration_ms': 0,
        'locator_replay': False,
        'learned_locator': None,
        'invalidate_learned_locator': False,
    }

    memory_enabled = True
    try:
        from app.runtime_config import healing_config as _hc
        memory_enabled = bool(getattr(_hc, "locator_memory_enabled", True))
    except Exception:
        memory_enabled = True

    cached_fp = step.get("learned_locator") if memory_enabled else None
    if not isinstance(cached_fp, dict):
        cached_fp = None

    try:
        snapshot = await mcp_manager.get_dom_snapshot()
        actions_log: list[str] = []
        thinking_parts: list[str] = []
        tool_call = None
        exec_result: dict[str, Any] = {"success": False}
        relocate_attempted = False
        used_replay = False

        # Fast path: replay learned locator (skip LLM)
        if cached_fp and not (label and option):
            replay = await try_replay_mcp(
                mcp_manager,
                cached_fp,
                snapshot=snapshot,
                step_description=raw_desc,
            )
            if replay.get("success") and not replay.get("skipped"):
                used_replay = True
                result["locator_replay"] = True
                tc = replay.get("tool_call") or {}

                class _ReplayTC:
                    pass

                tool_call = _ReplayTC()
                tool_call.action = tc.get("action")
                tool_call.selector = tc.get("selector")
                tool_call.value = tc.get("value")
                tool_call.thinking = tc.get("thinking") or ""
                tool_call.next_goal = ""
                exec_result = replay.get("exec_result") or {"success": True}
                actions_log.append(_format_action(tool_call))
                thinking_parts.append(tool_call.thinking)
            elif not replay.get("skipped"):
                result["invalidate_learned_locator"] = True
                logger.info(
                    "locator_memory replay MCP fail step=%s: %s",
                    step_number, (replay.get("error") or "")[:120],
                )

        if not used_replay:
            if label and option and option not in (snapshot or ""):
                open_desc = (
                    f"点击字段或标签为「{label}」的下拉框/组合框以展开选项列表。"
                    f"不要查找文案「下拉框」或「{label}下拉框」。"
                )
                tool_call, exec_result, open_relocated = await _llm_tool_and_run_with_relocate(
                    desc=open_desc,
                    snapshot=snapshot,
                    expected_result=None,
                    mcp_manager=mcp_manager,
                    llm_client=llm_client,
                    model=model,
                    system_prompt_override=system_prompt_override,
                    step_timeout_ms=step_timeout_ms,
                )
                relocate_attempted = relocate_attempted or open_relocated
                if tool_call is not None:
                    actions_log.append(_format_action(tool_call))
                    if tool_call.thinking:
                        thinking_parts.append(tool_call.thinking)
                if not exec_result.get('success'):
                    result['thinking'] = ' | '.join(thinking_parts) or '展开下拉失败'
                    result['action'] = ' → '.join(actions_log)
                    result['error'] = exec_result.get('error', '展开下拉失败')
                    result['relocate_attempted'] = relocate_attempted
                    await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
                    result['duration_ms'] = (time.monotonic() - t_start) * 1000
                    return result
                snapshot = await mcp_manager.get_dom_snapshot()
                pick_desc = (
                    f"在已展开的下拉列表中点击选项「{option}」。"
                    f"不要查找文案「下拉框」。"
                )
                tool_call, exec_result, pick_relocated = await _llm_tool_and_run_with_relocate(
                    desc=pick_desc,
                    snapshot=snapshot,
                    expected_result=expected_result,
                    mcp_manager=mcp_manager,
                    llm_client=llm_client,
                    model=model,
                    system_prompt_override=system_prompt_override,
                    step_timeout_ms=step_timeout_ms,
                )
                relocate_attempted = relocate_attempted or pick_relocated
            else:
                tool_call, exec_result, relocate_attempted = await _llm_tool_and_run_with_relocate(
                    desc=desc,
                    snapshot=snapshot,
                    expected_result=expected_result,
                    mcp_manager=mcp_manager,
                    llm_client=llm_client,
                    model=model,
                    system_prompt_override=system_prompt_override,
                    step_timeout_ms=step_timeout_ms,
                )

        if relocate_attempted:
            result['relocate_attempted'] = True

        if tool_call is not None and not used_replay:
            actions_log.append(_format_action(tool_call))
            if getattr(tool_call, "thinking", None):
                thinking_parts.append(tool_call.thinking)
            result['next_goal'] = getattr(tool_call, "next_goal", None) or ''

        result['thinking'] = ' | '.join(thinking_parts) or (
            f"Execute: {getattr(tool_call, 'action', '')}" if tool_call else ''
        )
        result['action'] = ' → '.join(a for a in actions_log if a)

        if tool_call is None:
            result['error'] = exec_result.get('error') or 'LLM 生成操作指令超时'
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
            result['duration_ms'] = (time.monotonic() - t_start) * 1000
            return result

        if getattr(tool_call, "action", None) == 'error':
            result['error'] = exec_result.get('error') or (
                f"LLM 无法确定操作: {getattr(tool_call, 'value', None)}"
            )
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
            result['duration_ms'] = (time.monotonic() - t_start) * 1000
            return result

        result['success'] = bool(exec_result.get('success'))
        if not result['success']:
            result['error'] = exec_result.get('error', '未知错误')
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)
            if used_replay:
                result["invalidate_learned_locator"] = True
        elif expected_result:
            mcp_error = exec_result.get('error')
            action_type = getattr(tool_call, "action", None)

            if strategy.should_verify(action_type, mcp_error):
                verified = False

                try:
                    level0_pass = await _level0_verify(mcp_manager, tool_call)
                    if level0_pass:
                        verified = True
                        result['verification'] = 'Level 0: deterministic check passed'
                except Exception as exc:
                    logger.debug("Level 0 verification error (non-fatal): %s", exc)

                if not verified:
                    try:
                        from core.llm_wrapper import generate_verification_conditions
                        conditions = await asyncio.wait_for(
                            generate_verification_conditions(
                                expected_result, client=llm_client, model=model,
                            ),
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

                if not verified:
                    try:
                        from core.llm_wrapper import verify_expected_result
                        post_snapshot = await mcp_manager.get_dom_snapshot()
                        verification = await asyncio.wait_for(
                            verify_expected_result(
                                expected_result,
                                post_snapshot,
                                step_description=raw_desc,
                                client=llm_client,
                                model=model,
                            ),
                            timeout=30,
                        )
                        if not verification.passed:
                            logger.info(
                                "Hybrid assert retry: L2 failed (%s); refreshing snapshot",
                                (verification.reason or '')[:120],
                            )
                            await asyncio.sleep(HYBRID_SETTLE_SECONDS)
                            post_snapshot = await mcp_manager.get_dom_snapshot()
                            verification = await asyncio.wait_for(
                                verify_expected_result(
                                    expected_result,
                                    post_snapshot,
                                    step_description=raw_desc,
                                    client=llm_client,
                                    model=model,
                                ),
                                timeout=30,
                            )
                            result['assert_retry_attempted'] = True
                            if not verification.passed:
                                result['success'] = False
                                result['error'] = f"预期结果验证失败: {verification.reason}"
                                if used_replay:
                                    result["invalidate_learned_locator"] = True
                                await _capture_screenshot(
                                    mcp_manager, screenshot_dir, step_number, result,
                                )
                            else:
                                result['verification'] = verification.reason
                        else:
                            result['verification'] = verification.reason
                    except asyncio.TimeoutError:
                        logger.warning("步骤 %s 预期结果验证超时", step_number)
                    except Exception as exc:
                        logger.warning(
                            "步骤 %s 预期结果验证异常: %s", step_number, exc, exc_info=True,
                        )

        if result["success"] and memory_enabled and tool_call is not None:
            action = getattr(tool_call, "action", None)
            selector = getattr(tool_call, "selector", None)
            value = getattr(tool_call, "value", None)
            if is_learnable_action(action) and selector:
                try:
                    fp = extract_from_snapshot(
                        snapshot, str(selector), action=str(action), value=value,
                    )
                    if fp:
                        if used_replay and cached_fp:
                            result["learned_locator"] = bump_hit_count(cached_fp)
                        else:
                            result["learned_locator"] = fp
                except Exception as exc:
                    logger.debug("learn locator failed: %s", exc)

    except Exception as exc:
        result['error'] = f'步骤执行异常: {exc}'
        logger.warning("步骤 %s 异常: %s", step_number, exc, exc_info=True)
        if not result.get('screenshot_path'):
            await _capture_screenshot(mcp_manager, screenshot_dir, step_number, result)

    result['duration_ms'] = (time.monotonic() - t_start) * 1000
    return result
