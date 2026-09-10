"""Rate limiter for API endpoints using slowapi."""
from __future__ import annotations

import logging
import os
import warnings

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

_DEFAULT_LIMITS = ["60/minute"]


def _build_limiter() -> Limiter:
    """构造限流器；容忍「.env 存在但当前用户无读权限」。

    slowapi 构造时通过 ``starlette.config.Config(".env")`` 读取 ``RATELIMIT_*``，
    而 starlette 的 ``_read_file`` 不捕获 ``PermissionError`` —— 只要 ``.env``
    **存在**（``os.path.isfile`` 为真）却不可读，导入本模块即抛异常：

        PermissionError: [Errno 13] Permission denied: '.env'

    典型触发场景：``.env`` 按最小权限设为 0600/0400 且属主不是运行用户（容器挂载
    密钥，或测试进程与 .env 属主 uid 不同）。本项目未使用任何 ``RATELIMIT_*``
    键，故这种情况降级为「不读 .env」即可，只告警提示，不该让进程起不来。
    """
    try:
        return Limiter(key_func=get_remote_address, default_limits=_DEFAULT_LIMITS)
    except PermissionError:
        logger.warning(
            "无法读取 .env（文件存在但当前用户无读权限），已跳过从 .env 读取限流参数；"
            "如需使用 RATELIMIT_* 配置，请让运行用户对 .env 可读。"
        )
        with warnings.catch_warnings():
            # starlette 对不存在的 env_file 会发 "Config file ... not found"；
            # 这里是刻意不读，用上面的日志替代，避免误导读日志的人。
            warnings.filterwarnings("ignore", message="Config file .* not found")
            return Limiter(
                key_func=get_remote_address,
                default_limits=_DEFAULT_LIMITS,
                config_filename=os.devnull,
            )


limiter = _build_limiter()
