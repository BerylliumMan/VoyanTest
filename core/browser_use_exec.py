# core/browser_use_exec.py
"""browser-use NL step execution helpers (no app/DB imports).

Safe for Agent client offline packaging — only depends on browser-use + stdlib.
"""

import base64
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Any]  # sync or returns awaitable


def _emit_progress(on_progress: ProgressCallback | None, message: str) -> None:
    """Fire progress callback; never raise into the execution path."""
    if not on_progress or not message:
        return
    try:
        on_progress(message)
    except Exception:
        logger.debug("on_progress failed", exc_info=True)


def _truncate(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _summarize_agent_output(output: Any) -> str:
    """Short summary of browser-use AgentOutput for server logs."""
    parts: list[str] = []
    try:
        state = getattr(output, "current_state", None)
        if state is not None:
            thinking = getattr(state, "thinking", None) or getattr(state, "evaluation_previous_goal", None)
            goal = getattr(state, "next_goal", None)
            if thinking:
                parts.append(f"think={_truncate(str(thinking), 120)}")
            if goal:
                parts.append(f"goal={_truncate(str(goal), 120)}")
        actions = getattr(output, "action", None) or getattr(output, "actions", None)
        if actions:
            names: list[str] = []
            for a in (actions if isinstance(actions, (list, tuple)) else [actions]):
                name = getattr(a, "name", None) or type(a).__name__
                # browser-use often uses model dump with one key = action type
                if hasattr(a, "model_dump"):
                    try:
                        dumped = a.model_dump(exclude_none=True)
                        if isinstance(dumped, dict) and dumped:
                            name = next(iter(dumped.keys()))
                    except Exception:
                        pass
                names.append(str(name))
            if names:
                parts.append("actions=" + ",".join(names[:6]))
    except Exception:
        return _truncate(str(output), 200)
    return _truncate("; ".join(parts) if parts else str(output), 300)


def _make_new_step_callback(
    on_progress: ProgressCallback | None,
    *,
    step_order: int,
    browser_session: Any | None = None,
) -> Callable[..., Any] | None:
    """Progress logger; also ensures newest-tab focus before the next observe.

    browser-use invokes this after LLM output and before/around action cycles;
    combined with ``register_should_stop_callback`` this keeps focus on popups.
    """
    if not on_progress and browser_session is None:
        return None

    async def _cb(browser_state: Any, output: Any, bu_step: int) -> None:
        if on_progress:
            summary = _summarize_agent_output(output)
            _emit_progress(
                on_progress,
                f"NL step {step_order} agent-turn {bu_step}: {summary}",
            )
        if browser_session is not None:
            try:
                await ensure_browser_use_on_newest_tab(
                    browser_session, settle_seconds=0.15,
                )
            except Exception:
                logger.debug("step-callback tab switch failed", exc_info=True)

    return _cb


def attach_browser_use_log_handler(
    *,
    on_progress: ProgressCallback | None,
    logger_names: tuple[str, ...] = ("browser_use", "bubus"),
) -> list[tuple[logging.Logger, logging.Handler]]:
    """Attach temporary handlers that forward library logs to on_progress.

    Returns list of (logger, handler) to remove later via ``detach_browser_use_log_handlers``.
    """
    if not on_progress:
        return []

    class _ProgressHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record)
                # Skip extremely noisy debug noise if somehow attached at DEBUG
                if record.levelno < logging.INFO:
                    return
                _emit_progress(on_progress, f"[{record.name}] {_truncate(msg, 400)}")
            except Exception:
                pass

    attached: list[tuple[logging.Logger, logging.Handler]] = []
    handler = _ProgressHandler(level=logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for name in logger_names:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        # Ensure INFO from library is not filtered if root is WARNING-only for that logger
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
        attached.append((lg, handler))
    return attached


def detach_browser_use_log_handlers(
    attached: list[tuple[logging.Logger, logging.Handler]],
) -> None:
    for lg, handler in attached:
        try:
            lg.removeHandler(handler)
        except Exception:
            pass


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
    learned_locator: dict | None = None,
) -> str:
    expected = (expected_result or "").strip()
    expected_block = (
        f"预期结果（必须据此判断成败）:\n{expected}\n"
        if expected
        else "预期结果: （未写明；完成本步操作即可，不要编造断言）\n"
    )
    base = (
        f"当前步骤编号: {step_order}\n"
        f"步骤描述（权威，用例已正确 — 请忠实执行，禁止自行改写意图）:\n{description}\n\n"
        f"{expected_block}\n"
        "规则:\n"
        "- 语义保真：【】/「」内文案是页面真实控件名；提交≠确定≠保存；查询≠搜索；禁止点「看起来差不多」的控件\n"
        "- 只完成本步骤一个主操作，不要擅自执行后续步骤、填未提及的字段、或提前提交表单\n"
        "- 禁止臆造输入值；只能使用步骤中写明的文本/选项\n"
        "- 若存在多个同样像的目标：先等待关键文案出现或报告失败，禁止随机点一个\n"
        "- 除非步骤明确要求打开/跳转/进入，否则不要离开当前页或关闭弹窗\n"
        "- 若页面未打开且步骤需要导航，可先打开相关页面\n"
        "- 下拉框/树选择/联想选项：输入文字后必须再点击匹配项完成选择，仅输入不算完成\n"
        "- 若点击打开了新浏览器标签页：立刻 switch 到新标签再继续（运行时也会自动切换）\n"
        "- 优先少步完成；看到目标选项就立刻点击，不要反复探索无关区域\n"
        "- 完成后必须 done：成功则 success=true，失败则 success=false 并说明原因\n"
        "- 判断成败时只依据页面真实可见内容与本步预期，不要臆造文案\n"
    )
    hint = ""
    if learned_locator:
        from core.locator_memory import format_hint_for_agent
        hint = format_hint_for_agent(learned_locator)
    if hint:
        base += f"- {hint}\n"
    if base_url:
        base = f"测试环境 BASE URL: {base_url}\n\n" + base
    return base


