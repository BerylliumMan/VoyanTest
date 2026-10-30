# Implementation Plan: 智能体对话能力增强

**Branch**: `044-chat-intelligence` | **Date**: 2026-10-29 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/044-chat-intelligence/spec.md`

**Note**: 本计划基于 ZCode（`zai-org/ZCode`）源码与本项目 `core/chat`、`core/tools` 的实读对照，任务级步骤见文末 `## Implementation Tasks`。

## Summary

把 ZCode 中经生产验证的六类机制移植到本项目的对话内核：结构化工具反馈、分层系统提示词、三层上下文压缩、system-reminder、Plan mode、子代理与项目记忆。核心目标是把对话从「8 轮扁平工具循环 + 24000 字符上下文」提升为「按模型窗口预算 + 工具证据回注 + 可规划可隔离」的智能体。

## Technical Context

**Language/Version**: Python 3.11（后端/对话内核）、TypeScript 5 + React 18（前端）
**Primary Dependencies**: FastAPI、SQLAlchemy async、Pydantic v2、httpx、React + Arco Design、Vite、pytest / pytest-asyncio、vitest
**Storage**: PostgreSQL 16（现有对话/消息表，不新增表）；项目记忆为 `data/memory/project-{id}/` 下的 Markdown 文件
**Testing**: `pytest tests/unit/`、`pytest tests/contract/`、`npx tsc --noEmit`、`npx vitest run`
**Target Platform**: Linux 服务端（Docker）+ React SPA
**Project Type**: Web 应用（FastAPI + React）
**Performance Goals**: 8 轮工具循环中节流额外等待 ≤3s；上下文窗口利用率 ≥60%；多用户并发时单用户首字节延迟增幅 ≤20%
**Constraints**: 不新增数据库表；不引入第三方依赖；每个增强机制可独立关闭且关闭后行为与现状一致；保持现有 REST 路径不变
**Scale/Scope**: 单对话历史 ≤500 条消息；单项目记忆 ≤200 条；单项目用例 ≤5000（沿用 042 约束）

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Principle | Status | Evidence |
|---|---|---|---|
| G1 | API-First Design | **PASS** | 记忆管理先落 `GET/DELETE /api/chat/memory` 接口，设置页再消费；对话增强不新增旁路 |
| G2 | Test Integrity | **PASS** | 全部机制走纯函数单测 + 契约测试；不写占位测试；`pytest tests/ -m "not integration"` 为合并门槛 |
| G3 | Asset Localization | **PASS** | 无新增前端外部资源；记忆管理复用现有设置页与 Arco 组件 |
| G4 | Configuration as Code | **PASS** | 新增 9 个 `chat_*` 配置统一进 `app/config.py`，经 `get_settings()` 读取 |
| G5 | Simplicity | **PASS（附注）** | 不引入向量库、不引入新框架、不新增表；章程 V 描述的前端技术栈（Vue 3 单文件、无构建工具）与仓库现状（React + Vite）不符，属章程过时，本次不改章程，也不新增构建工具 |

**Post-Phase 1 re-check**: PASS（无新增外部依赖；记忆为文件存储，不增加数据库耦合；所有开关默认开启但可关闭）

## Project Structure

### Documentation (this feature)

```text
specs/044-chat-intelligence/
├── plan.md              # 本文件
├── research.md          # Phase 0：机制取舍与替代方案
├── data-model.md        # Phase 1：记忆条目 / 计划状态 / 工具事件
├── quickstart.md        # Phase 1：手动验证脚本
├── contracts/
│   └── chat-intelligence.md   # 记忆接口 + 开关行为契约
└── tasks.md             # /speckit-tasks 生成
```

### Source Code (repository root)

