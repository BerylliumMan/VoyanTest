"""execution 包 — 聚合 server + client 端点 + 公共 schemas。

拆分前是单个 execution.py，509 行混合 7 个端点。
拆分后:
    _schemas.py  Pydantic 模型
    _server.py   服务端执行（5 个端点，浏览器在 server）
    _client.py   客户端 Agent 执行（2 个端点，浏览器在远端 Agent）
    __init__.py  聚合 router + 向后兼容 re-export + runner 入口转发

外部调用方式不变:
    from app.routers.testcase import execution
    execution.router                # FastAPI 路由
    execution.BatchRunRequest       # 测试 monkeypatch 用
    execution.run_test_case_endpoint  # 测试 monkeypatch 用

关于 runner 函数（run_test_case / run_batch_test_cases / run_test_case_in_browser）:
这些从 core.runner 导入并在此 re-export，目的是让 _server.py 通过包查找调用。
测试用 monkeypatch.setattr(execution, "run_test_case", fake) 时，包级属性被替换，
_server.py 内 `_exec.run_test_case(...)` 通过包查找能拿到 fake 函数。
"""
from fastapi import APIRouter

from ._schemas import (
    BatchCaseIdsRequest,
    BatchRunRequest,
    DebugRunRequest,
)
from ._server import (
    _run_debug_mode,
    batch_run_cases,
    run_module_test_cases,
    run_project_test_cases,
    run_test_case_debug,
    run_test_case_endpoint,
)
from ._client import (
    _os as _client_os,  # 客户端 Agent 执行路径会调 os.path.join
    batch_run_client,
    run_test_case_on_client,
)

# 提供 _os 给测试 monkeypatch 访问（_client.py 内的 _os 命名）
_os = _client_os

# Runner 入口转发 — 让 monkeypatch(execution, name) 生效
# 见本模块 docstring 关于延迟导入的说明
from core.runner import (  # noqa: E402
    run_batch_test_cases,
    run_test_case,
    run_test_case_in_browser,
)
from core.runner import save_run_results  # noqa: E402

# 聚合 server + client 路由到同一个 router
router = APIRouter()
router.include_router(_server.router)
router.include_router(_client.router)

__all__ = [
    "router",
    # Schemas
    "BatchRunRequest",
    "BatchCaseIdsRequest",
    "DebugRunRequest",
    # Server endpoints
    "run_test_case_endpoint",
    "run_test_case_debug",
    "run_module_test_cases",
    "run_project_test_cases",
    "batch_run_cases",
    # Client endpoints
    "run_test_case_on_client",
    "batch_run_client",
    # Runner entry points (re-exported for monkeypatch compatibility)
    "run_test_case",
    "run_test_case_in_browser",
    "run_batch_test_cases",
    "save_run_results",
    # Helpers
    "_run_debug_mode",
]