# Back-compat aliases used by unit tests
_build_step_task = build_step_task


async def list_browser_use_page_ids(session) -> list[str]:
    """Return page targetIds currently known to the BrowserSession."""
    if session is None:
        return []
    getter = getattr(session, "_cdp_get_all_pages", None)
    if not callable(getter):
        return []
    try:
        pages = await getter()
    except Exception as exc:
        logger.warning("browser-use list pages failed: %s", exc)
        return []
    ids: list[str] = []
    for p in pages or []:
        tid = p.get("targetId") if isinstance(p, dict) else None
        if tid:
            ids.append(tid)
    return ids


async def _dispatch_switch_tab(session, target_id: str | None) -> bool:
    """Dispatch SwitchTabEvent without nested-await deadlocks when possible."""
    bus = getattr(session, "event_bus", None)
    if bus is None:
        return False
    try:
        from browser_use.browser.events import SwitchTabEvent

        event = bus.dispatch(SwitchTabEvent(target_id=target_id))
        # Prefer awaiting completion so agent_focus is updated before next observe.
        # If the bus deadlocks under nested handlers, callers should use create_task.
        await event
        return True
    except Exception as exc:
        logger.warning("browser-use SwitchTabEvent failed: %s", exc, exc_info=True)
        return False


def _tab_auto_state(session) -> dict[str, Any] | None:
    state = getattr(session, "_voyantest_tab_auto", None)
    return state if isinstance(state, dict) else None


def _set_preferred_tab(session, target_id: str | None) -> None:
    state = _tab_auto_state(session)
    if state is None or not target_id:
        return
    state["preferred_target_id"] = target_id


def _preferred_tab_id(session) -> str | None:
    state = _tab_auto_state(session)
    if state is None:
        return None
    tid = state.get("preferred_target_id")
    return tid if isinstance(tid, str) and tid else None


async def ensure_browser_use_on_newest_tab(
    session,
    *,
    settle_seconds: float = 0.0,
) -> bool:
    """Re-focus the sticky preferred tab opened during this session.

    Important: CDP ``Target.getTargets`` order is **not** creation order. Never
    treat ``pages[-1]`` as newest — that flips focus back to an older tab.

    Preferred id is set by TabCreated (new pages only) or by
    ``switch_browser_use_to_newest_tab_if_opened`` via before/after diff.
    """
    import asyncio

    if session is None:
        return False
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    preferred = _preferred_tab_id(session)
    if not preferred:
        return False
    pages = await list_browser_use_page_ids(session)
    if preferred not in pages:
        state = _tab_auto_state(session)
        if state is not None:
            state["preferred_target_id"] = None
        return False
    focus = getattr(getattr(session, "agent_focus", None), "target_id", None)
    if focus == preferred:
        return False
    ok = await _dispatch_switch_tab(session, preferred)
    if ok:
        logger.info(
            "browser-use ensured focus on preferred tab %s (was %s)",
            preferred[-8:],
            (focus or "?")[-8:],
        )
    return ok