```text
core/
├── chat/
│   ├── budget.py            # 新增：token 估算与上下文预算
│   ├── compact.py           # 新增：microcompact / 摘要选择 / auto compact
│   ├── reminders.py         # 新增：system-reminder 定义与调度
│   ├── plan_mode.py         # 新增：Plan mode 状态机
│   ├── subagent.py          # 新增：隔离子代理
│   ├── memory.py            # 新增：文件记忆索引与召回
│   ├── context.py           # 修改：工具事件回注、分层 system 块
│   ├── orchestrator.py      # 修改：摘要合并方向、预算、记忆注入
│   └── prompts.py           # 修改：稳定段/动态段拆分
└── tools/
    ├── feedback.py          # 新增：结构化工具反馈
    ├── loop.py              # 修改：接入 feedback / reminders / compact
    └── builtin/actions.py   # 修改：新增 enter/exit_plan_mode、agent 工具

app/
├── config.py                # 修改：新增 9 个 chat_* 配置
├── gen/model_client.py      # 修改：节流按 key 隔离
├── routers/chat/
│   └── memory.py            # 新增：记忆列表/删除接口
└── services/
    └── chat_memory.py       # 新增：记忆接口的读写编排（复用 core/chat/memory.py）

frontend/src/pages/settings/
└── MemoryPanel.tsx          # 新增：设置页记忆列表（查看/删除）

tests/
├── unit/chat/               # 新增 test_chat_budget / compact / reminders / plan_mode / subagent / memory
├── unit/tools/              # 新增 test_tool_feedback
└── contract/                # 新增 test_chat_memory_api
```

**Structure Decision**: 沿用现有 `core/`（纯函数与运行时）+ `app/`（API 与持久化）+ `frontend/`（SPA）三层结构。新增机制全部落在 `core/chat/` 与 `core/tools/`，API 只新增记忆管理一个路由组，避免扩散。

## Complexity Tracking

> 本章程检查无 violation，无需填表。

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |

---

## Implementation Tasks

> 以下任务按 TDD 编写：写失败测试 → 跑失败 → 实现 → 跑通过 → 提交。每个任务可独立评审、独立回滚。

### Task 1: token 估算与上下文预算

**Files:**
- Create: `core/chat/budget.py`
- Create: `tests/unit/chat/test_chat_budget.py`
- Modify: `app/config.py:52-60`

**Interfaces:**
- Produces:
  - `estimate_tokens(text: str) -> int`
  - `estimate_messages_tokens(messages: list[dict]) -> int`
  - `resolve_context_budget_tokens(*, settings=None, max_context_tokens: int = 131072) -> int`
  - `fit_to_budget(messages: list[dict], max_tokens: int) -> list[dict]`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.budget import estimate_tokens, estimate_messages_tokens, resolve_context_budget_tokens, fit_to_budget


def test_estimate_tokens_mixed_text():
    assert estimate_tokens("hello world") == 3
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("") == 0


def test_estimate_messages_tokens_sums_content_and_args():
    msgs = [
        {"role": "system", "content": "x" * 400},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}]},
    ]
    assert estimate_messages_tokens(msgs) >= 100


def test_resolve_context_budget_uses_70_percent_window():
    class S:
        chat_context_max_tokens = 0
    assert resolve_context_budget_tokens(settings=S(), max_context_tokens=131072) == 91750
    S.chat_context_max_tokens = 60000
    assert resolve_context_budget_tokens(settings=S(), max_context_tokens=131072) == 60000


def test_fit_to_budget_keeps_system_and_newest_messages():
    msgs = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "old " * 2000},
        {"role": "assistant", "content": "new " * 10},
    ]
    out = fit_to_budget(msgs, max_tokens=200)
    assert out[-1]["content"] == msgs[-1]["content"]
    assert sum(len(m["content"]) for m in out) < sum(len(m["content"]) for m in msgs)
```

- [ ] **Step 2: 运行并确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_budget.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.chat.budget'`

- [ ] **Step 3: 实现 `core/chat/budget.py`**

