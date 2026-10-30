---
description: "Task list for 智能体对话能力增强"
---

# Tasks: 智能体对话能力增强

**Input**: Design documents from `specs/044-chat-intelligence/`
**Prerequisites**: plan.md（必需）、spec.md（用户故事）、research.md、data-model.md、contracts/

**Tests**: 本特性要求 TDD（用户明确「自动测试为主」），因此每个实现任务前都有对应的测试任务。

**Organization**: 任务按用户故事分组，每个故事可独立实现、独立验证。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可并行（不同文件、无未完成依赖）
- **[Story]**: 所属用户故事（US1-US5）
- 所有任务包含精确文件路径

## Path Conventions

本项目为 Web 应用：后端 `core/` + `app/`，前端 `frontend/src/`，测试 `tests/`。

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: 新增配置项，作为所有故事的开关基础

- [x] T001 在 `app/config.py` 的 chat 段新增 9 个配置项：`chat_context_max_tokens=0`、`chat_llm_min_interval_ms=250`、`chat_tool_result_replay_max=5`、`chat_tool_replay_max_chars=1200`、`chat_reminder_repeat_threshold=3`、`chat_compact_enabled=True`、`chat_plan_mode_enabled=True`、`chat_subagent_enabled=True`、`chat_memory_enabled=True`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: 所有故事共享的节流修复；当前全局 3 秒节流会拖慢每一个用户故事

**⚠️ CRITICAL**: 本阶段完成后才能开始用户故事

- [x] T002 [P] 写节流隔离失败测试 in `tests/unit/test_llm_pacing.py`（不同 key 不互相等待、同 key 按间隔等待）
- [x] T003 实现按 key 隔离的 `_pace_calls(key)` in `app/gen/model_client.py`，`_last_call_ts` 改为 `dict[str, float]`

**Checkpoint**: 节流不再阻塞多用户，后续故事可并行开发

---

## Phase 3: User Story 1 - 长对话保持上下文连续性 (Priority: P1) 🎯 MVP

**Goal**: 按模型窗口分配预算、工具证据回注、三层压缩，让长对话不丢事实

**Independent Test**: 30 轮对话后追问上一轮工具结果，助手不重复调用工具即可回答

### Tests for User Story 1

- [x] T004 [P] [US1] 写预算测试 in `tests/unit/chat/test_chat_budget.py`（估算、70% 窗口、保留最新）
- [x] T005 [P] [US1] 写摘要合并测试 in `tests/unit/chat/test_chat_context.py`（超限保留最新 chunk）
- [x] T006 [P] [US1] 写工具事件回注测试 in `tests/unit/chat/test_chat_context.py`（渲染、上限、可关闭）
- [x] T007 [P] [US1] 写压缩测试 in `tests/unit/chat/test_chat_compact.py`（阈值公式、只清旧结果、保留最近 assistant 组）
- [x] T008 [P] [US1] 写分层提示词测试 in `tests/unit/chat/test_chat_context.py`（稳定段无工具清单、动态段含工具与计划状态）

### Implementation for User Story 1

- [x] T009 [US1] 实现 `core/chat/budget.py`：`estimate_tokens`、`estimate_messages_tokens`、`resolve_context_budget_tokens`、`fit_to_budget`
- [x] T010 [US1] 在 `core/chat/orchestrator.py` 实现 `_merge_summary` 并替换第 153 行的 `[:8000]` 截断；`_maybe_update_summary` 改用 token 预算
- [x] T011 [US1] 在 `core/chat/context.py` 实现 `tool_events_digest` 并接入 `message_text`
- [x] T012 [US1] 实现 `core/chat/compact.py`（`CompactPolicy`、`microcompact`、`select_for_summary`、`auto_compact`）
- [x] T013 [US1] 在 `core/tools/loop.py` 用 `microcompact` 替换 `_compact_tool_messages`，并用 `fit_to_budget` 作为兜底
- [x] T014 [US1] 在 `core/chat/prompts.py` 拆分 `stable_prompt` / `dynamic_prompt` / `build_system_messages`，保持 `CHAT_ASSISTANT_PROMPT` 向后兼容
- [x] T015 [US1] 在 `core/chat/context.py` 的 `build_llm_messages` 支持 `system_messages` 参数