async def switch_browser_use_to_newest_tab_if_opened(
    session,
    *,
    page_ids_before: list[str] | None = None,
    settle_seconds: float = 0.5,
    retries: int = 3,
    retry_interval: float = 0.4,
) -> bool:
    """If page set grew, focus a newly appeared page (align with MCP auto-tab).

    browser-use's click watchdog tries this with a short 0.1s wait and then may
    stay on the old tab if the popup is slow. We re-check after settle + retries.
    Uses before/after targetId diff — never ``pages[-1]``.
    """
    import asyncio

    if session is None:
        return False
    before = list(page_ids_before or [])
    if not before:
        # Without a baseline we cannot tell which id is new (CDP order unstable).
        return False
    before_set = set(before)
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        wait = settle_seconds if attempt == 0 else retry_interval
        if wait > 0:
            await asyncio.sleep(wait)
        after = await list_browser_use_page_ids(session)
        if not after:
            continue
        new_ids = [tid for tid in after if tid not in before_set]
        if not new_ids:
            continue
        # Prefer the preferred sticky tab if it is among the new ids.
        preferred = _preferred_tab_id(session)
        newest_id = preferred if preferred in new_ids else new_ids[-1]
        _set_preferred_tab(session, newest_id)

        focus = getattr(getattr(session, "agent_focus", None), "target_id", None)
        if focus == newest_id:
            return False

        ok = await _dispatch_switch_tab(session, newest_id)
        if ok:
            logger.info("browser-use switched to new tab %s", newest_id[-8:])
            return True
    return False


def enable_browser_use_auto_switch_new_tabs(session) -> None:
    """Arm a TabCreatedEvent listener that focuses newly created pages.

    Ignored until ``arm_browser_use_auto_switch_new_tabs`` is called, so initial
    connect TabCreated events do not steal focus.

    Only tabs whose targetId was **not** in the baseline at arm-time are treated
    as new (reconnect emits TabCreated for existing tabs).

    Switch is scheduled via ``asyncio.create_task`` (not awaited in the event
    handler) to avoid bubus deadlocks with nested SwitchTabEvent waits, and
    delayed so it runs after browser-use's click handler resets focus to the
    opener tab.
    """
    if session is None:
        return
    state = getattr(session, "_voyantest_tab_auto", None)
    if state is not None:
        return

    state: dict[str, Any] = {
        "armed": False,
        "preferred_target_id": None,
        "baseline_ids": set(),
    }
    session._voyantest_tab_auto = state
    bus = getattr(session, "event_bus", None)
    if bus is None:
        return

    async def _on_tab_created(event) -> None:
        if not state.get("armed"):
            return
        tid = getattr(event, "target_id", None)
        if not tid:
            return
        baseline = state.get("baseline_ids") or set()
        if tid in baseline:
            # Existing tab discovered on CDP attach — do not steal focus.
            return
        # Sticky preferred: survive later CDP list reordering / opener refocus.
        state["preferred_target_id"] = tid
        baseline.add(tid)
        state["baseline_ids"] = baseline

        async def _switch_later() -> None:
            import asyncio

            # Click watchdog: sleep 0.1s → reset focus to opener (focus=True) →
            # maybe switch. Wait longer and switch to *this* targetId.
            await asyncio.sleep(0.6)
            if not state.get("armed"):
                return
            if state.get("preferred_target_id") != tid:
                return
            try:
                switched = await ensure_browser_use_on_newest_tab(
                    session, settle_seconds=0.0,
                )
                if switched:
                    logger.info(
                        "browser-use auto-switched after TabCreated %s",
                        str(tid)[-8:],
                    )
            except Exception as exc:
                logger.warning(
                    "browser-use TabCreated auto-switch failed: %s", exc,
                )

        try:
            import asyncio

            asyncio.create_task(_switch_later())
        except Exception as exc:
            logger.warning("failed to schedule TabCreated switch: %s", exc)

    try:
        from browser_use.browser.events import TabCreatedEvent

        bus.on(TabCreatedEvent, _on_tab_created)
    except Exception as exc:
        logger.warning("failed to register TabCreated auto-switch: %s", exc)