`estimate_tokens`：ASCII 4 字符 1 token，非 ASCII 1 字符 1 token。
`estimate_messages_tokens`：累加 `content`、`tool_calls[].function.arguments`、`name`。
`resolve_context_budget_tokens`：配置值 > 0 时返回配置；否则 `int(max_context_tokens * 0.7)`。
`fit_to_budget`：从最早非 system 消息开始截到 1200 字符加标记，保留最后 2 条不动。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_budget.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/budget.py tests/unit/chat/test_chat_budget.py app/config.py
git commit -m "feat(chat): 新增 token 估算与上下文预算模块"
```

---

### Task 2: 修复摘要合并方向并接入 token 预算

**Files:**
- Modify: `core/chat/orchestrator.py:130-159,291-298`
- Modify: `tests/unit/chat/test_chat_context.py`

**Interfaces:**
- Consumes: Task 1 `estimate_messages_tokens`、`resolve_context_budget_tokens`
- Produces: `_merge_summary(existing: str | None, chunk: str, *, max_chars: int = 8000) -> str`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.orchestrator import _merge_summary


def test_merge_summary_keeps_newest_content_when_over_limit():
    existing = "旧" * 8000
    chunk = "新摘要要点"
    merged = _merge_summary(existing, chunk, max_chars=100)
    assert merged.endswith(chunk)
    assert len(merged) <= 100


def test_merge_summary_without_existing_returns_chunk():
    assert _merge_summary(None, "abc", max_chars=10) == "abc"
```

- [ ] **Step 2: 运行并确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_context.py -q -k merge_summary`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 实现并替换第 153 行**

```python
def _merge_summary(existing: str | None, chunk: str, *, max_chars: int = 8000) -> str:
    chunk = (chunk or "").strip()
    if not existing:
        return chunk[:max_chars]
    room = max_chars - len(chunk) - 1
    if room <= 0:
        return chunk[:max_chars]
    return (existing[-room:] + "\n" + chunk).strip()
```

`_maybe_update_summary` 的预算判断改用 token 估算；`chat_context_max_chars` 保留为回退。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_context.py tests/unit/chat/test_chat_budget.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/orchestrator.py tests/unit/chat/test_chat_context.py
git commit -m "fix(chat): 摘要合并超限时保留最新内容并按 token 预算触发"
```

---

### Task 3: 历史工具事件回注

**Files:**
- Modify: `core/chat/context.py:17-39`
- Modify: `tests/unit/chat/test_chat_context.py`

**Interfaces:**
- Consumes: `ChatMessage.actions`（JSON 列）
- Produces:
  - `tool_events_digest(msg, *, max_items: int = 5, max_chars: int = 1200) -> str`
  - `message_text(msg, *, include_tool_events: bool = True) -> str`

- [ ] **Step 1: 写失败测试**

```python
from types import SimpleNamespace
from core.chat.context import message_text, tool_events_digest


def test_tool_events_digest_renders_recent_events():
    msg = SimpleNamespace(actions=[
        {"type": "tool", "tool": "search_cases", "status": "ok", "summary": "命中 3 条"},
        {"type": "tool", "tool": "run_case", "status": "failed", "summary": "环境不可达"},
    ])
    text = tool_events_digest(msg)
    assert "search_cases" in text and "命中 3 条" in text
    assert "run_case" in text and "环境不可达" in text


def test_tool_events_digest_caps_items_and_chars():
    msg = SimpleNamespace(actions=[
        {"type": "tool", "tool": f"t{i}", "status": "ok", "summary": "x" * 500}
        for i in range(10)
    ])
    text = tool_events_digest(msg, max_items=3, max_chars=200)
    assert len(text) <= 200
    assert "t9" in text and "t6" not in text


def test_message_text_includes_tool_events_by_default():
    msg = SimpleNamespace(content="结果如下", refs=[], cards=[], actions=[
        {"type": "tool", "tool": "get_batch", "status": "ok", "summary": "批次 12：3 通过 1 失败"},
    ])
    assert "get_batch" in message_text(msg)


def test_message_text_can_skip_tool_events():
    msg = SimpleNamespace(content="x", refs=[], cards=[], actions=[
        {"type": "tool", "tool": "get_batch", "status": "ok", "summary": "y"},
    ])
    assert "get_batch" not in message_text(msg, include_tool_events=False)
```

- [ ] **Step 2: 运行并确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_context.py -q -k tool_events`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 实现并接入 `message_text`**

`tool_events_digest` 取 `actions[-max_items:]`，渲染 `- 工具名(状态): 摘要`，整体截到 `max_chars`。
`message_text` 在 `cards_digest` 之前拼入该摘要；`include_tool_events=False` 时跳过。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_context.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/context.py tests/unit/chat/test_chat_context.py
git commit -m "feat(chat): 历史轮次的工具结果摘要回注上下文"
```

---

### Task 4: 结构化工具反馈

