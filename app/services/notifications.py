"""通知创建辅助函数。"""
from app.database import AsyncSessionLocal
from app import db_models
from app.crud.run import get_run_batch


async def notify_batch_completed(batch_id: int, user_id: int) -> None:
    """批次运行完成后创建通知。"""
    try:
        async with AsyncSessionLocal() as db:
            batch = await get_run_batch(db, batch_id)
            if not batch:
                return

            notif_type = "success" if batch.status == "passed" else ("error" if batch.status == "failed" else "info")
            title = f"批次「{batch.name}」运行完成"
            message = f"通过 {batch.passed}/{batch.total_cases}，失败 {batch.failed}"

            db.add(db_models.Notification(
                user_id=user_id,
                type=notif_type,
                title=title,
                message=message,
                batch_id=batch_id,
            ))
            await db.commit()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("创建通知失败", exc_info=True)