async def arm_browser_use_auto_switch_new_tabs(session) -> None:
    """Start auto-switching after session warmup / BASE URL open.

    Snapshots current page ids as baseline so reconnect TabCreated events for
    pre-existing tabs are ignored.
    """
    state = _tab_auto_state(session)
    if state is None:
        return
    try:
        ids = await list_browser_use_page_ids(session)
        state["baseline_ids"] = set(ids)
    except Exception:
        state["baseline_ids"] = set(state.get("baseline_ids") or set())
    state["armed"] = True


def _compose_should_stop_callback(
    session,
    existing: Callable[..., Any] | None = None,
):
    """Between agent actions/turns, restore sticky preferred-tab focus."""

    async def _cb() -> bool:
        try:
            await ensure_browser_use_on_newest_tab(session, settle_seconds=0.0)
        except Exception:
            logger.debug("ensure preferred tab in should_stop failed", exc_info=True)
        if existing is None:
            return False
        try:
            result = existing()
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            return bool(result)
        except Exception:
            logger.debug("existing should_stop callback failed", exc_info=True)
            return False

    return _cb


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


def maximize_browser_session(session) -> None:
    """Force headed Chrome to start maximized.

    browser-use's profile validator copies display size into ``window_size``,
    which makes launch use ``--window-size=WxH`` instead of ``--start-maximized``.
    Clear that and ensure the maximize flag is present.
    """
    profile = getattr(session, "browser_profile", None)
    if profile is None:
        return
    try:
        profile.window_size = None
    except Exception:
        logger.debug("clear window_size failed", exc_info=True)
    try:
        args = [a for a in (list(getattr(profile, "args", None) or [])) if a != "--start-maximized"]
        if "--disable-popup-blocking" not in args:
            args.append("--disable-popup-blocking")
        args.append("--start-maximized")
        profile.args = args
    except Exception:
        logger.debug("set --start-maximized failed", exc_info=True)


def create_browser_session(
    *,
    headless: bool = True,
    keep_alive: bool = True,
    enable_default_extensions: bool = False,
    cdp_url: str | None = None,
    **kwargs,
):
    """Create a BrowserSession; headed mode starts maximized.

    ``cdp_url``: attach to an existing Chromium (hybrid MCP fallback). Skip maximize.
    """
    from browser_use import BrowserSession

    if cdp_url:
        return BrowserSession(
            cdp_url=cdp_url,
            keep_alive=keep_alive,
            enable_default_extensions=enable_default_extensions,
            is_local=True,
            **kwargs,
        )

    session = BrowserSession(
        headless=headless,
        keep_alive=keep_alive,
        enable_default_extensions=enable_default_extensions,
        **kwargs,
    )
    # Ensure popup blocker is off even in headless (target=_blank / window.open).
    try:
        profile = getattr(session, "browser_profile", None)
        if profile is not None:
            args = list(getattr(profile, "args", None) or [])
            if "--disable-popup-blocking" not in args:
                args.append("--disable-popup-blocking")
                profile.args = args
    except Exception:
        logger.debug("set --disable-popup-blocking failed", exc_info=True)
    if not headless:
        maximize_browser_session(session)
    return session


async def capture_browser_screenshot(
    browser,
    *,
    step_order: int | None = None,
    screenshots_dir: str | None = None,
) -> tuple[str | None, str | None]:
    """Take a PNG screenshot. Returns ``(screenshot_path, screenshot_base64)``."""
    import asyncio

    if browser is None:
        return None, None
    take = getattr(browser, "take_screenshot", None)
    if not callable(take):
        return None, None
    try:
        data = await take(full_page=False, format="png")
        if not data:
            return None, None
        b64 = base64.b64encode(data).decode("ascii")
        path = None
        if screenshots_dir:
            await asyncio.to_thread(os.makedirs, screenshots_dir, exist_ok=True)
            name = f"step_{step_order or 'x'}.png"
            path = os.path.join(screenshots_dir, name).replace("\\", "/")
            with open(path, "wb") as f:
                f.write(data)
        return path, b64
    except Exception as exc:
        logger.warning("browser-use screenshot failed: %s", exc, exc_info=True)
        return None, None


