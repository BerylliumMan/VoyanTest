# Data Model: 智能体对话能力增强

**Feature**: 044-chat-intelligence
**Date**: 2026-10-29

## 1. 记忆条目（MemoryEntry）

**存储**: `data/memory/project-{project_id}/<name>.md`（文件，非数据库表）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 稳定标识，文件名字母数字与连字符，唯一 |
| `description` | string | 是 | 一句话描述，用于索引与召回打分 |
| `type` | enum | 是 | `user` / `feedback` / `project` / `reference` |
| `body` | string | 是 | 正文，Markdown |
| `written_at` | ISO8601 | 是 | 写入时间，frontmatter 记录 |

**规则**:
- 同一项目内 `name` 唯一；重复写入视为更新。
- 不记录：一次性任务细节、可从代码/配置推导的事实、已在项目文档中的内容。
- 单条正文上限 8KB；索引最多 200 行。
- 删除即删除文件；索引随之更新。

**文件格式**:

```markdown
---
name: run-habit
description: 用户偏好先跑冒烟用例
type: feedback
written_at: 2026-10-29T16:00:00+08:00
---
先执行 smoke 用例集，再执行全量。
```

## 2. 计划状态（PlanState）

**存储**: `ChatConversation.refs` JSON 中的一条
`{"type": "plan", "active": true, "phase": "understand", "plan_text": "..."}`

| 字段 | 类型 | 说明 |
|---|---|---|
| `active` | bool | 是否处于计划模式 |
| `phase` | enum | `understand` / `design` / `review` / `approve` |
| `plan_text` | string | 退出计划模式时的计划正文 |

**状态流转**:

```text
inactive --enter_plan_mode--> understand --> design --> review --> approve --exit_plan_mode--> inactive
                                   ^                     |
                                   +------ 用户要求修改 ---+
```

**规则**:
- 计划模式下只读工具可用，写操作工具不派发。
- 每个回合必须以 `AskUserQuestion` 或 `exit_plan_mode` 结束。
- 退出后 `plan_text` 保留在 refs 中供审计，图标变为 inactive。

## 3. 工具事件摘要（ToolEventDigest）

**来源**: `ChatMessage.actions`（已有 JSON 列，元素 `type=tool`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool` | string | 工具名 |
| `status` | string | `ok` / `failed` |
| `summary` | string | 结果摘要，落库时已截到 300 字符 |

**回注规则**:
- 每条历史消息最多 5 条，总长 1200 字符。
- 只回注摘要，不回注原始 `data`。
- 与 `cards_digest` 并列，不互相替代。

## 4. 对话预算与压缩策略（CompactPolicy）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `context_window` | 模型配置的 `max_context_tokens`（默认 131072） | 输入+输出共享窗口 |
| `max_output_tokens` | `min(window // 3, 16384)` | 输出预留 |
| `buffer_tokens` | 13000 | 安全余量 |
| `threshold` | `window - min(output, 21000) - buffer` | 触发压缩的输入 token 数 |

**压缩层级**:
1. `microcompact`：清理旧工具结果内容，保留最近 5 组。
2. `auto_compact`：较早消息摘要化，保留最近 1 个 assistant 组。
3. `fit_to_budget`：最后兜底，从最早消息起截断。

## 5. 配置项（新增）

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `chat_context_max_tokens` | int | 0 | 0 表示按窗口 70% 自动计算 |
| `chat_llm_min_interval_ms` | int | 250 | 同 key 最小调用间隔 |
| `chat_tool_result_replay_max` | int | 5 | 历史工具事件回注条数 |
| `chat_tool_replay_max_chars` | int | 1200 | 回注总字符上限 |
| `chat_reminder_repeat_threshold` | int | 3 | 重复调用提醒阈值 |
| `chat_compact_enabled` | bool | true | 压缩总开关 |
| `chat_plan_mode_enabled` | bool | true | 计划模式开关 |
| `chat_subagent_enabled` | bool | true | 子代理开关 |
| `chat_memory_enabled` | bool | true | 记忆开关 |

## 关系图

```text
ChatConversation 1──N ChatMessage
       │
       ├── refs[plan] ── PlanState
       ├── summary ── 摘要文本
       └── project_id ── MemoryEntry N（文件系统，非外键）
```
