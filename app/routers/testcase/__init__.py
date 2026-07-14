"""
Testcase router package.

拆分结构（保持路由注册顺序以避免 /preview-plan 被 /{case_id} 误匹配）:
    1. preview   - /preview-plan (字面量路径，必须先注册)
    2. crud      - POST /, GET /search, GET /{case_id}, DELETE/PUT /{case_id}, 列表
    3. execution - /{case_id}/run, /{case_id}/run-client, /batch-run*, /module/*/run, /project/*/run
    4. batch_ops - /batch-move, /batch-copy
"""
from fastapi import APIRouter

from . import batch_ops, crud, execution, preview

router = APIRouter(
    prefix="/api/testcases",
    tags=["Test Cases"],
)

router.include_router(preview.router)
router.include_router(crud.router)
router.include_router(execution.router)
router.include_router(batch_ops.router)

__all__ = ["router"]