**Checkpoint**: US1 可独立验证——长对话不丢工具证据，压缩保留最近进展

---

## Phase 4: User Story 2 - 复杂任务先规划再执行 (Priority: P2)

**Goal**: 计划模式四阶段，用户显式触发或助手对复杂任务建议

**Independent Test**: 提出多步任务，助手输出分步计划并等待批准

### Tests for User Story 2

- [x] T016 [P] [US2] 写计划模式测试 in `tests/unit/chat/test_chat_plan_mode.py`（进入/退出、提醒含退出路径）

### Implementation for User Story 2

- [x] T017 [US2] 实现 `core/chat/plan_mode.py`：`PlanState`、`enter_plan_mode`、`exit_plan_mode`、`plan_reminder`
- [x] T018 [US2] 在 `core/tools/builtin/actions.py` 注册 `enter_plan_mode` / `exit_plan_mode` 工具（side_effect=read）
- [x] T019 [US2] 在 `core/chat/orchestrator.py` 注入计划提醒（间隔 5 轮），状态读写 `conversation.refs`
- [x] T020 [US2] 在 `core/chat/prompts.py` 的 `dynamic_prompt` 增加计划模式规则与「复杂任务主动建议」判据

**Checkpoint**: US2 可独立验证——计划模式下不执行写操作，批准后才开始

---

## Phase 5: User Story 3 - 工具失败后能自我修复 (Priority: P2)

**Goal**: 结构化失败反馈 + 重复调用提醒

**Independent Test**: 构造缺参调用，助手下一轮补齐并成功

### Tests for User Story 3

- [x] T021 [P] [US3] 写工具反馈测试 in `tests/unit/tools/test_tool_feedback.py`（错误包装、校验逐条、截断恢复指引）
- [x] T022 [P] [US3] 写提醒测试 in `tests/unit/chat/test_chat_reminders.py`（转义、阈值只触发一次、todo 间隔）

### Implementation for User Story 3

- [x] T023 [US3] 实现 `core/tools/feedback.py`：`format_tool_error`、`format_validation_error`、`tool_message_content`
- [x] T024 [US3] 在 `core/tools/loop.py` 用 `tool_message_content` 替换 `_tool_message_content`，参数解析失败走 `format_validation_error`
- [x] T025 [US3] 实现 `core/chat/reminders.py`：`Reminder`、`ReminderTracker`、`wrap`
- [x] T026 [US3] 在 `core/tools/loop.py` 每轮工具结果后调用 `ReminderTracker`，提醒追加到最后一条 tool 消息尾部

**Checkpoint**: US3 可独立验证——错误可被模型理解并自修

---

## Phase 6: User Story 4 - 跨会话记住项目约定 (Priority: P3)

**Goal**: 文件型项目记忆 + 相关性召回 + 设置页管理

**Independent Test**: 对话 A 确立偏好，对话 B 生效；设置页可删除后不再召回

### Tests for User Story 4

- [x] T027 [P] [US4] 写记忆单测 in `tests/unit/chat/test_chat_memory.py`（保存/索引/召回/删除）
- [x] T028 [P] [US4] 写记忆接口契约测试 in `tests/contract/test_chat_memory_api.py`（401/403/404、列表、删除）

### Implementation for User Story 4

- [x] T029 [US4] 实现 `core/chat/memory.py`：`MemoryEntry`、`memory_root`、`load_index`、`recall`、`save_memory`、`delete_memory`
- [x] T030 [US4] 实现 `app/services/chat_memory.py` 与 `app/routers/chat/memory.py`：`GET /api/chat/memory`、`DELETE /api/chat/memory/{name}`
- [x] T031 [US4] 在 `app/routers/chat/__init__.py` 挂载记忆路由
- [x] T032 [US4] 在 `core/chat/orchestrator.py` 注入记忆索引与 `recall` 结果到动态 system 段
- [x] T033 [P] [US4] 实现 `frontend/src/pages/settings/MemoryPanel.tsx`：列表、删除、二次确认

**Checkpoint**: US4 可独立验证——跨对话生效，可在设置页管理

---