**Files:**
- Create: `core/tools/feedback.py`
- Create: `tests/unit/tools/test_tool_feedback.py`
- Modify: `core/tools/loop.py:114-118,148-166`

**Interfaces:**
- Produces:
  - `format_tool_error(name: str, message: str, *, hint: str | None = None) -> str`
  - `format_validation_error(name: str, issues: list[str]) -> str`
  - `tool_message_content(result: ToolResult, *, tool_name: str = "") -> str`

- [ ] **Step 1: 写失败测试**

```python
from core.tools.feedback import format_tool_error, format_validation_error, tool_message_content
from core.tools.result import ToolResult, error_result


def test_tool_error_uses_wrapper_and_includes_name():
    text = format_tool_error("run_case", "环境不可达")
    assert text.startswith("<tool_use_error>")
    assert "run_case" in text and "环境不可达" in text
    assert text.endswith("</tool_use_error>")


def test_validation_error_lists_each_issue():
    text = format_validation_error("read_file", ["缺少参数 file_path", "参数 offset 类型应为 number"])
    assert "缺少参数 file_path" in text
    assert "参数 offset 类型应为 number" in text


def test_tool_message_content_marks_truncation_with_recovery():
    r = ToolResult(ok=True, summary="ok", data={"truncated": True}, truncated=True)
    text = tool_message_content(r, tool_name="search_cases")
    assert "truncated" in text
    assert "翻页" in text


def test_failed_result_renders_structured_error():
    r = error_result("未知工具：foo")
    text = tool_message_content(r, tool_name="foo")
    assert "<tool_use_error>" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/tools/test_tool_feedback.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现并替换 `_tool_message_content`**

失败走 `<tool_use_error>工具 X 执行失败：...\n修复建议：...\n</tool_use_error>`；成功沿用 JSON；截断附恢复指引；参数解析失败走 `format_validation_error`。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/tools/test_tool_feedback.py tests/unit/tools/test_tool_loop.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/tools/feedback.py core/tools/loop.py tests/unit/tools/test_tool_feedback.py
git commit -m "feat(tools): 统一工具失败与校验错误的结构化反馈"
```

---

### Task 5: 节流按 Agent/用户隔离

**Files:**
- Modify: `app/gen/model_client.py:207-242,298-299`
- Modify: `app/config.py:52-60`
- Create: `tests/unit/test_llm_pacing.py`

**Interfaces:**
- Produces: `async def _pace_calls(key: str = "default") -> None`
- 配置：`chat_llm_min_interval_ms: int = 250`

- [ ] **Step 1: 写失败测试**

```python
import asyncio
import pytest
from app.gen import model_client


@pytest.mark.asyncio
async def test_pacing_is_isolated_per_key(monkeypatch):
    monkeypatch.setattr(model_client, "_LLM_MIN_INTERVAL", 0.2)
    model_client._last_call_ts.clear()
    t0 = asyncio.get_event_loop().time()
    await model_client._pace_calls("agent:1")
    await model_client._pace_calls("agent:2")
    assert asyncio.get_event_loop().time() - t0 < 0.15


@pytest.mark.asyncio
async def test_pacing_waits_within_same_key(monkeypatch):
    monkeypatch.setattr(model_client, "_LLM_MIN_INTERVAL", 0.2)
    model_client._last_call_ts.clear()
    await model_client._pace_calls("agent:1")
    t0 = asyncio.get_event_loop().time()
    await model_client._pace_calls("agent:1")
    assert asyncio.get_event_loop().time() - t0 >= 0.15
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/test_llm_pacing.py -q`
Expected: FAIL（`_last_call_ts` 是 float，没有 `.clear()`）

- [ ] **Step 3: 实现**

