"""044 US2：计划模式状态机（纯函数）。

四阶段：理解 → 设计 → 复查 → 批准。计划模式只读，回合必须以
AskUserQuestion 或 exit_plan_mode 结束，避免模型只输出文本无限等待。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

PHASES = ("understand", "design", "review", "approve")

PHASE_LABELS = {
    "understand": "理解（只读探索，最多 3 个并行查询）",
    "design": "设计（基于现有代码/用例模式给出可执行方案）",
    "review": "复查（核对关键对象，必要时用 AskUserQuestion 澄清）",
    "approve": "批准（调用 exit_plan_mode 提交计划，等待用户确认）",
}


@dataclass
class PlanState:
    active: bool = False
    phase: str = "understand"
    plan_text: str = ""
    turn: int = 0  # 计划模式下的对话轮次（用于提醒间隔）


def enter_plan_mode(state: PlanState) -> PlanState:
    return replace(state, active=True, phase="understand", plan_text="", turn=0)


def exit_plan_mode(state: PlanState, plan: str) -> PlanState:
    return replace(state, active=False, phase="approve", plan_text=(plan or "").strip())


def advance_phase(state: PlanState) -> PlanState:
    if not state.active:
        return state
    try:
        idx = PHASES.index(state.phase)
    except ValueError:
        return replace(state, phase="understand")
    nxt = PHASES[min(idx + 1, len(PHASES) - 1)]
    return replace(state, phase=nxt)


def plan_reminder(state: PlanState) -> str | None:
    """计划模式下每轮注入的提醒；非激活时返回 None。"""
    if not state.active:
        return None
    label = PHASE_LABELS.get(state.phase, PHASE_LABELS["understand"])
    return (
        "【计划模式 — 当前激活】\n"
        f"- 阶段：{label}\n"
        "- 计划模式下只读工具可用，不执行写操作；\n"
        "- 每个回合必须以 AskUserQuestion 或 exit_plan_mode 结束，不允许只输出文本等待；\n"
        "- 计划完成后调用 exit_plan_mode 提交计划，等待用户批准再执行。"
    )


def load_plan_state(refs) -> PlanState:
    """从 conversation.refs 读取计划状态（无则返回未激活）。"""
    for r in refs or []:
        if isinstance(r, dict) and r.get("type") == "plan":
            return PlanState(
                active=bool(r.get("active")),
                phase=str(r.get("phase") or "understand"),
                plan_text=str(r.get("plan_text") or ""),
                turn=int(r.get("turn") or 0),
            )
    return PlanState()


def dump_plan_state(refs, state: PlanState) -> list:
    """把计划状态写回 refs（保留其他引用；未激活且无计划文本时移除条目）。"""
    out = [
        dict(r) for r in (refs or [])
        if not (isinstance(r, dict) and r.get("type") == "plan")
    ]
    if state.active or state.plan_text:
        out.append({
            "type": "plan",
            "active": state.active,
            "phase": state.phase,
            "plan_text": state.plan_text,
            "turn": state.turn,
        })
    return out


def should_remind(state: PlanState, *, every: int = 5) -> bool:
    """计划提醒间隔：每 every 轮注入一次（第 1 轮必注入）。"""
    if not state.active:
        return False
    return state.turn % max(1, every) == 0
