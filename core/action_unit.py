"""Phase B：宏动作单元 —— 一次模型往返推进一段**机械**动作。

对齐 ZCode"一格推进多步"的收益，但保留我们的逐步审计与安全门：

- 决策可携带 ``unit``：一串机械子动作（填表/点击/选择/按键/等待/断言），
  一次 LLM 往返内由执行器顺序执行，省掉 N-1 次往返（每次往返 ≈ LLM 2-3s + 快照）。
- 每个子动作仍**逐步入账**（journal / 覆盖 / 工具调用），固化与报告口径不变。
- 子动作可带 ``fallbacks``（备用定位符：CSS / ``text=`` / ``role=``）：
  主定位失败时本地依次尝试，不花模型轮次。
- 瞬时失败（元素未就绪/超时/被替换）本地重试（与 Phase C 同一判定）。
- 止损：任一步在重试与备选都失败后**原地停止**，如实回报"第 k 步失败 + 已完成步数"，
  由下一轮重新观察决策；绝不吞错、绝不假通过。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.mcp_args import is_transient_action_error

logger = logging.getLogger(__name__)

# 允许放进单元的动作：机械、可顺序执行、无需截图/坐标/二次判断
ALLOWED_UNIT_ACTIONS: set[str] = {
    "click",
    "double_click",
    "right_click",
    "fill",
    "select",
    "press_key",
    "check",
    "hover",
    "scroll",
    "wait",
    "goto",
    "dialog",
    "assert_text",
}

# 需要 selector 的动作（缺失即丢弃该子动作）
_REQUIRE_SELECTOR = {
    "click", "double_click", "right_click", "fill", "select",
    "press_key", "check", "hover", "scroll", "assert_text",
}
# 需要 value 的动作
_REQUIRE_VALUE = {"fill", "select", "press_key", "wait", "dialog", "goto", "assert_text"}


@dataclass
class UnitStep:
    index: int
    action: str
    selector: str | None
    value: str | None
    element_desc: str | None
    fallbacks: list[str] = field(default_factory=list)
    timeout_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnitOutcome:
    success: bool
    executed: list[dict[str, Any]] = field(default_factory=list)
    failed_index: int | None = None
    failed_action: dict[str, Any] | None = None
    error: str | None = None

    @property
    def executed_count(self) -> int:
        return len(self.executed)


def normalize_unit(
    raw: Any,
    *,
    max_steps: int = 6,
    is_valid_ref: Callable[[str], bool] | None = None,
) -> tuple[list[UnitStep], list[str]]:
    """把 LLM 给的 ``unit`` 规范化成可执行子动作序列。

    Returns:
        (steps, problems)：problems 记录被丢弃的子动作与原因（用于日志/审计）。
    """
    problems: list[str] = []
    steps: list[UnitStep] = []
    if not isinstance(raw, list):
        return [], ["unit 不是数组"]

    for i, item in enumerate(raw):
        if len(steps) >= int(max_steps):
            problems.append(f"超过上限 {max_steps}，忽略其余 {len(raw) - i} 项")
            break
        if not isinstance(item, dict):
            problems.append(f"#{i} 不是对象")
            continue
        name = str(item.get("action") or item.get("name") or "").strip().lower()
        name = name.replace("browser_", "")
        if name not in ALLOWED_UNIT_ACTIONS:
            problems.append(f"#{i} 动作不允许: {name or '(空)'}")
            continue
        selector = item.get("selector")
        selector = str(selector).strip() if selector is not None else None
        value = item.get("value")
        value = value if value is None else (str(value) if not isinstance(value, (int, float, bool)) else value)
        if name in _REQUIRE_SELECTOR and not selector:
            problems.append(f"#{i} {name} 缺 selector")
            continue
        if name in _REQUIRE_VALUE and (value is None or str(value).strip() == ""):
            problems.append(f"#{i} {name} 缺 value")
            continue
        if selector and is_valid_ref is not None and not is_valid_ref(selector):
            problems.append(f"#{i} ref 不在当前候选: {selector}")
            continue
        fallbacks: list[str] = []
        for fb in item.get("fallbacks") or []:
            fb_s = str(fb).strip()
            if fb_s and fb_s not in fallbacks:
                fallbacks.append(fb_s)
        raw_clean = {
            k: v for k, v in item.items()
            if k not in ("unit", "fallbacks") and v is not None
        }
        raw_clean["action"] = name
        steps.append(
            UnitStep(
                index=i,
                action=name,
                selector=selector,
                value=value if value is not None else None,
                element_desc=item.get("element_desc") or item.get("element"),
                fallbacks=fallbacks,
                timeout_ms=item.get("timeout_ms"),
                raw=raw_clean,
            )
        )
    return steps, problems


def expand_fallbacks(step: UnitStep) -> list[dict[str, Any]]:
    """主定位 + fallbacks → 依次尝试的动作列表（备用定位符为空则只有主定位）。"""
    base = dict(step.raw)
    attempts = [dict(base)]
    for fb in step.fallbacks:
        alt = dict(base)
        alt["selector"] = fb
        attempts.append(alt)
    return attempts


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def run_unit(
    steps: list[UnitStep],
    execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    on_step_ok: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
    retry_delays: tuple[float, ...] = (0.6, 1.5),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> UnitOutcome:
    """顺序执行单元；任一步最终失败即停止（已完成的步骤照常入账）。"""
    executed: list[dict[str, Any]] = []
    for step in steps:
        ok = False
        last: dict[str, Any] = {"success": False, "error": "not executed"}
        for attempt_action in expand_fallbacks(step):
            result = await execute(attempt_action)
            if not result.get("success") and is_transient_action_error(result.get("error")):
                for delay in retry_delays:
                    logger.info(
                        "unit: 第 %d 步瞬时失败，本地重试（%.1fs 后）: %s",
                        step.index + 1, delay, str(result.get("error"))[:80],
                    )
                    await sleep(delay)
                    result = await execute(attempt_action)
                    if result.get("success"):
                        break
            if result.get("success"):
                ok = True
                last = result
                executed.append(attempt_action)
                if on_step_ok is not None:
                    await _maybe_await(on_step_ok(attempt_action, result))
                break
            last = result
        if not ok:
            return UnitOutcome(
                success=False,
                executed=executed,
                failed_index=step.index,
                failed_action=step.raw,
                error=str(last.get("error") or "unknown"),
            )
    return UnitOutcome(success=True, executed=executed)
