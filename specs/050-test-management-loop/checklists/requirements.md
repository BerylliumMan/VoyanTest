# Requirements Checklist: 测试管理闭环

**Purpose**: 验证 spec 的完整性、可测性与边界覆盖（实现前 gate）。
**Created**: 2026-11-11

## 需求完整性

- [x] 计划、轮次、任务、评论、评审五块都有独立用户故事与验收场景
- [x] 每个用户故事都有优先级（US1/US2/US3=P1，US4/US5=P2）与独立测试方式
- [x] 关键实体（TestPlan/TestPlanItem/TestCycle/TestCycleItem/Comment/CaseReview）已定义
- [x] 执行语义明确「不改变」；环境重试沿用 049
- [x] 权限口径（admin/project_ids）明确
- [x] 非目标（组织/RBAC、外部集成、移动端）明确

## 可测性

- [x] 每个验收场景都有 Given/When/Then
- [x] 进度口径（passed / 有效项）可计算
- [x] 轮次并发/重复启动有 409 约定
- [x] 重跑、停止、归档、用例删除等边界有说明
- [x] 通知触发点明确（指派、提及、评审发起/结论）
- [x] 性能目标（列表 P95、待办聚合）可度量

## 一致性

- [x] 与 049 的 `source/source_id`、`finalize_batch` 兼容
- [x] 与 042 的 QualityRegressionPlan 并存且不污染
- [x] 复用 `notifications`，不新增外发渠道
- [x] 新表走 `create_all` + 幂等 DDL，符合项目约定
- [x] 前端只消费新 REST 端点，符合 API-First

## 风险

- [x] 轮次快照与计划项修改的解耦已明确（快照固化）
- [x] 批次被自动重试时的轮次统计口径已明确（最新批次覆盖）
- [x] 同一用例 pending 评审唯一性已明确（应用层 + 索引）
- [x] 评论 XSS 风险已明确（纯文本渲染）