`_last_call_ts` 改为 `dict[str, float]`；`_pace_calls(key)` 按 key 读写；间隔从 `chat_llm_min_interval_ms/1000` 读取，默认 0.25；`call_model` 传 `pace_key=f"{agent_type or 'default'}:{agent_id or 0}"`。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/test_llm_pacing.py tests/unit/chat/test_chat_tool_loop.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/gen/model_client.py app/config.py tests/unit/test_llm_pacing.py
git commit -m "perf(chat): LLM 节流按 Agent/用户隔离并降低默认间隔"
```

---

### Task 6: system-reminder 调度

**Files:**
- Create: `core/chat/reminders.py`
- Create: `tests/unit/chat/test_chat_reminders.py`
- Modify: `core/tools/loop.py:395-451`

**Interfaces:**
- Produces:
  - `@dataclass Reminder(source: str, content: str)`
  - `class ReminderTracker`：`on_tool_round(round_no: int, signatures: list[str]) -> list[Reminder]`
  - `def wrap(reminder: Reminder) -> str`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.reminders import Reminder, ReminderTracker, wrap


def test_wrap_escapes_nested_tags():
    text = wrap(Reminder(source="x", content="a <system-reminder> b"))
    assert text.count("<system-reminder>") == 1
    assert "&lt;system-reminder>" in text


def test_repeat_reminder_fires_once_at_threshold():
    t = ReminderTracker(repeat_threshold=3)
    sig = ["search_cases::{}"]
    assert t.on_tool_round(1, sig) == []
    assert t.on_tool_round(2, sig) == []
    out = t.on_tool_round(3, sig)
    assert len(out) == 1 and "重复" in out[0].content
    assert t.on_tool_round(4, sig) == []


def test_todo_reminder_has_interval():
    t = ReminderTracker(todo_interval=3)
    fired = [len(t.on_tool_round(i, [])) for i in range(1, 10)]
    assert fired.count(1) >= 2
    assert fired[0] == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_reminders.py -q`
Expected: FAIL

- [ ] **Step 3: 实现并接入 `run_tool_loop`**

`wrap` 转义嵌套标签；`ReminderTracker` 维护计数、已提醒集合、上次 todo 轮次；每轮工具结果后调用，追加到最后一条 `role=tool` 消息末尾（不新增消息，保持 tool_call 配对）。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_reminders.py tests/unit/chat/test_chat_tool_loop.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/reminders.py core/tools/loop.py tests/unit/chat/test_chat_reminders.py
git commit -m "feat(chat): 新增 system-reminder 调度（重复调用与 todo 提醒）"
```

---

### Task 7: 压缩三件套

**Files:**
- Create: `core/chat/compact.py`
- Create: `tests/unit/chat/test_chat_compact.py`
- Modify: `core/tools/loop.py:253-282,449-451`
- Modify: `core/chat/context.py:176-207`

**Interfaces:**
- Produces:
  - `@dataclass CompactPolicy(context_window: int, max_output_tokens: int, buffer_tokens: int = 13000)`，属性 `threshold`
  - `def microcompact(messages: list[dict], *, keep_recent: int = 5, min_savings: int = 256) -> list[dict]`
  - `def select_for_summary(messages: list[dict], *, preserve_groups: int = 1) -> tuple[list[dict], list[dict]]`
  - `async def auto_compact(messages, llm_call, *, policy: CompactPolicy) -> tuple[list[dict], str | None]`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.compact import CompactPolicy, microcompact, select_for_summary


def test_policy_threshold_matches_zcode_formula():
    p = CompactPolicy(context_window=200_000, max_output_tokens=32_000)
    assert p.threshold == 166_000


def test_microcompact_clears_only_old_tool_results():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 2000})
    out = microcompact(msgs, keep_recent=2)
    assert out[-1]["content"] == msgs[-1]["content"]
    assert "已清理" in out[2]["content"]
    assert out[0]["content"] == "s"


def test_select_for_summary_preserves_recent_assistant_group():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "t1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "tool", "content": "t2"},
    ]
    older, recent = select_for_summary(msgs, preserve_groups=1)
    assert [m["content"] for m in recent] == ["u2", "a2", "t2"]
    assert len(older) == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_compact.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`threshold = context_window - min(max_output_tokens, 21000) - buffer_tokens`。
`microcompact`：保留最近 `keep_recent` 组工具结果，更早的替换为 `[Old tool result content cleared]`；节省不足 `min_savings` 则原样返回。
`select_for_summary`：按「assistant 消息 + 其后 tool 消息」分组，从尾部保留 `preserve_groups` 组。
`auto_compact`：调用 `context.summarize_history` 生成摘要。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_compact.py tests/unit/chat/test_chat_context.py tests/unit/chat/test_chat_tool_loop.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/compact.py core/chat/context.py core/tools/loop.py tests/unit/chat/test_chat_compact.py
git commit -m "feat(chat): 三层上下文压缩（microcompact/摘要/reactive 兜底）"
```