def persist_step_screenshot_files(
    step_results: list[dict],
    output_dir: str | None,
) -> list[dict]:
    """Decode ``screenshot_base64`` into ``output_dir/screenshots`` and set ``screenshot_path``."""
    if not output_dir or not step_results:
        return step_results
    ss_dir = os.path.join(output_dir, "screenshots")
    os.makedirs(ss_dir, exist_ok=True)
    for r in step_results:
        b64 = r.pop("screenshot_base64", None)
        if not b64 or r.get("screenshot_path"):
            continue
        try:
            order = r.get("step_number") or "x"
            ss_path = os.path.join(ss_dir, f"step_{order}.png").replace("\\", "/")
            with open(ss_path, "wb") as f:
                f.write(base64.b64decode(b64))
            r["screenshot_path"] = ss_path
        except Exception as exc:
            logger.warning("persist screenshot failed: %s", exc, exc_info=True)
    return step_results


def _is_max_steps_incomplete(error: str | None) -> bool:
    if not error:
        return False
    low = error.lower()
    return "max_steps" in low or "reached max" in low or "maximum step" in low


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
    max_steps_per_nl: int = 30,
    browser_session=None,
    stop_browser: bool = True,
    screenshots_dir: str | None = None,
    cdp_url: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[dict]:
    """Run NL steps with a shared browser-use session. No DB side effects.

    Each ``steps`` item: ``{step_order, description, expected_result?}``.

    ``browser_session``: reuse an existing BrowserSession (batch follow-up).
    ``stop_browser``: when False, leave the session open for the next case.
    ``screenshots_dir``: if set, also write PNGs there (server-side runs).
    ``cdp_url``: attach to existing Chromium (hybrid MCP fallback); implies no stop.
    ``on_progress``: optional sync callback for step / agent-turn progress lines.
    """
    from browser_use import Agent

    step_results: list[dict] = []
    prompt_override = resolve_system_prompt_override()

    if cdp_url:
        stop_browser = False

    log_handlers = attach_browser_use_log_handler(on_progress=on_progress)

    # Disable default extensions: sync download can block the asyncio loop
    # (WS heartbeat dies → server unregisters the agent mid-run).
    browser = browser_session or create_browser_session(
        headless=headless,
        keep_alive=True,
        enable_default_extensions=False,
        cdp_url=cdp_url,
    )
    enable_browser_use_auto_switch_new_tabs(browser)

    agent_common: dict[str, Any] = {
        "llm": llm,
        "use_vision": "auto",
        "max_failures": 2,
        # Between multi_act actions / turns: jump to newest tab if click opened one.
        "register_should_stop_callback": _compose_should_stop_callback(browser),
    }
    if prompt_override is not None:
        agent_common["override_system_message"] = prompt_override

    try:
        if base_url:
            # Keep URL alone on a line: browser-use URL extraction may swallow
            # trailing Chinese punctuation into the navigate target.
            _emit_progress(on_progress, f"Opening BASE URL: {base_url}")
            open_agent = Agent(
                task=(
                    f"Open this URL exactly (copy as-is):\n{base_url}\n"
                    "Wait until the page has basically loaded, then call done with success=true."
                ),
                browser_session=browser,
                max_actions_per_step=5,
                register_new_step_callback=_make_new_step_callback(
                    on_progress, step_order=0, browser_session=browser,
                ),
                **agent_common,
            )
            try:
                await open_agent.run(max_steps=8)
            except Exception as exc:
                logger.warning("browser-use 打开 BASE URL 失败: %s", exc, exc_info=True)
                _emit_progress(on_progress, f"BASE URL open failed: {exc}")

        # After warmup: auto-focus newly created tabs (target=_blank / window.open).
        # Baseline current ids so reconnect TabCreated for existing tabs is ignored.
        await arm_browser_use_auto_switch_new_tabs(browser)

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
            cached_fp = step.get("learned_locator") if isinstance(step.get("learned_locator"), dict) else None
            memory_enabled = True
            try:
                from app.runtime_config import healing_config as _hc
                memory_enabled = bool(getattr(_hc, "locator_memory_enabled", True))
            except Exception:
                memory_enabled = True

            used_replay = False
            fields: dict[str, Any] | None = None
            if memory_enabled and cached_fp:
                from core.locator_memory import try_replay_browser_use
                replay = await try_replay_browser_use(
                    browser, cached_fp, step_description=desc,
                )
                if replay.get("success") and not replay.get("skipped"):
                    used_replay = True
                    fields = {
                        "success": True,
                        "thinking": "locator_memory browser-use CDP replay",
                        "action": "browser_use_replay",
                        "error": None,
                    }

            if not used_replay:
                task = build_step_task(
                    description=desc,
                    expected_result=expected,
                    step_order=order,
                    base_url=base_url,
                    learned_locator=cached_fp if memory_enabled else None,
                )
                logger.info("--- browser-use Step %s: %s ---", order, desc)
                _emit_progress(
                    on_progress,
                    f"--- Step {order} start: {_truncate(desc, 160)} ---",
                )
                pages_before = await list_browser_use_page_ids(browser)
                step_cb = _make_new_step_callback(
                    on_progress,
                    step_order=int(order) or 0,
                    browser_session=browser,
                )
                agent_kwargs = dict(agent_common)
                if step_cb is not None:
                    agent_kwargs["register_new_step_callback"] = step_cb
                agent = Agent(
                    task=task,
                    browser_session=browser,
                    max_actions_per_step=8,
                    **agent_kwargs,
                )
                try:
                    history = await agent.run(max_steps=max_steps_per_nl)
                    fields = history_to_step_fields(history)
                    if (not fields["success"]) and _is_max_steps_incomplete(fields.get("error")):
                        cont_budget = max(10, min(20, int(max_steps_per_nl)))
                        logger.info(
                            "browser-use step %s hit max_steps, continue +%s",
                            order, cont_budget,
                        )
                        _emit_progress(
                            on_progress,
                            f"Step {order} hit max_steps, continue +{cont_budget}",
                        )
                        cont_task = (
                            "继续完成尚未完成的操作，不要从头开始。\n"
                            f"原步骤:\n{desc}\n\n"
                            f"上次进度/原因:\n{fields.get('error') or ''}\n\n"
                            "若是下拉/树选择，直接点击已出现的匹配选项。"
                            "完成后 done(success=true)；仍无法完成则 done(success=false)。"
                        )
                        cont_agent = Agent(
                            task=cont_task,
                            browser_session=browser,
                            max_actions_per_step=8,
                            **agent_kwargs,
                        )
                        history = await cont_agent.run(max_steps=cont_budget)
                        fields = history_to_step_fields(history)
                except Exception as exc:
                    logger.exception("browser-use step %s failed", order)
                    _emit_progress(on_progress, f"Step {order} exception: {exc}")
                    fields = {
                        "success": False,
                        "thinking": "",
                        "action": "browser_use",
                        "error": str(exc),
                    }

                try:
                    await switch_browser_use_to_newest_tab_if_opened(
                        browser,
                        page_ids_before=pages_before,
                        settle_seconds=0.6,
                        retries=4,
                        retry_interval=0.5,
                    )
                except Exception as exc:
                    logger.warning("post-step new-tab switch failed: %s", exc)
            else:
                logger.info("--- browser-use Step %s: locator replay ---", order)
                _emit_progress(on_progress, f"--- Step {order}: locator replay ---")
                pages_before = await list_browser_use_page_ids(browser)

            assert fields is not None

            ss_path, ss_b64 = None, None
            # Always capture on failure; also on success for report consistency
            ss_path, ss_b64 = await capture_browser_screenshot(
                browser, step_order=order, screenshots_dir=screenshots_dir,
            )

            result = {
                "step_number": order,
                "original_description": desc,
                "success": fields["success"],
                "thinking": fields.get("thinking") or "",
                "action": fields.get("action") or "browser_use",
                "next_goal": "",
                "error": fields.get("error"),
                "screenshot_path": ss_path,
                "screenshot_base64": ss_b64,
                "duration_ms": (time.monotonic() - t0) * 1000,
                "backend": "browser_use",
            }
            step_results.append(result)
            status = "passed" if result["success"] else "failed"
            err_bit = f" — {_truncate(str(result.get('error') or ''), 160)}" if not result["success"] else ""
            _emit_progress(
                on_progress,
                f"--- Step {order} {status} ({result['duration_ms']:.0f}ms){err_bit} ---",
            )
            if not result["success"]:
                failed_step = order
    finally:
        detach_browser_use_log_handlers(log_handlers)
        if stop_browser:
            try:
                if hasattr(browser, "stop"):
                    await browser.stop()
                elif hasattr(browser, "close"):
                    await browser.close()
            except Exception as exc:
                logger.warning("browser-use session cleanup: %s", exc, exc_info=True)

    return step_results
