from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

I = TypeVar("I")
O = TypeVar("O")


class BaseAgent(ABC, Generic[I, O]):
    """Agent 基类，定义 agent 的通用接口。"""

    name: str = ""

    @abstractmethod
    async def run(self, input_data: I) -> O:
        """执行 agent 任务。"""
        ...
