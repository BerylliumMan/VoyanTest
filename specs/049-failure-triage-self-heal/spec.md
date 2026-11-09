# Feature Specification: 失败分类与自愈

**Feature Branch**: `049-failure-triage-self-heal`
**Created**: 2026-11-04
**Status**: Draft
**Input**: User description: "失败分类与自愈：环境/数据类失败自动重试、只通知真实缺陷。"

## 背景

047/048 已把「代码变更 → 自动补用例 → 自动执行」这条链路打通，但**失败之后仍然要人逐条看**：

1. 批次结束只有一个「失败 N 条」，断言不成立、元素找不到、接口 5xx、token 过期混在一起，人得点开每条报告才知道该不该管。
2. 同一类抖动（网络超时、服务重启）会稳定复现，但平台只会原样失败，不会自己再试一次；031 的「仅失败重跑」要人手点。
3. 通知对任何失败都发 `error`，环境抖动和真实缺陷一个优先级，通知中心逐渐被训练成"不用看"。

平台已有可复用的基础：报告 JSON 里有每步的 `status/error/assertions`；031 有手动「仅失败重跑」与 `parent_batch_id`；028/037 有执行中的 UI 定位自愈；041 有 LLM 缺陷报告（人工触发）。缺的是**把失败先分好类，再按类别决定重试与通知**。

本特性不改变"失败"的判定（status 仍由执行器决定），只回答三个问题：这条失败是什么性质？要不要自动再跑一次？该不该叫人？

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 每条失败带类别，报告能直接看出性质 (Priority: P1)

执行结束后，每条失败记录被归到一个类别：`env`（网络/超时/5xx/服务不可用）、`auth`（401/403/凭证失效）、`data`（目标或前置数据不存在）、`assertion`（断言不成立）、`locator`（元素定位失败）、`script`（变量/表达式/用例配置错误）、`unknown`。报告页与详情展示类别与一句原因。

**Why this priority**: 没有类别，后面两件事（重试、通知）都无从谈起；这是本特性的地基。

**Independent Test**: 用固定样本（5xx、超时、401、断言失败、元素找不到、变量缺失、404）各跑一条用例，检查每条 `test_runs.failure_kind` 与报告页标签是否与预期一致。

**Acceptance Scenarios**:

1. **Given** 接口返回 502 且无断言失败，**When** 批次结束，**Then** 该运行 `failure_kind=env`。
2. **Given** 用例断言 `status_code` 期望 200 实际 200 但结构断言字段缺失，**When** 结束，**Then** `failure_kind=assertion`。
3. **Given** UI 步骤在等待元素时超时且自愈未命中，**When** 结束，**Then** `failure_kind=locator`。
4. **Given** 请求因 `{{missing_var}}` 未定义而失败，**When** 结束，**Then** `failure_kind=script`。
5. **Given** 旧报告没有可解析的错误文本，**When** 展示，**Then** 显示「未知」而不是报错。

---

### User Story 2 - 环境类失败自动重试一次 (Priority: P2)

批次结束后，若存在 `env` 类失败且开关打开，平台自动用这些用例新建一个「环境重试」批次（`parent_batch_id` 关联原批次），默认最多 1 轮；`assertion`/`locator`/`script` 类**永不**自动重试。

**Why this priority**: 这是"降低人的参与度"最直接的收益——环境抖动不再需要人肉重跑。

**Independent Test**: 造一个 5xx 接口与一个断言失败用例同批执行，打开开关后应只重跑 5xx 那条；关掉开关则一条都不重跑。

**Acceptance Scenarios**:

1. **Given** 批次有 3 条 `env` 失败与 2 条 `assertion` 失败，**When** 结束且开关开启，**Then** 自动创建重试批次，只含 3 条 env 用例，`parent_batch_id` 指向原批次。
2. **Given** 重试批次再次失败，**When** 结束，**Then** 不再自动重试（最多 1 轮），不产生链式批次。
3. **Given** 批次被手动停止或取消，**When** 结束，**Then** 不触发自动重试。
4. **Given** 开关关闭，**When** 结束，**Then** 不创建任何重试批次（行为与现状一致）。
5. **Given** env 失败数超过单次上限，**When** 结束，**Then** 只取前 N 条重试，并在批次说明中记录被截断的数量。

---

### User Story 3 - 通知只报真实缺陷 (Priority: P2)

批次完成通知按分类决定级别与文案：存在未解决的非 `env` 失败才发 `error`；只有 `env` 失败且已自动重试/重试通过时发 `info` 或 `success`，并在消息中给出分类计数。

**Why this priority**: 通知是"人要不要介入"的开关，噪音不降，前两项的价值会被淹没。

**Independent Test**: 构造「全 env 且重试通过」「含 assertion」「开关关闭」三种批次，检查通知中心的类型与文案。

**Acceptance Scenarios**:

1. **Given** 批次失败全部为 `env` 且自动重试批次通过，**When** 原批次通知创建，**Then** 类型不为 `error`，消息含「环境重试通过」。
2. **Given** 批次含 1 条 `assertion` 失败，**When** 通知创建，**Then** 类型为 `error`，消息含「真实缺陷 1」。
3. **Given** 自动重试开关关闭且只有 env 失败，**When** 通知创建，**Then** 类型为 `info`，提示可开启自动重试。
4. **Given** 历史批次（无分类数据），**When** 查看通知，**Then** 不报错，按「含失败」处理。

