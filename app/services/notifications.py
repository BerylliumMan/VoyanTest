"""通知创建辅助函数。"""
import asyncio
import logging
import time

from app.database import AsyncSessionLocal
from app import db_models
from app.crud.run import get_run_batch

logger = logging.getLogger(__name__)


async def notify_batch_completed(
    batch_id: int,
    user_id: int,
    *,
    max_wait_seconds: int = 7200,
    poll_interval: float = 2.0,
) -> None:
    """批次运行完成后创建通知。

    若调用时批次仍在 running/pending，会轮询等待 ``finished_at`` 后再发通知，
    避免与 BackgroundTasks 并行执行时抢跑。
    """
    try:
        deadline = time.monotonic() + max_wait_seconds
        batch = None
        while time.monotonic() < deadline:
            async with AsyncSessionLocal() as db:
                batch = await get_run_batch(db, batch_id)
                if not batch:
                    return
                if batch.finished_at is not None or (
                    batch.status and batch.status not in ("running", "pending")
                ):
                    # 类型按计数判定（status 字符串各路径写法不统一）：
                    # 有失败→error；全过→success；否则 info。
                    failed = batch.failed or 0
                    passed = batch.passed or 0
                    notif_type = (
                        "error" if failed > 0
                        else ("success" if passed > 0 else "info")
                    )
                    name = batch.name or f"#{batch.id}"
                    title = f"批次「{name}」运行完成"
                    message = f"通过 {batch.passed}/{batch.total_cases}，失败 {batch.failed}"
                    db.add(db_models.Notification(
                        user_id=user_id,
                        type=notif_type,
                        title=title,
                        message=message,
                        batch_id=batch_id,
                    ))
                    await db.commit()
                    return
            await asyncio.sleep(poll_interval)
        logger.warning(
            "批次 %s 在 %ss 内未完成，跳过完成通知", batch_id, max_wait_seconds,
        )
    except Exception:
        logger.warning("创建通知失败", exc_info=True)
