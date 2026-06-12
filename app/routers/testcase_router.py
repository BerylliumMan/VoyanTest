# app/routers/testcase_router.py
# 兼容 shim：实际实现已拆分到 app/routers/testcase/ 子包
# 保持 `from .routers import testcase_router` 兼容
from .testcase import router

__all__ = ["router"]