---

### User Story 4 - 报告页按类别查看与筛选 (Priority: P3)

报告页的用例列表新增失败类别列，支持按类别筛选；批次详情顶部给出分类分布（真实缺陷 / 环境 / 定位 / 数据 / 脚本 / 未知）与自动重试批次的跳转。

**Why this priority**: 让人在仍需介入时能快速定位问题面，属于体验补全。

**Independent Test**: 打开一个混合失败批次，按 `assertion` 筛选只看断言失败；从分布点进重试批次。

**Acceptance Scenarios**:

1. **Given** 批次含 4 类失败，**When** 打开报告，**Then** 顶部显示四类计数，列表每行有类别标签。
2. **Given** 选择「环境」筛选，**When** 查询，**Then** 只返回 `failure_kind=env` 的运行。
3. **Given** 该批次有自动重试批次，**When** 查看，**Then** 有「环境重试」徽标且可跳到重试批次。

### Edge Cases

- 一个用例多步失败：取**第一处失败**的类别作为运行类别（后续步骤不再覆盖）。
- 401/5xx 同时伴随断言失败：按真实响应状态归类（`auth` / `env`），避免过期 token 或服务故障被当成真实缺陷。
- 执行器异常退出、没有报告文件：用 `RunLog` 的 CRITICAL 文本兜底，仍无法判断则 `unknown`。
- 数据集/参数化批次：按 `(case_id, dataset_group)` 各自分类，重试时保留组。
- 自动重试与「仅失败重跑」并发：同一批次只允许一个自动重试批次（幂等，重复触发返回已存在批次）。
- 场景批次（`source=api_scenario`）与普通批次走同一套分类与通知。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 每条失败运行 MUST 落一个 `failure_kind`，取值限定为 `env|auth|data|assertion|locator|script|unknown`。
- **FR-002**: 分类 MUST 可在无网络、无 LLM 调用的情况下完成（纯规则），单次分类不得增加可感知的执行耗时。
- **FR-003**: 分类结果 MUST 附带一句可读原因（截断至 500 字符），且不得包含请求体/响应体等敏感原文。
- **FR-004**: 分类 MUST NOT 改变运行 `status` 的既有语义（`failed` 仍是 `failed`）。
- **FR-005**: 批次结束后 MUST 汇总各分类计数，供报告与通知使用。
- **FR-006**: 系统 MUST 支持开关控制自动重试（默认开启），并支持配置重试类别（默认仅 `env`）、单次重试用例上限与轮次上限（默认 1）。
- **FR-007**: 自动重试 MUST 只针对配置的类别；`assertion`/`locator`/`script` MUST NOT 被自动重试。
- **FR-008**: 自动重试批次 MUST 带 `parent_batch_id` 与 `auto_retry=true` 标记，命名含「环境重试」；重试批次自身 MUST NOT 再触发自动重试。
- **FR-009**: 批次被停止/取消、或仍有运行中用例时 MUST NOT 触发自动重试。
- **FR-010**: 完成通知 MUST 按分类决定级别：存在未解决的非 `env` 失败为 `error`；仅 `env` 且已派发重试为 `info`；全部通过为 `success`；**自动重试批次仍只有环境失败时 MUST 升级为 `error`**（环境未恢复，继续静默会掩盖故障）。
- **FR-011**: 通知文案 MUST 包含分类计数，并在触发自动重试时说明重试批次数与用例数。
- **FR-012**: 报告 API MUST 支持按 `failure_kind` 筛选运行，并返回批次分类汇总。
- **FR-013**: 历史数据（`failure_kind` 为空）MUST 正常展示为「未知」，不报错、不回填。
- **FR-014**: 所有新开关 MUST 通过 `app/config.py` 的 `Settings` 暴露，并在关闭时保持现有行为不变。

### Key Entities

- **失败运行**：`test_runs` 中的一条执行记录；新增失败类别与原因，仍归属于批次。
- **批次分类摘要**：批次维度的类别计数与自动重试标记，供报告与通知读取。
- **自动重试批次**：由 env 失败派生出的子批次，通过 `parent_batch_id` 与原批次互跳。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 固定的 12 组失败样本分类 100% 命中预期类别（单测锁定）。
- **SC-002**: 只有 `env` 失败的批次，开启自动重试后重试覆盖率达到 100%（失败用例全部进入重试批次）。
- **SC-003**: 含 `assertion`/`locator`/`script` 失败的批次，自动重试用例数为 0。
- **SC-004**: 全 env 且重试通过的批次，通知级别不为 `error`；含真实缺陷的批次一定为 `error`。
- **SC-005**: 全量单元测试与 046/047/048 契约测试无回归。

## Assumptions

- 分类只依赖执行期已有的失败信息（异常类型、HTTP 状态、断言结果、错误文本、报告步骤），不引入模型调用；LLM 复核留待后续版本。
- 自动重试复用现有「仅失败重跑」能力，不新建表；新增列沿用启动时 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 的迁移方式。
- 只自动重试一轮：环境抖动一次没恢复，继续重试收益低且可能掩盖真实故障。
- 失败类别是"运行级"的，不细化到单个步骤。
- 前端沿用 Arco Design 与现有报告页结构，不新增页面。
