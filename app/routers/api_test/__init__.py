"""接口测试路由聚合（029-api-testing）。

子路由按 Wave 挂载：
  - import_.py   文档导入（US1）
  - definitions.py 接口定义 CRUD（US1）
  - generate.py  生成用例（US1）
  - debug.py     调试发送（US2）
  - datasets.py  数据集（US5）
  - scenarios.py 场景（场景页）
  - files.py     测试文件（031 US1：上传/列表/删除）
契约：specs/029-api-testing/contracts/api-test-contract.md
      specs/031-api-testing-enhancements/contracts/api-enhancements.md
"""
from fastapi import APIRouter

from app.routers.api_test import (
    tokens as tokens_router,
    datasets,
    debug,
    definitions,
    files,
    generate,
    import_,
    mocks,
    overview,
    scenarios,
)

router = APIRouter(prefix="/api/api-test", tags=["api-test"])
router.include_router(import_.router)
router.include_router(definitions.router)
router.include_router(generate.router)
router.include_router(debug.router)
router.include_router(datasets.router)
router.include_router(scenarios.router)
router.include_router(overview.router)
router.include_router(mocks.router)
router.include_router(files.router)
router.include_router(tokens_router.router)


@router.get("/health")
async def api_test_health() -> dict:
    """注册探针：确认路由聚合已挂载（T005 验证用）。"""
    return {"status": "ok", "module": "api-test"}
