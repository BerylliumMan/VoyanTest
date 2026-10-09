"""Pydantic schemas for execution endpoints.

按执行模式分组:
- BatchRunRequest / BatchCaseIdsRequest: 批量运行参数
- DebugRunRequest: 单用例调试模式参数
"""
from typing import List, Literal, Optional

from pydantic import BaseModel

InitPolicy = Literal["once", "before_each"]


class BatchRunRequest(BaseModel):
    case_ids: List[int]
    environment_id: Optional[int] = None
    # 2026-09-30 修复：前端批量会带 agent_name（选客户端执行），此前字段缺失被 Pydantic 静默忽略
    # → 批量「选客户端」实际跑在服务端。现在显式接收并路由到客户端派发。
    agent_name: Optional[str] = None
    init_case_ids: List[int] = []
    # 列表批跑默认 before_each；用例集入口应显式传 once
    init_policy: InitPolicy = "before_each"
    # 031（US9）：接口用例的数据驱动执行参数（覆盖用例内绑定；不落用例）
    dataset_mode: Optional[str] = None  # sequential | random | loop
    loop_count: Optional[int] = None    # 仅 loop 模式（1~100）
    fail_fast: Optional[bool] = None    # 迭代失败即停（默认沿用用例 fail_policy）
    # 031（US10）：批次内复用会话（共享 httpx client，cookie 可见；默认关闭）
    session_reuse: Optional[bool] = None
    # 033（US10）：用例间隔（毫秒，0 = 不等待）与首个失败即停止
    delay_ms: Optional[int] = None
    stop_on_error: Optional[bool] = None


def validate_delay_ms(value: Optional[int], max_ms: int) -> int:
    """033（US10）：delay_ms 校验（None→0；负值/超上限抛 ValueError 由路由转 400）。"""
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("delay_ms 必须是整数")
    if value < 0:
        raise ValueError("delay_ms 不能为负数")
    if value > int(max_ms):
        raise ValueError(f"delay_ms 不能超过 {int(max_ms)}")
    return value


class BatchCaseIdsRequest(BaseModel):
    case_ids: List[int]
    agent_name: Optional[str] = None
    init_case_ids: List[int] = []
    environment_id: Optional[int] = None
    backend: Optional[str] = None  # ota only (legacy values normalized)
    # 列表批跑默认 before_each；用例集入口应显式传 once
    init_policy: InitPolicy = "before_each"


class DebugRunRequest(BaseModel):
    environment_id: Optional[int] = None
