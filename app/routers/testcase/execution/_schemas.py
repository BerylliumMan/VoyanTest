"""Pydantic schemas for execution endpoints.

按执行模式分组:
- BatchRunRequest / BatchCaseIdsRequest: 批量运行参数
- DebugRunRequest: 单用例调试模式参数
"""
from typing import List, Optional

from pydantic import BaseModel


class BatchRunRequest(BaseModel):
    case_ids: List[int]
    environment_id: Optional[int] = None
    init_case_ids: List[int] = []


class BatchCaseIdsRequest(BaseModel):
    case_ids: List[int]
    agent_name: Optional[str] = None
    init_case_ids: List[int] = []
    environment_id: Optional[int] = None


class DebugRunRequest(BaseModel):
    environment_id: Optional[int] = None
