# Implementation Plan: 探索元素固化

**Branch**: `045-explore-element-fixation` | **Date**: 2026-10-30 | **Spec**: [spec.md](./spec.md)

## Summary

把浏览器探索时实际操作的页面元素（role + 可访问名称 + 候选定位）记录下来，随用例生成写入步骤的 `structured_step`，执行时优先用固化定位、失败即回退动态定位，并在成功后把有效定位写回 `learned_locator`。

## Technical Context

**Language/Version**: Python 3.11、TypeScript 5 + React 18
**Primary Dependencies**: FastAPI、SQLAlchemy async、Playwright MCP、现有 `core/locator_candidates.py` / `core/locator_memory.py` / `core/step_intent.py`
**Storage**: PostgreSQL（复用 `test_steps.structured_step` 与 `learned_locator`，**不新增表/列**）
**Testing**: pytest（纯函数单测 + 执行链路单测）
**Target Platform**: Linux 服务端 + 浏览器（服务端无头 / 客户端 Agent）
**Constraints**: 不改现有用例结构；历史用例无固化信息时必须照常执行；固化失败必须静默回退

## 关键现状（调研结论）

| 事实 | 影响 |
|---|---|
| 执行是 OTA（模型从快照候选选 ref），`structured_step.selector` 未被实时消费 | 需要新增 fast path，而不是改造已有定位器 |
| `learned_locator` 有人存、没人产、没人回放（`persist_learned_locators_from_results` 无调用方） | 需要接上写入与回放 |
| `core/locator_memory.resolve_ref` / `try_replay_plan_mcp` 已实现但无人调用 | 直接复用，不重写 |
| 服务端 `_orchestrator.py:355-364` 丢弃 `structured_step` / `learned_locator` | 需要透传 |
| 探索 journal 只记临时 ref，候选元素未进报告 | 需要记录 role/name |
| 生成侧靠 `parse_instant_to_structured` 从文本解析 target_name | 元素名必须出现在步骤文本里才能被固化 |

## 设计

### 数据流

```text
探索（browse.py）
  observations.candidates: LocatorCandidate{ref, role, name, text, attributes, frame_path}
  → 每次成功操作记录 element{role, name, text} 到 journal/report
生成（browse.build_report_materials → chat_gen）
  → 材料中的操作清单带元素名
  → 步骤文本规范化为「点击【登录】按钮」
  → 导入时 parse_instant_to_structured 得到 target_name/target_role
  → 写入 step.structured_step
执行（agent_bridge 客户端 / agent_runner 服务端）
  observe → [fast path: learned_locator → structured_step 匹配候选唯一 ref]
         → 命中：直接 act，跳过 LLM
         → 未命中：原 OTA
  成功 → learn_fingerprint_after_success → 写 learned_locator
  失败 → invalidate_learned_locator
报告 → tool_args.locator_source = learned | structured | dynamic
```

### 新增接口

`core/locator_memory.py` 新增纯函数：

```python
def resolve_persisted_locator(
    step: dict,
    snapshot: str,
    *,
    candidates=None,
    step_description: str = "",
) -> dict | None:
    """按固化优先级解析当前可执行目标。

    顺序：learned_locator(v1/v2) → structured_step.selector → structured_step(target_role+target_name)。
    返回 {"ref": "e12", "source": "learned"|"structured_selector"|"structured"} 或 None。
    """
```

`core/chat/browse.py` 新增：

```python
def element_from_candidates(candidates, ref: str) -> dict | None:
    """从候选快照取元素身份（排除临时 ref）。"""
```

### 任务拆分

| # | 任务 | 文件 |
|---|---|---|
| T1 | 探索记录元素身份到 journal/report | `core/chat/browse.py` |
| T2 | 报告材料带操作元素清单 | `core/chat/browse.py` |
| T3 | 生成导入写 structured_step（role/name） | `app/services/chat_gen.py` |
| T4 | `resolve_persisted_locator` 纯函数 + 单测 | `core/locator_memory.py` |
| T5 | 客户端 OTA fast path | `core/agent_bridge.py` |
| T6 | 成功后写回 learned_locator / 失败失效 | `core/agent_bridge.py`、`core/locator_memory.py` |
| T7 | 服务端步骤透传 + fast path | `core/runner/_orchestrator.py`、`core/agent_runner/runner.py` |
| T8 | 报告标注定位来源 | `core/agent_bridge.py`、`core/step_logs.py` |
| T9 | 用例详情展示/编辑固化信息 | `frontend/src/pages/testcases/components/StepList.tsx` |
| T10 | 开关与回归 | `app/config.py`、tests |

### 开关

- `ui_locator_fixation_enabled`（默认开启）：关闭后执行侧不读固化信息，行为回到现状。
- 复用现有 `healing_config.locator_memory_enabled` 作为二级开关。

## 验收

按 spec 的 SC-001~SC-006；本期以自动化测试 + 一次真实探索执行验证为主。

---

## 实施状态（2026-10-30）

| # | 任务 | 状态 |
|---|---|---|
| T1 | 探索记录元素身份到 journal/report | ✅ 完成 |
| T2 | 报告材料带操作元素清单 | ✅ 完成 |
| T3 | 生成导入写 structured_step | ✅ 复用现有 `parse_instant_to_structured`（target_name） |
| T4 | `resolve_persisted_locator` 纯函数 + 单测 | ✅ 完成（9 项测试） |
| T5 | 执行优先固化（接入 `build_auto_unit`） | ✅ 完成（16 项测试） |
| T6 | 成功后写回 learned_locator / 失败失效 | ⏳ 未接线（需接运行结束流程，后续迭代） |
| T7 | 服务端步骤透传 | ✅ 完成 |
| T8 | 报告标注定位来源 | ⏳ 未完成（action 已带 `locator_source`，未进报告行） |
| T9 | 用例详情展示固化信息 | ✅ 已有（StepList 展示 learned_locator） |
| T10 | 开关 `ui_locator_fixation_enabled` | ✅ 完成 |

当前生效的行为：探索记录元素 → 材料提示元素名 → 生成步骤解析出 target_name →
执行时 `build_auto_unit` 优先按固化定位匹配候选，未命中回退原逻辑。
