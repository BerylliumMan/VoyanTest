# app/services/dataset_execution.py — 039 UI 参数化执行预检与展开
#
# 单一入口：批量/单用例/客户端/用例集/对话/调度/CLI 在**创建批次之前**调用，
# 完成数据集用途/列/参数/组数校验，并返回「每个用例 → 展开后的数据行」。
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from core.dataset_iteration import DatasetPlanError, validate_dataset_plan
from core.dataset_variables import discover_parameters, missing_columns, validate_parameters

logger = logging.getLogger(__name__)


@dataclass
class DatasetExecutionPlan:
    """一次批次（或单用例）的展开结果。"""

    # case_id -> 展开后的行（每组一行）；未绑定数据集为 None
    rows_by_case: dict[int, Optional[list[dict]]] = field(default_factory=dict)
    # case_id -> 生效的模式/失败策略（用于组间跳过判定）
    modes: dict[int, str] = field(default_factory=dict)
    fail_fast: dict[int, bool] = field(default_factory=dict)
    total_groups: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def rows_for(self, case_id: int) -> Optional[list[dict]]:
        return self.rows_by_case.get(case_id)


def _effective(value, fallback):
    return fallback if value is None else value


async def plan_batch_datasets(
    db: AsyncSession,
    cases: Iterable,
    *,
    dataset_id: Optional[int] = None,
    dataset_mode: Optional[str] = None,
    loop_count: Optional[int] = None,
    dataset_fail_fast: Optional[bool] = None,
    max_groups: Optional[int] = None,
) -> DatasetExecutionPlan:
    """校验并展开 UI 用例的数据集（创建批次之前）。

    - 覆盖参数（dataset_id/mode/loop/fail_fast）对本次执行的所有 UI 用例生效；
    - 未绑定数据集的用例保持单次执行；
    - 任何错误写入 ``plan.errors``，调用方应在建批前转为 400/异常。
    """
    from app.config import get_settings
    from app.crud.api_dataset import get_dataset

    if max_groups is None:
        max_groups = int(getattr(get_settings(), "ui_dataset_max_groups", 100) or 100)

    plan = DatasetExecutionPlan()
    dataset_cache: dict[int, object] = {}

    for case in cases or []:
        cid = getattr(case, "id", None)
        if cid is None:
            continue
        if (getattr(case, "case_kind", None) or "") != "ui":
            plan.rows_by_case[cid] = None
            continue

        effective_dataset_id = _effective(dataset_id, getattr(case, "dataset_id", None))
        # 只接受真实整数 id（测试桩/异常对象不视为绑定）
        if isinstance(effective_dataset_id, bool) or not isinstance(effective_dataset_id, int):
            effective_dataset_id = None
        effective_mode = _effective(dataset_mode, getattr(case, "dataset_mode", None)) or "sequential"
        if not isinstance(effective_mode, str):
            effective_mode = "sequential"
        effective_loop = _effective(loop_count, getattr(case, "loop_count", None)) or 1
        if isinstance(effective_loop, bool) or not isinstance(effective_loop, int):
            effective_loop = 1
        effective_fail_fast = bool(
            _effective(dataset_fail_fast, getattr(case, "fail_fast", False))
        )
        plan.modes[cid] = str(effective_mode)
        plan.fail_fast[cid] = effective_fail_fast

        steps = list(getattr(case, "steps", None) or [])
        declared = getattr(case, "parameters", None)

        if not effective_dataset_id:
            # 未绑定数据集：若有占位符则拒绝（不能把 {{x}} 原样发给模型）
            discovered = discover_parameters(steps)
            if discovered:
                plan.errors.append(
                    f"用例 #{cid} 引用了参数（{', '.join(discovered)}）但未绑定数据集"
                )
                continue
            plan.rows_by_case[cid] = None
            continue

        dataset = dataset_cache.get(int(effective_dataset_id))
        if dataset is None:
            dataset = await get_dataset(db, int(effective_dataset_id))
            if dataset is not None:
                dataset_cache[int(effective_dataset_id)] = dataset
        if dataset is None:
            plan.errors.append(f"数据集 #{effective_dataset_id} 不存在或已被删除（用例 #{cid}）")
            continue
        if int(getattr(dataset, "project_id", 0) or 0) != int(getattr(case, "project_id", 0) or 0):
            plan.errors.append(f"数据集 #{effective_dataset_id} 不属于用例 #{cid} 所在项目")
            continue
        if (getattr(dataset, "purpose", None) or "api") != "ui":
            plan.errors.append(f"数据集 #{effective_dataset_id} 不是 UI 数据集（用例 #{cid}）")
            continue

        if declared:
            for error in validate_parameters(steps, declared):
                plan.errors.append(f"用例 #{cid}：{error}")
        missing = missing_columns(steps, list(getattr(dataset, "columns", None) or []))
        if missing:
            plan.errors.append(
                f"用例 #{cid}：数据集 #{effective_dataset_id} 缺少列 {'、'.join(missing)}"
            )
            continue

        try:
            rows = validate_dataset_plan(
                {
                    "columns": list(getattr(dataset, "columns", None) or []),
                    "rows": list(getattr(dataset, "rows", None) or []),
                },
                mode=str(effective_mode),
                loop_count=int(effective_loop),
                max_groups=max_groups,
            )
        except DatasetPlanError as exc:
            plan.errors.append(f"用例 #{cid}：{exc}")
            continue

        plan.rows_by_case[cid] = rows
        plan.total_groups += len(rows)

    return plan


__all__ = ["DatasetExecutionPlan", "plan_batch_datasets"]
