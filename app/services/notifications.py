"""通知创建辅助函数。

049 起：批次完成不再直接发通知，而是交给 ``batch_finalize.finalize_batch``
统一做「分类汇总 → 环境重试 → 分级通知」；本模块保留原入口以兼容三个调用点。
"""

import logging

logger = logging.getLogger(__name__)


async def notify_batch_completed(
    batch_id: int,
    user_id: int,
    *,
    max_wait_seconds: int = 7200,
    poll_interval: float = 2.0,
) -> None:
    """批次运行完成后收尾并创建通知（按失败分类决定级别）。"""
    try:
        from app.services.batch_finalize import finalize_batch

        await finalize_batch(
            batch_id,
            user_id,
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
        )
    except Exception:  # noqa: BLE001 - 收尾失败不应影响执行结果
        logger.warning("批次收尾失败 batch=%s", batch_id, exc_info=True)


__all__ = ["notify_batch_completed"]
