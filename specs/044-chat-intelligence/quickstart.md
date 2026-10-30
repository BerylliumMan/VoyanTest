# Quickstart: 智能体对话能力增强

**Feature**: 044-chat-intelligence
**Date**: 2026-10-29

## 前置

- 已完成 `apps` 依赖安装，后端可启动。
- 已配置可用的 AI 网关（Settings → AI 配置）。

## S1. P0 验证（Task 1-5）

### S1.1 上下文预算

1. 新建对话，连续发送 20 条较长的用例描述。
2. 观察后端日志中的预算值：应接近 `模型窗口 × 70%`，而不是 24000 字符。
3. 第 21 条消息应正常发送，不出现"上下文超限"。

**通过标准**: 长对话不再因 24000 字符上限提前截断。

### S1.2 工具结果回注

1. 问「列出当前项目的用例」→ 助手调用 `search_cases`。
2. 下一轮问「刚才第 3 条是什么」。
3. 助手应直接回答，不再调用 `search_cases`。

**通过标准**: 第二轮没有重复的工具调用。

### S1.3 结构化失败

1. 构造一次缺少必填参数的调用（例如让助手执行一个不存在的环境 id）。
2. 观察工具返回：应包含 `<tool_use_error>` 与修复建议。
3. 助手应补齐参数并重试成功。

### S1.4 节流

1. 两个浏览器会话同时发起对话。
2. 观察两边的首条流式响应时间。
3. 不应出现一方明显等待另一方 3 秒的情况。

## S2. P1 验证（Task 6-8）

### S2.1 重复调用提醒

1. 让助手对同一对象连续查询同一工具 3 次（可通过反复要求"再查一次"）。
2. 第 3 次工具结果后应出现 `[治理提示]`，且第 4 次不再重复提醒。

### S2.2 压缩

1. 在长对话中继续追加消息直到接近预算。
2. 观察较早的工具结果变为 `[Old tool result content cleared]`，最近 5 组保持完整。
3. 最近 1 个 assistant 组（含工具结果）原文保留。

### S2.3 分层提示词

1. 打开任意对话，检查后端组装的 messages。
2. 应存在两条 `system` 消息：第一条不含工具清单，第二条含工具清单与运行状态。

## S3. P2 验证（Task 9-11）

### S3.1 计划模式

1. 发送「计划一下：找出最近失败的 5 条用例并分析原因」。
2. 助手应进入计划模式并输出分步计划，请求确认。
3. 回复「同意」后开始执行；回复「改一下」应修订计划。

### S3.2 子代理

1. 发送「在全部用例里找所有跟登录相关的」。
2. 主对话只出现结论与来源摘要，不出现几百条原始记录。
3. 子代理不可写：要求子代理直接删除用例时，应返回"只读"说明。

### S3.3 记忆

1. 在对话 A 中说「记住：执行前先跑冒烟用例」。
2. 打开设置页 → 项目记忆，应看到该条，包含来源与时间。
3. 新建对话 B，提出执行任务，助手应主动先跑冒烟。
4. 在设置页删除该条，对话 B 下一轮不再应用。

## S4. 开关回归

1. 在 `app/config.py` 或环境变量中关闭 `chat_compact_enabled`、`chat_plan_mode_enabled`、`chat_subagent_enabled`、`chat_memory_enabled`。
2. 重启服务。
3. 对话应回到增强前行为：工具清单不含 `agent` / `enter_plan_mode` / `exit_plan_mode`，不注入记忆，不执行 microcompact。
4. 运行 `venv/bin/python -m pytest tests/unit/ tests/contract/ -q`，应全部通过。

## 自动化验证

```bash
# 单测
venv/bin/python -m pytest tests/unit/ -q

# 契约
venv/bin/python -m pytest tests/contract/ -q

# 前端
cd frontend && npx tsc --noEmit && npx vitest run
```

---

## 验证证据（2026-10-30，机制级端到端）

> 真实模型网关本次不可用（`authorization failed`, code 11200），以下验证使用本地 OpenAI 兼容桩服务（`/tmp/mock_llm.py`，端口 8020）驱动完整对话链路。桩服务只决定“调哪个工具/回什么话”，所有被测机制仍由产品代码执行。

