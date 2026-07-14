"""运行时全局配置（内存级，各模块共享）。

提供 FastAPI 路由（app/）和核心执行引擎（core/）之间的单向读写契约：
- ``app/routers/`` 模块通过 HTTP API 写入。
- ``core/runner/`` 模块在用例执行时读取。

重启后重置为默认值。
"""

from pydantic import BaseModel


class HealingConfig(BaseModel):
    enabled: bool = True
    max_retries: int = 3
    threshold: float = 0.8


healing_config = HealingConfig()