---

### Task 8: 分层系统提示词

**Files:**
- Modify: `core/chat/prompts.py`
- Modify: `core/chat/context.py:134-173`
- Modify: `tests/unit/chat/test_chat_context.py`

**Interfaces:**
- Produces:
  - `def stable_prompt() -> str`
  - `def dynamic_prompt(*, tools_md: str, plan_active: bool, memory_index: str = "") -> str`
  - `def build_system_messages(stable: str, dynamic: str) -> list[dict]`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.prompts import stable_prompt, dynamic_prompt, build_system_messages


def test_stable_prompt_excludes_tool_listing():
    text = stable_prompt()
    assert "【职责范围" in text
    assert "generate_cases" not in text


def test_dynamic_prompt_carries_tools_and_plan_state():
    text = dynamic_prompt(tools_md="- generate_cases：生成用例", plan_active=True, memory_index="m1")
    assert "generate_cases" in text
    assert "计划模式" in text
    assert "m1" in text


def test_build_system_messages_returns_two_blocks():
    msgs = build_system_messages("S", "D")
    assert [m["role"] for m in msgs] == ["system", "system"]
    assert msgs[0]["content"] == "S" and msgs[1]["content"] == "D"
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_context.py -q -k "stable_prompt or dynamic_prompt or system_messages"`
Expected: FAIL

- [ ] **Step 3: 实现并保持兼容**

`stable_prompt()` 为身份/职责/硬规则；`dynamic_prompt(...)` 为工具清单、Plan 状态、记忆索引；`CHAT_ASSISTANT_PROMPT` 保留为两者拼接，保证 DB 种子与旧调用不变。`build_llm_messages` 增加可选 `system_messages` 参数。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_context.py tests/unit/chat/test_chat_degrade.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/prompts.py core/chat/context.py tests/unit/chat/test_chat_context.py
git commit -m "refactor(chat): 系统提示词拆分为稳定段与动态段"
```

---

### Task 9: Plan mode（用户显式 + 助手建议）

**Files:**
- Create: `core/chat/plan_mode.py`
- Create: `tests/unit/chat/test_chat_plan_mode.py`
- Modify: `core/tools/builtin/actions.py`
- Modify: `core/chat/orchestrator.py`
- Modify: `app/config.py`

**Interfaces:**
- Produces:
  - `@dataclass PlanState(active: bool = False, phase: str = "understand", plan_text: str = "")`
  - `def enter_plan_mode(state: PlanState) -> PlanState`
  - `def exit_plan_mode(state: PlanState, plan: str) -> PlanState`
  - `def plan_reminder(state: PlanState) -> str | None`
  - 工具：`enter_plan_mode()`、`exit_plan_mode(plan: str)`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.plan_mode import PlanState, enter_plan_mode, exit_plan_mode, plan_reminder


def test_enter_plan_mode_sets_active_and_phase():
    s = enter_plan_mode(PlanState())
    assert s.active is True and s.phase == "understand"
    assert "理解" in plan_reminder(s)


def test_plan_reminder_none_when_inactive():
    assert plan_reminder(PlanState()) is None


def test_exit_plan_mode_returns_plan_and_deactivates():
    s = exit_plan_mode(PlanState(active=True, phase="approve"), "1. 查用例\n2. 执行")
    assert s.active is False
    assert "查用例" in s.plan_text


def test_plan_reminder_always_offers_exit_path():
    text = plan_reminder(PlanState(active=True, phase="design"))
    assert "exit_plan_mode" in text or "退出" in text
    assert "AskUserQuestion" in text or "exit_plan_mode" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_plan_mode.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

