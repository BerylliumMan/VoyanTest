# Implementation Plan: 变更即补用例（047）

## Summary

在 046 的 `plan_after_sync` 中，对"受影响但没有用例"的接口，用**规则生成**（`generate_cases_for_definition`，纯函数）补出基础用例，直接导入为 `case_kind='api'`，再重算影响清单纳入变更计划。全程受开关与规模上限约束，失败不阻塞计划。

## Technical Context

- Python 3.11 / FastAPI / SQLAlchemy async
- 复用：`app.gen.api.generator.generate_cases_for_definition`（规则，无 IO）
- 复用：`app/services/api_change_impact.compute_impact`、`app/services/api_change_plans.plan_after_sync`
- 不引入新依赖

## 数据模型

`api_definitions` 扩列：

| 字段 | 类型 | 说明 |
|---|---|---|
| auto_case_generated_at | TIMESTAMPTZ NULL | 上次自动生成时间；非空即不再自动补 |

自动用例标识写进 `test_cases.api_spec.meta`：

```json
{ "auto_generated": true, "auto_source": "change_trigger", "generated_at": "...", "trigger_id": 7 }
```

## 配置（app/config.py）

| 变量 | 默认 | 说明 |
|---|---|---|
| api_auto_case_enabled | true | 总开关 |
| api_auto_case_max_definitions | 5 | 单次变更最多补几个接口 |
| api_auto_case_max_each | 3 | 单接口最多几条用例 |
| api_auto_case_daily_limit | 50 | 单项目每日自动生成上限 |

## 实现

### 新服务 `app/services/api_auto_cases.py`

```python
async def auto_generate_missing_cases(
    db, *, project_id, definition_ids, trigger_id=None,
) -> AutoCaseStats  # generated_definitions / generated_cases / skipped / errors
```

流程：

1. 读配置；关闭则直接返回。
2. 每日上限检查：统计当日 `api_spec.meta.auto_generated=true` 的用例数（JSON 查询，SQLite 测试用 Python 兜底）。
3. 按 `definition_ids` 顺序处理，最多 `max_definitions`：
   - 定义必须属于项目、`status='active'`、`auto_case_generated_at IS NULL`
   - 调 `generate_cases_for_definition(definition, {"max_cases_per_operation": max_each})`
   - 每条草案 → `TestCase(project_id, name=f"[自动] {method} {path}", case_kind="api", api_definition_id=..., api_spec=...)`
   - 写 `auto_case_generated_at`
4. 单条失败记入 `errors`，继续。
5. flush（由调用方 commit）。

### 接入 `plan_after_sync`

```python
affected = await compute_impact(...)
missing = [a for a in affected_without_cases]
stats = await auto_generate_missing_cases(db, project_id=..., definition_ids=missing, trigger_id=result.trigger_id)
if stats.generated_cases:
    affected = await compute_impact(...)  # 重新映射
```

注意：`compute_impact` 目前只返回有用例的接口，因此需要先取"命中但没有用例"的接口集合。为此新增 `compute_impact(..., include_without_cases=True)` 或单独函数 `match_affected_definitions` 返回命中集合（含无用例）。

实现选择：给 `compute_impact` 增加参数 `include_without_cases: bool = False`，为 True 时返回 `case_ids=[]` 的条目。计划创建前再过滤空条目。

### 审计与通知

- `auto_generate_missing_cases` 结束后写 `log_audit("api_change.auto_cases", {...})`
- 计划创建通知的 message 追加"已自动补 N 条用例"

### 前端

- 变更计划展开：`affected[].case_ids` 已包含新用例，无需改结构；在计划说明中显示自动补测数量（后端 reason/notes）。
- 接口用例列表：`api_spec.meta.auto_generated` 的用例显示「自动」标签（可选，P2）。

## 风险

| 风险 | 对策 |
|---|---|
| 自动用例断言弱 | 名称带 `[自动]`，显式可识别；后续可接 LLM 增强 |
| 规则生成不适合 multipart | 生成器已有 `skip_multipart`，保持默认跳过 |
| 每次变更触发过多 LLM/DB 写入 | 规则生成无 LLM；上限与每日限额 |
| 与人工用例冲突 | 只新增，不修改/删除人工用例；同接口只生成一次 |

## 验收

- 单测：生成/幂等/上限/开关/失败隔离/影响重算
- 契约：变更检查后无用例接口出现在计划中
- 真机：本地仓库新增接口 → 同步+检查 → 计划含 `[自动]` 用例 → 执行出批次
