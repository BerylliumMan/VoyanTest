# core/api_runner/iteration.py — 数据集行展开与迭代标注（029-api-testing T045）
#
# 从 runner 拆出的纯函数：把数据集快照展开为「每行一个变量字典」，并给步骤结果
# 打 iteration 序号 / 多迭代描述前缀。无 IO、无状态，便于单测与复用。
from __future__ import annotations

from typing import Any, Optional

_DATASET_MODE_NOTES = {
    "random": "dataset_mode=random 暂按 sequential 顺序执行（本期仅透传与校验）",
    "loop": "dataset_mode=loop 暂按 sequential 顺序执行（本期仅透传与校验）",
}


def dataset_mode_notes(dataset_mode: str) -> list[str]:
    """random/loop 本期不实现迭代策略，只回一条降级说明。"""
    note = _DATASET_MODE_NOTES.get(dataset_mode)
    return [note] if note else []


def stringify_value(value: Any) -> str:
    return "" if value is None else str(value)


def stringify_row(row: dict) -> dict[str, str]:
    return {str(key): stringify_value(value) for key, value in row.items()}


def expand_iteration_rows(
    dataset: Optional[dict], dataset_row: Optional[dict]
) -> list[dict[str, str]]:
    """把数据集快照展开成「每行一个变量字典」的迭代列表。

    - 显式 ``dataset_row``（单行绑定，debug/兼容路径）优先，只产生一个迭代
    - 无数据集 / 数据集无行 → 单迭代空行（保持单次执行语义，行为与未绑定一致）
    - 兼容两种行形状：dict（T044 CSV/手工）与 list（data-model §1.3，按 columns 映射）
    """
    if dataset_row is not None:
        return [stringify_row(dataset_row)]
    if not isinstance(dataset, dict):
        return [{}]
    rows = dataset.get("rows")
    if not isinstance(rows, list) or not rows:
        return [{}]
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


def tag_iteration(step_result: dict, iteration: int, multi: bool) -> dict:
    """给步骤结果打迭代序号；多迭代时描述加 ``[迭代 N]`` 前缀（单迭代保持原格式）。"""
    tagged = {**step_result, "iteration": iteration}
    if multi:
        description = str(tagged.get("description") or "")
        tagged["description"] = f"[迭代 {iteration}] {description}".rstrip()
    return tagged


__all__ = [
    "dataset_mode_notes",
    "stringify_value",
    "stringify_row",
    "expand_iteration_rows",
    "tag_iteration",
]
