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
    init_case_ids: List[int] = []
    # 列表批跑默认 before_each；用例集入口应显式传 once
    init_policy: InitPolicy = "before_each"
    # 031（US9）：接口用例的数据驱动执行参数（覆盖用例内绑定；不落用例）
    dataset_mode: Optional[str] = None  # sequential | random | loop
    loop_count: Optional[int] = None    # 仅 loop 模式（1~100）
    fail_fast: Optional[bool] = None    # 迭代失败即停（默认沿用用例 fail_policy）
    # 031（US10）：批次内复用会话（共享 httpx client，cookie 可见；默认关闭）
    session_reuse: Optional[bool] = None


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
