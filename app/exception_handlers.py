"""全局异常处理 — 防止未捕获异常泄露堆栈。"""
import logging
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

async def unhandled_exception_handler(request: Request, _exc: Exception) -> JSONResponse:
    """未捕获异常统一返回 500。"""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
