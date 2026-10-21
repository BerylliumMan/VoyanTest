"""038 US3：批次停止控制（进程内标志 + DB 轮询）。

执行循环（AgentBridge / AgentRunner）通过 `BatchStopWatcher.should_stop()`
在每个决策点检查停止。停止的单一事实源是 `run_batches.status`：
- 同进程：`app.execution_control.is_stopped()` 立即生效；
- 跨 worker / 对话取消：按 `exec_stop_poll_ms` 轮询数据库，避免每个动作一次查询。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

STOPPED_BATCH_STATUSES = frozenset({"stopping", "stopped", "cancelled"})


class BatchStopWatcher:
    """轻量停止观察器（每实例对应一次 run）。"""

    def __init__(self, db, batch_id: int | None, *, poll_ms: int | None = None):
        self._db = db
        self._batch_id = int(batch_id) if batch_id else None
        if poll_ms is None:
            try:
                from app.config import get_settings

                poll_ms = int(getattr(get_settings(), "exec_stop_poll_ms", 1000) or 1000)
            except Exception:
                poll_ms = 1000
        self._poll_s = max(0.1, float(poll_ms) / 1000.0)
        self._last_check = 0.0
        self._stopped = False

    @property
    def active(self) -> bool:
        return self._batch_id is not None

    async def should_stop(self) -> bool:
        if not self._batch_id:
            return False
        if self._stopped:
            return True
        # 同进程快速路径（暂停/停止端点会写内存标志）
        try:
            from app import execution_control

            if execution_control.is_stopped(self._batch_id):
                self._stopped = True
                return True
        except Exception:
            logger.debug("stop flag check failed", exc_info=True)
        now = time.monotonic()
        if now - self._last_check < self._poll_s:
            return False
        self._last_check = now
        try:
            from sqlalchemy import select

            from app.models.batch import RunBatch

            res = await self._db.execute(
                select(RunBatch.status).where(RunBatch.id == self._batch_id)
            )
            status = res.scalar_one_or_none()
        except Exception:
            logger.debug("batch stop poll failed", exc_info=True)
            return False
        if status in STOPPED_BATCH_STATUSES:
            self._stopped = True
        return self._stopped