四阶段提示词：理解 → 设计 → 复查 → 批准；回合只能以 `AskUserQuestion` 或 `exit_plan_mode` 结束。
状态写入 `conversation.refs` 的 `{"type":"plan","active":true,"phase":"..."}`；`exit_plan_mode` 返回计划卡片。
助手对「多步、跨对象、需要先查后改」的任务主动建议（写入动态提示词规则），用户显式要求时直接进入。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_plan_mode.py tests/unit/chat/test_chat_tool_loop.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/plan_mode.py core/tools/builtin/actions.py core/chat/orchestrator.py app/config.py tests/unit/chat/test_chat_plan_mode.py
git commit -m "feat(chat): 新增 Plan mode 四阶段规划"
```

---

### Task 10: 只读子代理

**Files:**
- Create: `core/chat/subagent.py`
- Create: `tests/unit/chat/test_chat_subagent.py`
- Modify: `core/tools/builtin/actions.py`

**Interfaces:**
- Produces:
  - `@dataclass SubagentResult(status: str, content: str, tool_uses: int, duration_ms: int)`
  - `async def run_subagent(*, ctx, prompt: str, agent_type: str = "explore", max_rounds: int = 4, tool_allowlist: list[str] | None = None, llm=None) -> SubagentResult`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from core.chat.subagent import run_subagent


@pytest.mark.asyncio
async def test_subagent_uses_allowlist_and_returns_text(fake_ctx, fake_llm):
    result = await run_subagent(
        ctx=fake_ctx, prompt="查一下用例 1", agent_type="explore",
        tool_allowlist=["get_case"], llm=fake_llm,
    )
    assert result.status == "completed"
    assert "get_case" in result.content
    assert result.tool_uses >= 1


@pytest.mark.asyncio
async def test_subagent_respects_max_rounds(fake_ctx, looping_llm):
    result = await run_subagent(ctx=fake_ctx, prompt="x", max_rounds=2, llm=looping_llm)
    assert result.status in {"completed", "max_rounds"}


@pytest.mark.asyncio
async def test_subagent_rejects_write_tools(fake_ctx, fake_llm):
    result = await run_subagent(ctx=fake_ctx, prompt="x", tool_allowlist=["run_case"], llm=fake_llm)
    assert result.status == "rejected"
    assert "只读" in result.content
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_subagent.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

子代理使用独立 `messages` 与收窄后的 `specs`；`tool_allowlist` 只接受 `side_effect == "read"` 的工具，含写操作时直接 `rejected`。
调用现有 `run_tool_loop`；结果回传 `outcome.reply`，`tool_uses=len(outcome.actions_log)`。
`agent` 工具注册为 `side_effect="read"`。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_subagent.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/subagent.py core/tools/builtin/actions.py tests/unit/chat/test_chat_subagent.py
git commit -m "feat(chat): 新增只读隔离子代理与结果回传"
```

---

### Task 11: 文件记忆 + 管理界面

**Files:**
- Create: `core/chat/memory.py`
- Create: `app/services/chat_memory.py`
- Create: `app/routers/chat/memory.py`
- Create: `frontend/src/pages/settings/MemoryPanel.tsx`
- Create: `tests/unit/chat/test_chat_memory.py`
- Create: `tests/contract/test_chat_memory_api.py`
- Modify: `core/chat/orchestrator.py`
- Modify: `app/config.py`
- Modify: `app/routers/chat/__init__.py`

**Interfaces:**
- Produces:
  - `@dataclass MemoryEntry(name: str, description: str, type: str, body: str)`
  - `def memory_root(project_id: int | None) -> Path`
  - `def load_index(project_id: int | None) -> str`
  - `def recall(project_id: int | None, query: str, *, limit: int = 5) -> list[MemoryEntry]`
  - `def save_memory(project_id: int | None, entry: MemoryEntry) -> Path`
  - `def delete_memory(project_id: int | None, name: str) -> bool`
  - `GET /api/chat/memory`、`DELETE /api/chat/memory/{name}`

- [ ] **Step 1: 写失败测试**