## Phase 7: User Story 5 - 大范围查询不污染主对话 (Priority: P3)

**Goal**: 只读隔离子代理，主对话只收结论

**Independent Test**: 大范围检索时主对话只出现结论，子代理拒绝写操作

### Tests for User Story 5

- [x] T034 [P] [US5] 写子代理测试 in `tests/unit/chat/test_chat_subagent.py`（白名单、轮次上限、拒绝写工具）

### Implementation for User Story 5

- [x] T035 [US5] 实现 `core/chat/subagent.py`：`SubagentResult`、`run_subagent`（仅接受 `side_effect=read`）
- [x] T036 [US5] 在 `core/tools/builtin/actions.py` 注册 `agent` 工具（side_effect=read）

**Checkpoint**: US5 可独立验证——明细不外泄到主对话

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: 开关回归、全量验证、文档

- [x] T037 [P] 写开关回归测试 in `tests/unit/chat/test_chat_flags.py`（四个开关关闭后工具清单与提示词恢复原状）
- [x] T038 运行 `venv/bin/python -m pytest tests/unit/ -q` 并修复全部失败
- [x] T039 运行 `venv/bin/python -m pytest tests/contract/ -q` 并修复全部失败
- [x] T040 运行 `cd frontend && npx tsc --noEmit && npx vitest run`
- [x] T041 按 `specs/044-chat-intelligence/quickstart.md` 的 S1-S4 做人工验证并回填结果

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: 无依赖，可立即开始
- **Foundational (Phase 2)**: 依赖 Setup，阻塞所有用户故事
- **User Stories (Phase 3-7)**: 依赖 Foundational；故事之间可并行
- **Polish (Phase 8)**: 依赖所需故事完成

### User Story Dependencies

| 故事 | 依赖 | 说明 |
|---|---|---|
| US1 (P1) | Foundational | 无 |
| US2 (P2) | Foundational、US1 的 `dynamic_prompt` | 计划模式需要动态提示词承载规则 |
| US3 (P2) | Foundational | 无 |
| US4 (P3) | Foundational、US1 的 `dynamic_prompt` | 记忆索引注入动态段 |
| US5 (P3) | Foundational、US3 的工具反馈 | 子代理错误要用统一格式回传 |

### Within Each User Story

- 测试先写并确认失败
- 纯函数模块先于接入点
- 接入点先于 UI

### Parallel Opportunities

- T002 与 T001 可并行
- US1 内部 T004-T008 五个测试任务全部可并行
- US3 的 T021/T022 可并行
- US4 的 T027/T028 可并行；T033 前端可与后端并行
- US2、US3 在 US1 的 `dynamic_prompt`（T014）完成后可与 US1 剩余任务并行

---

## Parallel Example: User Story 1

```bash
# 五个测试文件互不冲突，可同时启动
Task: "写预算测试 in tests/unit/chat/test_chat_budget.py"
Task: "写摘要合并测试 in tests/unit/chat/test_chat_context.py"
Task: "写工具事件回注测试 in tests/unit/chat/test_chat_context.py"
Task: "写压缩测试 in tests/unit/chat/test_chat_compact.py"
Task: "写分层提示词测试 in tests/unit/chat/test_chat_context.py"
```

注意：T005/T006/T008 都改 `tests/unit/chat/test_chat_context.py`，实际执行时需串行或分文件拆分。

---

## Implementation Strategy

### MVP First（仅 US1）

1. 完成 Phase 1 + Phase 2
2. 完成 Phase 3（US1）
3. **停止并验证**：30 轮对话追问工具结果
4. 可先发布 P0 收益

### Incremental Delivery

1. Setup + Foundational → 基础就绪
2. US1 → 验证长对话 → 交付
3. US3 → 验证工具自修 → 交付
4. US2 → 验证计划模式 → 交付
5. US4 → 验证记忆 → 交付
6. US5 → 验证子查询隔离 → 交付
7. Polish 全量回归

---

## Notes

- [P] 任务 = 不同文件、无未完成依赖
- 每个实现任务内部仍按 TDD：测试先失败再实现
- 每个任务或逻辑组后提交
- 关闭开关后行为必须与增强前一致，这是合并门槛
