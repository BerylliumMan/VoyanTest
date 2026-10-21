# core/dataset_iteration.py — 数据集展开与计划（039 UI 用例参数化）
#
# 从 core/api_runner/iteration.py 抽出的纯函数，供接口测试与 UI 参数化共用。
# 与接口侧的区别：本模块提供**严格模式**（plan_dataset_rows），空数据集/越界/未知模式
# 直接抛 DatasetPlanError，由调用方在执行前转 400；接口侧保留原有宽松语义（钳制/降级）。
from __future__ import annotations

import random as _random
from typing import Any, Optional

# 031（US9）：loop 模式重复上限（防放大）
MAX_LOOP_COUNT = 100
ALLOWED_DATASET_MODES = ("sequential", "random", "loop")


class DatasetPlanError(ValueError):
    """数据集展开预检失败（消息可直接展示给用户）。"""


def stringify_value(value: Any) -> str:
    return "" if value is None else str(value)


def stringify_row(row: dict) -> dict[str, str]:
    return {str(key): stringify_value(value) for key, value in row.items()}


def normalize_rows(dataset: Optional[dict]) -> list[dict[str, str]]:
    """把数据集行归一为 ``[{列名: 字符串}]``。

    兼容 dict 行与 list 行（按 columns 映射）；非法行归一为空 dict。
    """
    if not isinstance(dataset, dict):
        return []
    rows = dataset.get("rows")
    if not isinstance(rows, list):
        return []
    columns = [str(column) for column in (dataset.get("columns") or [])]
    out: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(stringify_row(row))
        elif isinstance(row, (list, tuple)):
            out.append(
                {
                    columns[index]: stringify_value(value)
                    for index, value in enumerate(row)
                    if index < len(columns)
                }
            )
        else:
            out.append({})
    return out


def dataset_columns(dataset: Optional[dict]) -> list[str]:
    if not isinstance(dataset, dict):
        return []
    return [str(column) for column in (dataset.get("columns") or [])]


def validate_dataset_plan(
    dataset: Optional[dict],
    *,
    mode: str = "sequential",
    loop_count: int = 1,
    max_groups: int = 100,
    require_rows: bool = True,
) -> list[dict[str, str]]:
    """严格预检并展开数据集（039）。

    - 未知模式 → DatasetPlanError
    - loop_count 非整数/越界 → DatasetPlanError
    - require_rows 且数据集为空 → DatasetPlanError
    - 行数 × 循环次数 > max_groups → DatasetPlanError（消息含实际组数与建议）
    - 返回展开后的行列表（长度 = 组数）
    """
    normalized = str(mode or "sequential")
    if normalized not in ALLOWED_DATASET_MODES:
        raise DatasetPlanError(
            f"未知展开模式 {normalized}（可选 sequential/random/loop）"
        )
    try:
        times = int(loop_count if loop_count is not None else 1)
    except (TypeError, ValueError):
        raise DatasetPlanError("循环次数必须为 1~100 的整数")
    if times < 1 or times > MAX_LOOP_COUNT:
        raise DatasetPlanError(f"循环次数必须为 1~{MAX_LOOP_COUNT} 的整数")

    rows = normalize_rows(dataset)
    if not rows:
        if require_rows:
            raise DatasetPlanError("数据集没有数据行")
        return []

    groups = len(rows) * (times if normalized == "loop" else 1)
    if groups > int(max_groups):
        raise DatasetPlanError(
            f"本次将执行 {groups} 组（{len(rows)} 行 × {times if normalized == 'loop' else 1} 次），"
            f"超过单次上限 {int(max_groups)}，请分批执行或减少数据行"
        )

    if normalized == "random":
        shuffled = list(rows)
        _random.Random(None).shuffle(shuffled)
        return shuffled
    if normalized == "loop":
        return list(rows) * times
    return list(rows)


__all__ = [
    "ALLOWED_DATASET_MODES",
    "DatasetPlanError",
    "MAX_LOOP_COUNT",
    "dataset_columns",
    "normalize_rows",
    "stringify_row",
    "stringify_value",
    "validate_dataset_plan",
]