| 场景 | 证据 | 结果 |
|---|---|---|
| S1.2 工具循环 + 结果回注 | 第 1 轮 assistant 调 `list_projects`；第 2 轮请求 messages 中出现 `【上一轮工具结果】`（桩日志 `/tmp/mock-llm-requests.jsonl` 第 3 条） | 通过 |
| S1.3 结构化失败 | `run_case` 传非法 id → 工具状态 `failed`，tool 消息含 `<tool_use_error>` | 通过 |
| S2.1 重复调用提醒 | 单轮内连续 3 次 `list_projects` → 第 3 次 tool 消息尾部出现 `<system-reminder>相同调用…重复 3 次…</system-reminder>`，且只出现一次 | 通过 |
| S2.3 分层提示词 | 请求中 system 消息为 2 条（稳定段 + 动态段），动态段含工具清单 | 通过 |
| S3.1 计划模式 | `enter_plan_mode` 工具 ok，`conversation.refs` 出现 `{"type":"plan","active":true,"phase":"understand"}`；后续轮 system 含“计划模式 — 当前激活”与 `exit_plan_mode` 退出路径 | 通过 |
| S3.2 子代理 | `agent` 工具 ok；子代理请求使用独立单条 system（`SUBAGENT_PROMPT`），主对话只收到结论 | 通过（前台同步版） |
| S3.3 记忆 | `save_memory` 写入 `run-habit`；`GET /api/chat/memory` 返回该条；新对话 system 含“项目记忆索引”与 `run-habit` | 通过 |
| S1.1 上下文预算 | 自动化：`tests/unit/chat/test_chat_budget.py` 4 项 | 通过（未做端到端观察） |
| S1.4 节流隔离 | 自动化：`tests/unit/test_llm_pacing.py` 2 项 | 通过 |
| S4 开关回归 | 自动化：`tests/unit/chat/test_chat_flags.py` 5 项 | 通过 |

### 本次验证中发现并修复的问题

1. **记忆只读不写**：`FR-012` 要求用户可显式要求记住，但缺少写入工具 → 新增 `save_memory` 工具。
2. **分层提示词未接入编排层**：`stable_prompt`/`dynamic_prompt` 已实现但 `_build_llm_messages` 未使用 → 已接入，仅对内置提示词生效（自定义模板保持单条）。
3. **计划提醒破坏分层**：提醒拼进 `system_prompt` 导致分层判断失效、退回单条 system → 改为与其他上下文块一样追加到最后一条 system。

### 未验证项

- 真实模型下的回答质量与工具选择准确性（网关 key 失效）。
- 计划模式的阶段推进（`phase` 目前停在 `understand`，未实现自动推进）。

---

## 验证证据（2026-10-30，真实模型）

> 网关：`https://opencode.ai/zen/go/v1`，模型 `deepseek-v4.1-flash`（需 `x-opencode-session` 头，项目已自动注入）。验证实例端口 8010，复用生产 postgres 库；验证后测试对话与记忆文件已清理。

| 场景 | 实测 | 结果 |
|---|---|---|
| 工具调用 | 首轮 `list_projects` → 正确列出项目并给 id | 通过 |
| 历史结果回注 | 第 2 轮追问“第一个项目叫什么”，**未重复调用工具**，直接答 “Final Test (id=1)” | 通过（SC-001） |
| 计划模式 | 模型主动 `enter_plan_mode` → 只读探索（list_batches/count_cases/run_script）→ `exit_plan_mode` 提交分步计划并等待批准；`refs.plan_text` 完整落库 | 通过（SC-006） |
| 记忆写入 | 用户说“记住：执行前先跑冒烟用例集” → 模型调 `save_memory`，写入 `smoke-before-run`，API 可查 | 通过 |
| 失败自修 | 要求执行不存在的用例 → `get_case` 失败 → 模型自主改用 `list_projects`/`search_cases`/`count_cases` 核查 → 诚实说明“用例不存在”并给替代方案，未编造 | 通过（SC-004 方向） |
| 结构化失败反馈 | 失败工具结果以 `<tool_use_error>` 回填，模型能理解并换策略 | 通过 |

### 真实模型下的观察

- 单轮工具编排合理：只读工具连续调用（`list_batches` ×2、`count_cases`、`run_script`），未出现重复死循环。
- 计划模式产出的是可执行的分步计划（含前置确认、失败清单、归因、闭环验证），不是空泛复述。
- 模型会主动指出“当前项目没有数据”，没有为了完成任务而编造批次或失败原因。
- 单轮耗时 4.5s–46s（取决于工具数量），计划模式最慢。

### 仍未覆盖

- 计划模式的 `phase` 自动推进（目前停在首阶段，由模型自行调用 `exit_plan_mode` 退出）。
- 长对话（30+ 轮）下的压缩触发端到端观察（逻辑由单测覆盖）。
