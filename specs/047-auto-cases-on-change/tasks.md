# Tasks: 变更即补用例（047）

- [X] T001 配置项：`api_auto_case_enabled`、`api_auto_case_max_definitions`、`api_auto_case_max_each`、`api_auto_case_daily_limit` in `app/config.py`
- [X] T002 模型与 DDL：`api_definitions.auto_case_generated_at` in `app/models/api_test.py`、`app/main.py`
- [X] T003 影响计算支持「命中但无用例」：`compute_impact(..., include_without_cases=False)` in `app/services/api_change_impact.py`
- [X] T004 写测试 `tests/unit/test_api_auto_cases.py`（生成/幂等/上限/开关/失败隔离）
- [X] T005 实现 `app/services/api_auto_cases.py`
- [X] T006 接入 `plan_after_sync`：补用例 → 重算影响 → 建计划，错误写入说明 in `app/services/api_change_plans.py`
- [X] T007 审计与通知补充自动补测信息
- [X] T008 契约测试：变更检查后无用例接口进入计划 in `tests/contract/test_api_repo_sync.py`
- [X] T009 前端：计划说明展示自动补测数量（可选标签）
- [X] T010 真机验证：新增接口 → 计划含 `[自动]` 用例 → 执行批次
- [X] T011 文档：quickstart/contracts 补充 047 行为
