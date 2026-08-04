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


class BatchCaseIdsRequest(BaseModel):
    case_ids: List[int]
    agent_name: Optional[str] = None
    init_case_ids: List[int] = []
    environment_id: Optional[int] = None
    backend: Optional[str] = None  # nl_goal | compiled_script | legacy_* | browser_use
    # 列表批跑默认 before_each；用例集入口应显式传 once
    init_policy: InitPolicy = "before_each"


class DebugRunRequest(BaseModel):
    environment_id: Optional[int] = None
