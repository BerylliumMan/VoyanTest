"""结构化日志 — JSON formatter + request_id 上下文传播。"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from traceback import format_tb
from typing import Optional

# ==================== request_id 上下文 ====================
# 每个 HTTP 请求/后台任务通过 ContextVar 传递唯一 ID，
# 日志记录时自动附带，无需在每个 logger.info() 手动传递。

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def set_request_id(rid: str) -> None:
    """设置当前上下文的 request_id。"""
    request_id_var.set(rid)


def get_request_id() -> Optional[str]:
    """获取当前上下文 request_id，供 middleware/extra fields 用。"""
    return request_id_var.get(None)


# ==================== JSON Formatter ====================


class JsonFormatter(logging.Formatter):
    """输出 JSON 行，每行一个完整日志事件。

    字段:
      - ts:       ISO-8601 时间戳 (UTC)
      - level:    ERROR / WARNING / INFO / DEBUG
      - logger:   日志源模块名
      - msg:      格式化后的消息文本
      - request_id: 当前 HTTP 请求 ID（如有）
      - exc_info: 异常详情（如有）
    """

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.fromtimestamp(record.created, tz=timezone.utc)
        event: dict = {
            "ts": now.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # request_id
        rid = get_request_id()
        if rid:
            event["request_id"] = rid

        # 合并 extra fields（调用方通过 extra={...} 传入的非标准字段）
        # 跳过 logging 内置字段 + 不序列化 exc_info/text
        skip_keys = {
            "args", "asctime", "created", "exc_info", "exc_text",
            "filename", "funcName", "levelname", "levelno",
            "lineno", "module", "msecs", "message", "msg",
            "name", "pathname", "process", "processName",
            "relativeCreated", "stack_info", "thread", "threadName",
        }
        for key, value in record.__dict__.items():
            if key not in skip_keys and not key.startswith("_"):
                try:
                    json.dumps(value)
                    event[key] = value
                except (TypeError, ValueError):
                    event[key] = str(value)

        # 异常信息
        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            tb = record.exc_info[2]
            event["exception"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": format_tb(tb) if tb else None,
            }

        return json.dumps(event, ensure_ascii=False, default=str)


# ==================== 日志初始化 ====================


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
) -> None:
    """配置根 logger。

    Parameters
    ----------
    level : str
        日志级别（DEBUG / INFO / WARNING / ERROR）。
    fmt : str
        "json" → JsonFormatter；"text" → 标准可读格式。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 清除已有 handler，再用我们配好的
    root.handlers.clear()
    root.addHandler(handler)

    # uvicorn 的 access log 也走 JSON 格式
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvi = logging.getLogger(name)
        uvi.handlers.clear()
        uvi.addHandler(handler)
        uvi.propagate = False
