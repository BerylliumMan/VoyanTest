"""044：system-reminder 的定义、包装与调度。

只做两件事：把运行时事实以固定格式注入模型上下文，并保证提醒有间隔与次数上限。
"""

from __future__ import annotations

from dataclasses import dataclass

TODO_REMINDER_TEXT = (
    "The TodoWrite tool hasn't been used recently. If you're working on tasks that "
    "would benefit from tracking progress, consider using a todo list - create one now. "
    "This is just a gentle reminder - ignore if not applicable."
)


@dataclass
class Reminder:
    source: str
    content: str


def wrap(reminder: Reminder) -> str:
    """固定信封；嵌套标签转义，防止内容破坏结构。"""
    content = str(reminder.content).replace("<system-reminder>", "&lt;system-reminder>")
    return f"<system-reminder>\n{content}\n</system-reminder>"


class ReminderTracker:
    """按对话轮次调度提醒：重复调用只在阈值触发一次，todo 提醒按间隔触发。"""

    def __init__(self, *, repeat_threshold: int = 3, todo_interval: int = 10):
        self.repeat_threshold = max(1, int(repeat_threshold))
        self.todo_interval = max(1, int(todo_interval))
        self._counts: dict[str, int] = {}
        self._reminded: set[str] = set()
        self._last_todo_round = 0

    def on_tool_round(self, round_no: int, signatures: list[str]) -> list[Reminder]:
        out: list[Reminder] = []
        for sig in signatures or []:
            self._counts[sig] = self._counts.get(sig, 0) + 1
            n = self._counts[sig]
            if n >= self.repeat_threshold and sig not in self._reminded:
                self._reminded.add(sig)
                name = sig.split("::", 1)[0]
                out.append(Reminder(
                    source="repeat",
                    content=(
                        f"相同调用（{name}，同参数）已重复 {n} 次，结果不会变化——"
                        "请勿再重复调用；信息不足时请换用其他工具或直接给出结论。"
                    ),
                ))
        if not signatures and round_no - self._last_todo_round >= self.todo_interval:
            self._last_todo_round = round_no
            out.append(Reminder(source="todo", content=TODO_REMINDER_TEXT))
        return out
