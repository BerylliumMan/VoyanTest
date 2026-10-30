# Research: 智能体对话能力增强

**Feature**: 044-chat-intelligence
**Date**: 2026-10-29
**Input**: [spec.md](./spec.md)

## 决策清单

### D1: token 估算方式

**Decision**: 本地启发式（ASCII 4 字符 ≈ 1 token，CJK 1 字符 ≈ 1 token）
**Rationale**: 只需相对预算，不需要精确计费；避免引入 tiktoken 与模型词表绑定；估算偏差由 70% 预算余量吸收
**Alternatives considered**:
- tiktoken：精确但对非 OpenAI 模型不准，且新增依赖
- 直接按字符：中文与英文比例失真，正是当前 24000 字符预算的问题根源

### D2: 上下文预算比例

**Decision**: 默认取模型上下文窗口的 70%，并允许 `chat_context_max_tokens` 覆盖
**Rationale**: 输入+输出共享窗口；输出侧预留 16384；70% 给输入留出工具结果增长空间
**Alternatives considered**:
- 83%（ZCode 的阈值）：对 131072 窗口会留下过少输出空间
- 50%：过于保守，仍会让长对话频繁压缩

### D3: 摘要保留边界

**Decision**: 按「assistant 消息 + 其后的 tool 消息」分组，至少保留最近 1 组
**Rationale**: 工具调用与结果必须成对保留，否则模型会重复调用或看到孤儿结果
**Alternatives considered**:
- 固定保留 N 条消息：可能切断 tool_call/tool_result 配对
- 只保留最近 1 条：丢失当前轮的工作证据

### D4: 记忆存储介质

**Decision**: 项目目录下的 Markdown 文件（`data/memory/project-{id}/`），frontmatter + 正文
**Rationale**: 不新增表、不引入向量库；可读可编辑可备份；检索用关键词打分足够
**Alternatives considered**:
- 新增数据库表：违反本项目「不新增表」约定，且迁移成本高
- 向量检索：需要 embedding 服务与向量存储，超出本期范围

### D5: 计划状态存储

**Decision**: 复用 `ChatConversation.refs` 中的 `{"type":"plan",...}` 条目
**Rationale**: refs 已是 JSON 列，读写现成；避免迁移
**Alternatives considered**:
- 新增 `chat_plans` 表：数据模型更干净，但违反本期不新增表约束
- 写入 `summary` 前缀标记：会污染摘要文本，解析脆弱

### D6: 子代理执行模式

**Decision**: 前台同步执行，只读工具白名单
**Rationale**: 不引入后台任务生命周期与事件镜像；主对话等待结果，交互简单
**Alternatives considered**:
- 后台异步：ZCode 的做法，但需要任务通知、输出文件、取消恢复，工作量翻倍
- 全工具权限：风险高，spec FR-011 已排除

### D7: LLM 节流隔离

**Decision**: 按 `agent_type:agent_id` 维护独立时间戳，默认间隔 250ms
**Rationale**: 原全局 3s 会让 8 轮循环多等 24s；按 key 隔离后不同用户互不阻塞
**Alternatives considered**:
- 完全去掉节流：网关 RPM 限制仍可能触发 429
- 保留全局 3s：多用户并发时体验不可接受

### D8: 系统提示词分段

**Decision**: 稳定段（身份/职责/硬规则）与动态段（工具清单/Plan/记忆索引）分成两条 system 消息，不传 `cache_control`
**Rationale**: OpenAI 兼容网关对 `cache_control` 支持不一致，传了可能 400；分段本身已利于缓存命中
**Alternatives considered**:
- 传 Anthropic `cacheControl`：仅 Anthropic 首方可用
- 只保留单段：动态信息每次变化会破坏整个前缀缓存

### D9: 工具事件回注粒度

**Decision**: 每条历史消息最多回注 5 条工具事件，总长 1200 字符
**Rationale**: 覆盖「上一轮查到了什么」的核心需求，同时不让历史膨胀
**Alternatives considered**:
- 全量回注：上下文迅速耗尽
- 不回注：当前问题，模型反复查询

### D10: 压缩触发阈值

**Decision**: `context_window - min(max_output, 21000) - 13000`
**Rationale**: 沿用 ZCode 经生产验证的公式；输出侧预留与实际 provider 行为一致
**Alternatives considered**:
- 固定字符阈值：与模型窗口脱钩，正是当前问题

## 未决事项

无。spec 中的 Assumptions 已覆盖剩余细节。
