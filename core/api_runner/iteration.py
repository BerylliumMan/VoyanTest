# core/api_runner/iteration.py — 数据集行展开与迭代标注（029-api-testing T045）
#
# 从 runner 拆出的纯函数：把数据集快照展开为「每行一个变量字典」，并给步骤结果
# 打 iteration 序号 / 多迭代描述前缀。无 IO、无状态，便于单测与复用。
from __future__ import annotations

import random as _random
from typing import Optional

from core.dataset_iteration import (  # noqa: F401（对外 API 兼容再导出）
    ALLOWED_DATASET_MODES,
    MAX_LOOP_COUNT,
    normalize_rows,
    stringify_row,
    stringify_value,
)


def dataset_mode_notes(dataset_mode: str) -> list[str]:
    """仅对未知模式回一条降级说明（random/loop 自 031 起已实现）。"""
    if dataset_mode and dataset_mode not in ALLOWED_DATASET_MODES:
        return [f"未知 dataset_mode={dataset_mode}，按 sequential 执行"]
    return []


def expand_iteration_rows(
    dataset: Optional[dict], dataset_row: Optional[dict]
) -> list[dict[str, str]]:
    """把数据集快照展开成「每行一个变量字典」的迭代列表（接口侧宽松语义）。

    - 显式 ``dataset_row``（单行绑定，debug/兼容路径）优先，只产生一个迭代
    - 无数据集 / 数据集无行 → 单迭代空行（保持单次执行语义，行为与未绑定一致）
    """
    if dataset_row is not None:
        return [stringify_row(dataset_row)]
    return normalize_rows(dataset) or [{}]


def plan_iterations(
    dataset: Optional[dict],
    dataset_row: Optional[dict],
    *,
    mode: str = "sequential",
    loop_count: int = 1,
    seed: Optional[int] = None,
) -> list[dict[str, str]]:
    """031（US9）迭代计划：按模式把数据集展开为「每行一个变量字典」的执行序列。

    - sequential：保持行序（既有语义）
    - random：行序随机（同 seed 可复现；仍是同一集合的排列）
    - loop：行序列重复 ``loop_count`` 遍（钳制 1~100）
    - 未知模式 → sequential（配合 dataset_mode_notes 回降级说明）
    - 无数据集/无行/显式单行 → 单迭代（零回归）
    """
    base = expand_iteration_rows(dataset, dataset_row)
    normalized = str(mode or "sequential")
    if normalized == "random":
        shuffled = list(base)
        _random.Random(seed).shuffle(shuffled)
        return shuffled
    if normalized == "loop":
        try:
            times = int(loop_count or 1)
        except (TypeError, ValueError):
            times = 1
        times = max(1, min(times, MAX_LOOP_COUNT))
        return list(base) * times
    return base


def tag_iteration(step_result: dict, iteration: int, multi: bool) -> dict:
    """给步骤结果打迭代序号；多迭代时描述加 ``[迭代 N]`` 前缀（单迭代保持原格式）。"""
    tagged = {**step_result, "iteration": iteration}
    if multi:
        description = str(tagged.get("description") or "")
        tagged["description"] = f"[迭代 {iteration}] {description}".rstrip()
    return tagged


__all__ = [
    "ALLOWED_DATASET_MODES",
    "MAX_LOOP_COUNT",
    "dataset_mode_notes",
    "plan_iterations",
    "stringify_value",
    "stringify_row",
    "expand_iteration_rows",
    "tag_iteration",
]