```python
from core.chat.memory import MemoryEntry, memory_root, save_memory, load_index, recall, delete_memory


def test_save_and_index_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)
    save_memory(7, MemoryEntry(name="run-habit", description="用户偏好先跑冒烟", type="feedback", body="先跑 smoke"))
    index = load_index(7)
    assert "run-habit" in index and "用户偏好先跑冒烟" in index
    assert (memory_root(7) / "run-habit.md").exists()


def test_recall_scores_by_keyword(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)
    save_memory(1, MemoryEntry(name="a", description="环境不可达排查", type="project", body="检查 agent 在线"))
    save_memory(1, MemoryEntry(name="b", description="用例命名规范", type="project", body="中文标题"))
    hits = recall(1, "环境不可达怎么排查", limit=2)
    assert hits and hits[0].name == "a"


def test_delete_memory_removes_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)
    save_memory(2, MemoryEntry(name="x", description="d", type="user", body="b"))
    assert delete_memory(2, "x") is True
    assert delete_memory(2, "x") is False
```

契约测试覆盖：未登录 401、非项目成员 403、列表返回条目与写入时间、删除不存在条目 404。

- [ ] **Step 2: 运行确认失败**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_memory.py tests/contract/test_chat_memory_api.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`_ROOT = Path("data/memory")`；每条记忆一个 `.md`，frontmatter 含 `name/description/type/written_at`；索引上限 200 行。
`recall` 用关键词计数打分（中文 2 字滑窗），返回前 `limit` 条。
API 复用现有对话权限校验；`DELETE` 不存在返回 404。
设置页新增「项目记忆」面板：列表展示名称、描述、类型、时间，带删除按钮与二次确认。

- [ ] **Step 4: 运行确认通过**

Run: `venv/bin/python -m pytest tests/unit/chat/test_chat_memory.py tests/contract/test_chat_memory_api.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/chat/memory.py app/services/chat_memory.py app/routers/chat/memory.py frontend/src/pages/settings/MemoryPanel.tsx core/chat/orchestrator.py app/config.py tests/unit/chat/test_chat_memory.py tests/contract/test_chat_memory_api.py
git commit -m "feat(chat): 项目文件记忆与管理界面"
```

---

### Task 12: 全量回归与开关验证

**Files:**
- Create: `tests/unit/chat/test_chat_flags.py`
- 验证：所有开关关闭时行为与改动前一致

- [ ] **Step 1: 写开关回归测试**

```python
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [
    "chat_compact_enabled", "chat_plan_mode_enabled",
    "chat_subagent_enabled", "chat_memory_enabled",
])
async def test_feature_flags_off_restores_legacy(patch_settings, flag):
    setattr(patch_settings, flag, False)
    # 构造一轮对话，断言不注入对应内容、工具清单不含新工具
```

- [ ] **Step 2: 运行全量单测**

Run: `venv/bin/python -m pytest tests/unit/ -q`
Expected: 全部 passed

- [ ] **Step 3: 运行契约测试**

Run: `venv/bin/python -m pytest tests/contract/ -q`
Expected: 全部 passed

- [ ] **Step 4: 前端类型检查与单测**

Run: `cd frontend && npx tsc --noEmit && npx vitest run`
Expected: 0 error，全部 passed

- [ ] **Step 5: 提交**

```bash
git add tests/ core/ app/ frontend/
git commit -m "test(chat): ZCode 机制复刻的开关回归与全量验证"
```

---

## 分阶段交付建议

- **第一批（Task 1-5）**：P0，低风险高收益，可独立发布。
- **第二批（Task 6-8）**：P1，架构层，重点回归 `test_chat_context.py`、`test_chat_degrade.py`。
- **第三批（Task 9-11）**：P2，新能力，每个任务单独提交。
- **Task 12** 每批结束都跑一次。

## 与 ZCode 的刻意差异

| ZCode 做法 | 本项目取舍 | 原因 |
|---|---|---|
| `cacheControl: ephemeral` 标记稳定前缀 | 只分段，不传 cache_control | 现有网关为 OpenAI 兼容，传该字段可能 400 |
| 子代理共享 event store、后台任务通知 | 前台同步子代理 | 先降低复杂度，后台模式留待后续 |
| 完整 expert workflow DAG + critic 闭环 | 只做 Plan mode 四阶段 | 测试平台的叶子任务粒度不需要 DAG |
| Dream 每日记忆整理 | 不实现 | 投入产出比低，先做召回 |
| microcompact 默认关闭 | 默认开启但阈值保守 | 本项目上下文预算本就偏紧 |
| 子代理可用全部工具 | 仅只读工具 | spec FR-011 明确约束，写操作交回主对话 |
