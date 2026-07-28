# core/browser_use_exec.py
"""browser-use NL step execution helpers (no app/DB imports).

Safe for Agent client offline packaging — only depends on browser-use + stdlib.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def resolve_system_prompt_override() -> str | None:
    """Return override text when browser_use cannot load its packaged prompts.

    PyInstaller / incomplete installs often miss ``browser_use.agent.system_prompts``.
    Prefer the real package; fall back to vendored copies under ``core/browser_use_prompts``.
    """
    try:
        import browser_use.agent.system_prompts  # noqa: F401
        return None
    except Exception:
        pass

    # Installed package layout but importlib.resources failed (frozen apps)
    try:
        import browser_use

        pkg = Path(browser_use.__file__).resolve().parent
        for rel in (
            ("agent", "system_prompts", "system_prompt.md"),
            ("agent", "system_prompt.md"),
        ):
            p = pkg.joinpath(*rel)
            if p.is_file():
                logger.warning("browser-use system_prompts 模块缺失，改用文件: %s", p)
                return p.read_text(encoding="utf-8")
    except Exception:
        logger.debug("scan installed browser_use prompts failed", exc_info=True)

    vendored = Path(__file__).resolve().parent / "browser_use_prompts" / "system_prompt.md"
    if vendored.is_file():
        logger.warning("browser-use system_prompts 缺失，使用内置回退模板: %s", vendored)
        return vendored.read_text(encoding="utf-8")

    raise RuntimeError(
        "browser-use 缺少 system_prompts（No module named "
        "'browser_use.agent.system_prompts'）。请用离线包 wheels 重装 browser-use，"
        "或重新执行 install_and_build.bat（需 --collect-all browser_use）。"
    )


def build_step_task(
    *,
    description: str,
    expected_result: str | None,
    step_order: int,
    base_url: str | None,
) -> str:
    expected = (expected_result or "").strip()
    expected_block = (
        f"预期结果（必须据此判断成败）:\n{expected}\n"
        if expected
        else "预期结果: （未写明；完成本步操作即可，不要编造断言）\n"
    )
    base = (
        f"当前步骤编号: {step_order}\n"
        f"步骤描述:\n{description}\n\n"
        f"{expected_block}\n"
        "规则:\n"
        "- 只完成本步骤，不要擅自执行后续无关操作\n"
        "- 若页面未打开且需要导航，可先打开相关页面\n"
        "- 完成后必须 done：成功则 success=true，失败则 success=false 并说明原因\n"
        "- 判断成败时只依据页面真实可见内容，不要臆造文案\n"
    )
    if base_url:
        base = f"测试环境 BASE URL: {base_url}\n\n" + base
    return base


# Back-compat aliases used by unit tests
_build_step_task = build_step_task


def create_browser_use_llm_from_config(
    *,
    api_key: str,
    api_base: str | None,
    model: str | None,
):
    """Build browser-use ChatOpenAI from explicit credentials."""
    from browser_use import ChatOpenAI

    return ChatOpenAI(
        model=model or "gpt-4o-mini",
        api_key=api_key,
        base_url=api_base or None,
        temperature=0.2,
    )


def history_to_step_fields(history) -> dict[str, Any]:
    """Map AgentHistoryList to VoyanTest step result fields."""
    success: bool | None = None
    error = None
    thinking = ""
    action = ""
    try:
        success = history.is_successful()
    except Exception:
        success = None
    try:
        if hasattr(history, "model_thoughts"):
            thoughts = history.model_thoughts() or []
            if thoughts:
                thinking = str(thoughts[-1])[:2000]
    except Exception:
        pass
    try:
        if hasattr(history, "action_names"):
            names = history.action_names() or []
            action = " → ".join(str(n) for n in names if n)[:500]
    except Exception:
        pass
    try:
        final = history.final_result() if hasattr(history, "final_result") else None
        if success is False and final:
            error = str(final)[:1000]
        elif success is False:
            error = "browser-use 判定步骤失败"
        elif success is None:
            error = "browser-use 未返回 done/success"
            success = False
    except Exception:
        if success is None:
            success = False
            error = "browser-use 结果解析失败"
    return {
        "success": bool(success),
        "thinking": thinking,
        "action": action or "browser_use",
        "error": error,
    }


_history_to_step_fields = history_to_step_fields


async def execute_nl_steps_browser_use(
    steps: list[dict],
    *,
    llm,
    base_url: str | None = None,
    headless: bool = True,
    max_steps_per_nl: int = 20,
    browser_session=None,
    stop_browser: bool = True,
) -> list[dict]:
    """Run NL steps with a shared browser-use session. No DB side effects.

    Each ``steps`` item: ``{step_order, description, expected_result?}``.

    ``browser_session``: reuse an existing BrowserSession (batch follow-up).
    ``stop_browser``: when False, leave the session open for the next case.
    """
    from browser_use import Agent, BrowserSession

    step_results: list[dict] = []
    prompt_override = resolve_system_prompt_override()
    agent_common = {
        "llm": llm,
        "use_vision": "auto",
        "max_failures": 2,
    }
    if prompt_override is not None:
        agent_common["override_system_message"] = prompt_override

    # Disable default extensions: sync download can block the asyncio loop
    # (WS heartbeat dies → server unregisters the agent mid-run).
    browser = browser_session or BrowserSession(
        headless=headless,
        keep_alive=True,
        enable_default_extensions=False,
    )
    try:
        if base_url:
            # Keep URL alone on a line: browser-use URL extraction may swallow
            # trailing Chinese punctuation into the navigate target.
            open_agent = Agent(
                task=(
                    f"Open this URL exactly (copy as-is):\n{base_url}\n"
                    "Wait until the page has basically loaded, then call done with success=true."
                ),
                browser_session=browser,
                max_actions_per_step=5,
                **agent_common,
            )
            try:
                await open_agent.run(max_steps=8)
            except Exception as exc:
                logger.warning("browser-use 打开 BASE URL 失败: %s", exc, exc_info=True)

        failed_step: int | None = None
        for step in steps:
            order = step.get("step_order") or step.get("step_number") or 0
            desc = step.get("description") or ""
            expected = step.get("expected_result")
            if failed_step is not None:
                step_results.append({
                    "step_number": order,
                    "original_description": desc,
                    "success": False,
                    "status": "skipped",
                    "thinking": "",
                    "action": "",
                    "next_goal": "",
                    "error": f"因步骤{failed_step}失败而跳过",
                    "screenshot_path": None,
                    "duration_ms": 0,
                    "backend": "browser_use",
                })
                continue

            t0 = time.monotonic()
            task = build_step_task(
                description=desc,
                expected_result=expected,
                step_order=order,
                base_url=base_url,
            )
            logger.info("--- browser-use Step %s: %s ---", order, desc)
            agent = Agent(
                task=task,
                browser_session=browser,
                max_actions_per_step=8,
                **agent_common,
            )
            try:
                history = await agent.run(max_steps=max_steps_per_nl)
                fields = history_to_step_fields(history)
            except Exception as exc:
                logger.exception("browser-use step %s failed", order)
                fields = {
                    "success": False,
                    "thinking": "",
                    "action": "browser_use",
                    "error": str(exc),
                }

            result = {
                "step_number": order,
                "original_description": desc,
                "success": fields["success"],
                "thinking": fields.get("thinking") or "",
                "action": fields.get("action") or "browser_use",
                "next_goal": "",
                "error": fields.get("error"),
                "screenshot_path": None,
                "duration_ms": (time.monotonic() - t0) * 1000,
                "backend": "browser_use",
            }
            step_results.append(result)
            if not result["success"]:
                failed_step = order
    finally:
        if stop_browser:
            try:
                if hasattr(browser, "stop"):
                    await browser.stop()
                elif hasattr(browser, "close"):
                    await browser.close()
            except Exception as exc:
                logger.warning("browser-use session cleanup: %s", exc, exc_info=True)

    return step_results
