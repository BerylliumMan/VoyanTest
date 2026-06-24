"""
定时任务调度模块
支持 Cron 表达式定时执行测试
"""

import asyncio
import logging
from datetime import datetime
from app.tz import now as tz_now
from typing import Optional, Callable, Dict, List
from dataclasses import dataclass, field

from croniter import croniter

logger = logging.getLogger(__name__)


@dataclass
class ScheduledTask:
    """定时任务定义"""
    id: str
    name: str
    cron_expression: str
    task_type: str  # testcase, module, project
    target_id: int
    enabled: bool = True
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0
    fail_count: int = 0
    created_at: datetime = field(default_factory=tz_now)
    
    def calculate_next_run(self, base_time: datetime = None) -> Optional[datetime]:
        """计算下次执行时间"""
        try:
            itr = croniter(self.cron_expression, base_time or tz_now())
            return itr.get_next(datetime)
        except ValueError:
            logger.exception("计算下次执行时间失败")
            return None


class TaskScheduler:
    """定时任务调度器"""
    
    def __init__(self):
        self.tasks: Dict[str, ScheduledTask] = {}
        self._running = False
        self._task_handles: Dict[str, asyncio.Task] = {}
        self._check_interval = 60  # 检查间隔（秒）
        self._executor: Optional[Callable] = None
        self._lock = asyncio.Lock()
    
    def set_executor(self, executor: Callable):
        """设置任务执行器"""
        self._executor = executor
    
    async def add_task(self, task_id: str, name: str, cron_expression: str,
                 task_type: str, target_id: int, enabled: bool = True) -> ScheduledTask:
        """
        添加定时任务

        Args:
            task_id: 任务唯一标识
            name: 任务名称
            cron_expression: Cron 表达式，如 "0 9 * * 1-5"（工作日早上9点）
            task_type: 任务类型 (testcase/module/project)
            target_id: 目标ID
            enabled: 是否启用
            
        Returns:
            ScheduledTask: 创建的任务
        """
        task = ScheduledTask(
            id=task_id,
            name=name,
            cron_expression=cron_expression,
            task_type=task_type,
            target_id=target_id,
            enabled=enabled
        )
        
        # 计算下次执行时间
        task.next_run = task.calculate_next_run()
        
        self.tasks[task_id] = task
        logger.info("添加定时任务: %s (%s), 下次执行: %s", name, cron_expression, task.next_run)
        
        return task
    
    async def remove_task(self, task_id: str) -> bool:
        """移除定时任务"""
        async with self._lock:
            if task_id in self.tasks:
                if task_id in self._task_handles:
                    self._task_handles[task_id].cancel()
                    del self._task_handles[task_id]
                del self.tasks[task_id]
                logger.info("移除定时任务: %s", task_id)
                return True
        return False
    
    async def enable_task(self, task_id: str) -> bool:
        """启用任务"""
        async with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].enabled = True
                self.tasks[task_id].next_run = self.tasks[task_id].calculate_next_run()
                logger.info("启用定时任务: %s", task_id)
                return True
        return False
    
    async def disable_task(self, task_id: str) -> bool:
        """禁用任务"""
        async with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].enabled = False
                if task_id in self._task_handles:
                    self._task_handles[task_id].cancel()
                    del self._task_handles[task_id]
                logger.info("禁用定时任务: %s", task_id)
                return True
        return False
    
    async def update_task_cron(self, task_id: str, cron_expression: str) -> bool:
        """更新任务的 Cron 表达式"""
        async with self._lock:
            if task_id in self.tasks:
                task = self.tasks[task_id]
                task.cron_expression = cron_expression
                task.next_run = task.calculate_next_run()
                logger.info("更新任务 %s 的 Cron 表达式: %s, 下次执行: %s", task_id, cron_expression, task.next_run)
                return True
        return False
    
    async def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """获取任务信息"""
        async with self._lock:
            return self.tasks.get(task_id)
    
    async def get_all_tasks(self) -> List[ScheduledTask]:
        """获取所有任务"""
        async with self._lock:
            return list(self.tasks.values())
    
    async def get_enabled_tasks(self) -> List[ScheduledTask]:
        """获取所有启用的任务"""
        async with self._lock:
            return [t for t in self.tasks.values() if t.enabled]
    
    async def start(self):
        """启动调度器"""
        if self._running:
            logger.warning("调度器已在运行")
            return
        
        self._running = True
        logger.info("定时任务调度器已启动")
        
        # 启动所有启用的任务
        enabled = await self.get_enabled_tasks()
        for task in enabled:
            handle = asyncio.create_task(self._schedule_task(task), name=f"sched-{task.id}")
            self._task_handles[task.id] = handle
        
        # 启动检查循环
        while self._running:
            await asyncio.sleep(self._check_interval)
    
    async def stop(self):
        """停止调度器"""
        self._running = False
        
        # 取消所有任务
        for handle in self._task_handles.values():
            handle.cancel()
        self._task_handles.clear()
        
        logger.info("定时任务调度器已停止")
    
    async def _schedule_task(self, task: ScheduledTask):
        """调度单个任务"""
        while self._running and task.enabled:
            if not task.next_run:
                task.next_run = task.calculate_next_run()
            
            if not task.next_run:
                logger.error("任务 %s 无法计算下次执行时间", task.id)
                break
            
            # 计算等待时间
            now = tz_now()
            wait_seconds = (task.next_run - now).total_seconds()
            
            if wait_seconds > 0:
                logger.debug("任务 %s 将在 %.0f 秒后执行", task.id, wait_seconds)
                try:
                    await asyncio.sleep(wait_seconds)
                except asyncio.CancelledError:
                    break
            
            # 执行任务
            if self._running and task.enabled:
                await self._execute_task(task)
                
                # 更新执行统计
                task.last_run = tz_now()
                task.run_count += 1
                
                # 计算下次执行时间
                task.next_run = task.calculate_next_run()
    
    async def _execute_task(self, task: ScheduledTask):
        """执行定时任务"""
        logger.info("执行定时任务: %s (%s:%s)", task.name, task.task_type, task.target_id)
        
        if not self._executor:
            logger.error("未设置任务执行器")
            return
        
        try:
            await self._executor(task)
        except Exception:  # noqa: BLE001 - executor 为任意 callable，必须吞掉所有异常以保持调度循环健壮
            logger.exception("定时任务执行失败")
            task.fail_count += 1
    
    async def run_task_now(self, task_id: str) -> bool:
        """立即执行指定任务"""
        task = await self.get_task(task_id)
        if not task:
            return False
        
        handle = asyncio.create_task(self._execute_task(task), name=f"sched-exec-{task_id}")
        self._task_handles[task_id] = handle
        handle.add_done_callback(lambda _: self._task_handles.pop(task_id, None))
        return True


# 常用 Cron 表达式示例
CRON_EXAMPLES = {
    "每小时": "0 * * * *",
    "每天凌晨2点": "0 2 * * *",
    "工作日早上9点": "0 9 * * 1-5",
    "每周一早上8点": "0 8 * * 1",
    "每月1号凌晨3点": "0 3 1 * *",
    "每15分钟": "*/15 * * * *",
    "每30分钟": "*/30 * * * *",
    "每天早上8点和晚上8点": "0 8,20 * * *",
}


def validate_cron_expression(expression: str) -> bool:
    """验证 Cron 表达式是否有效"""
    try:
        croniter(expression)
        return True
    except ValueError:
        return False


def get_next_run_times(expression: str, count: int = 5) -> List[datetime]:
    """获取接下来几次的执行时间"""
    try:
        itr = croniter(expression, tz_now())
        return [itr.get_next(datetime) for _ in range(count)]
    except ValueError:
        return []


# 全局调度器实例
scheduler = TaskScheduler()


async def start_scheduler():
    """启动全局调度器"""
    await scheduler.start()


async def stop_scheduler():
    """停止全局调度器"""
    await scheduler.stop()
