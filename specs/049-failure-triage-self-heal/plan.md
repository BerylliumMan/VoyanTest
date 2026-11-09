# Implementation Plan: 失败分类与自愈

**Branch**: `049-failure-triage-self-heal` | **Date**: 2026-11-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/049-failure-triage-self-heal/spec.md`

## Summary

在不改变失败判定语义的前提下，补上三层能力：

1. **分类**：执行收尾时用纯规则把每条失败运行归入 `env/auth/data/assertion/locator/script/unknown`，落库并展示。
2. **自愈**：批次结束后，把 `env` 类失败自动交给现有「仅失败重跑」链路重试一次（可开关、可限流、幂等），断言/定位/脚本类永不重试。
3. **降噪**：完成通知按分类决定级别与文案，只有未解决的真实缺陷才发 `error`。

不新增表，只加列；复用 031 的 `parent_batch_id` 与重跑编排；分类不引入 LLM 调用。

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI、SQLAlchemy async、pydantic-settings（未新增三方依赖）
**Storage**: PostgreSQL（`test_runs` 加 2 列、`run_batches` 加 2 列，启动时幂等 DDL）
**Testing**: pytest；单测无网络，契约测试用本地 sqlite（沿用 046/047/048 的 fixture）
**Target Platform**: Linux 服务端容器（前端为本地静态资源）
**Project Type**: Web 服务（FastAPI + React/Arco）
**Performance Goals**: 分类为纯文本/状态匹配，单批次千条运行额外开销 < 50ms；不阻塞事件循环
**Constraints**: 分类不得读取请求/响应原文入库；自动重试最多 1 轮；关闭开关时行为与现状完全一致
**Scale/Scope**: 单批次 ≤ 2000 条运行；单次自动重试 ≤ 配置上限（默认 20 条用例）

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. API-First**：分类汇总、按类别筛选、按类别重跑均先提供 REST 端点，前端只消费这些端点。✅
- **II. Test Integrity**：分类器与重试编排全部本地可测（纯函数 + sqlite 契约），不依赖网络/浏览器。✅
- **III. Asset Localization**：仅改现有报告页，不引入任何外部 CDN 资源。✅
- **IV. Configuration as Code**：所有开关进 `app/config.py::Settings`，默认值保持现有行为。✅
- **V. Simplicity**：复用 `parent_batch_id`、既有重跑编排与报告 JSON，不新建表、不引入新服务进程。✅

## Project Structure

### Documentation (this feature)

```text
specs/049-failure-triage-self-heal/
├── plan.md              # 本文件
├── spec.md              # 需求
├── research.md          # 分类口径与实现取舍
├── data-model.md        # 列与枚举
├── quickstart.md        # 真机验证脚本
├── contracts/
│   └── failure-triage.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source (repository)

```text
app/
├── config.py                     # 新增 failure_triage_* 开关
├── main.py                       # 启动期幂等 DDL（4 列）
├── models/batch.py               # TestRun/RunBatch 新增列
├── routers/report_router.py      # 分类汇总、筛选、按类别重跑
└── services/
    ├── failure_triage.py         # 新增：分类、汇总、通知判定（纯函数为主）
    ├── batch_rerun.py            # 新增：把「仅失败重跑」从路由抽成服务
    ├── failure_retry.py          # 新增：自动重试编排（幂等、限流、轮次）
    └── notifications.py          # 接入分类文案（不再无条件 error）

core/
├── runner/_persistence.py        # 失败落库时写入 failure_kind/reason
├── runner/_api_execution.py      # 批次收尾改调 finalize（重试 + 通知）
└── agent_bridge.py               # UI 批次收尾同样走 finalize

frontend/src/pages/reports/
├── index.tsx                     # 类别列 + 筛选 + 重试徽标
└── RunDetail.tsx                 # 详情头部类别与原因

tests/
├── unit/test_failure_triage.py   # 12 组样本分类
├── unit/test_failure_retry.py    # 重试门控与决策
└── contract/test_failure_summary_api.py
```

**Structure Decision**: 分类与决策放在 service 层（纯函数，便于单测），落库在 `core/runner/_persistence.py` 的统一入口做，三个批次收尾点收敛到一个 `finalize_batch()`，避免三处各写一套通知逻辑。

## Phase 0 研究结论

见 [research.md](./research.md)：分类优先级、规则来源、重跑复用方式、通知时机均已定稿，无未决项。

## Phase 1 设计

- 数据模型：[data-model.md](./data-model.md)（枚举 + 4 个新列 + 兼容策略）
- 接口契约：[contracts/failure-triage.md](./contracts/failure-triage.md)
- 关键决策：
  1. **先断言后环境**：响应已到达的断言失败优先判 `assertion`，避免"5xx 顺带断言失败"被误判为环境问题。
  2. **分类写在落库处**：`save_test_run_result()` 与 `mark_run_failed()` 是两条唯一写入路径，集中在此，历史数据留空。
  3. **重跑复用**：把 `POST /batches/{id}/rerun-failed` 的实现下沉为 `batch_rerun.create_rerun_batch()`，手动与自动共用，`kinds` 作为筛选参数。
  4. **幂等**：自动重试前查 `parent_batch_id == 本批次 and auto_retry == true`，已存在则跳过。
  5. **通知改为 finalize 内完成**：`finalize_batch()` 依次做「汇总 → 视情况重跑 → 发通知」，替换三处直接调用 `notify_batch_completed` 的位置。

## Implementation Strategy

1. **Slice A（P1，US1）**：分类器 + 落库 + 报告展示，先让类别可见。
2. **Slice B（P2，US2）**：重跑下沉 + 自动重试 + 幂等/限流。
3. **Slice C（P2/P3，US3/US4）**：通知分级 + 报告筛选与分布。

每片结束跑一次相关单测；全部完成后跑全量单测与 046/047/048 契约。

## Risks & Mitigations

| 风险 | 影响 | 对策 |
|---|---|---|
| 误判 assertion 为 env（5xx + 断言失败） | 真实缺陷被自动重试掩盖 | 断言优先；重试批次仍失败时通知为 `error` |
| 自动重试形成链式批次 | 环境持续故障时反复刷批次 | `auto_retry=true` 的批次不再触发；最多 1 轮 |
| 分类依赖中文错误文本 | 上游文案改动导致漏判 | 规则同时匹配异常类型/状态码/英文关键字，样本单测锁定 |
| 收尾逻辑改动影响现有通知 | 通知丢失或重复 | `finalize_batch` 保留原轮询语义；开关关闭走原路径；契约测试覆盖 |
| 前端展示改动 | 报告页回归 | 只加列与标签，不改现有列与排序 |

## Complexity Tracking

无宪法违规；新增模块 3 个（triage / rerun / retry），均不超过 300 行且为纯逻辑或轻编排。
