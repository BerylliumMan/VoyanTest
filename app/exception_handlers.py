"""全局异常处理 — 防止未捕获异常泄露堆栈。"""
import logging
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

async def unhandled_exception_handler(request: Request, _exc: Exception) -> JSONResponse:
    """未捕获异常统一返回 500（数据库未配置时返回友好提示）。"""
    msg = str(_exc)
    if "数据库未配置" in msg:
        logger.warning("数据库未配置，拒绝请求 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=503,
            content={"detail": "数据库未配置，请先访问 /setup 页面完成初始化"},
        )
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
