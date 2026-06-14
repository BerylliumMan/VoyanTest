"""
WebSocket 模块
支持实时日志推送
"""

import json
import logging
from typing import Dict, Set
from app.tz import now as tz_now
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class LogWebSocketManager:
    """日志 WebSocket 管理器"""
    
    def __init__(self):
        # 存储活跃的连接：run_id -> Set[WebSocket]
        self.active_connections: Dict[int, Set[WebSocket]] = {}
        
    async def connect(self, websocket: WebSocket, run_id: int):
        """建立 WebSocket 连接"""
        await websocket.accept()
        
        if run_id not in self.active_connections:
            self.active_connections[run_id] = set()
        
        self.active_connections[run_id].add(websocket)
        
        # 发送连接成功消息
        await self.send_message(run_id, {
            "type": "connected",
            "timestamp": tz_now().isoformat(),
            "message": "WebSocket 连接成功"
        })
    
    def disconnect(self, websocket: WebSocket, run_id: int):
        """断开 WebSocket 连接"""
        if run_id in self.active_connections:
            self.active_connections[run_id].discard(websocket)
            
            # 如果没有连接了，清理
            if not self.active_connections[run_id]:
                del self.active_connections[run_id]
    
    async def send_message(self, run_id: int, message: dict):
        """向指定 run_id 的所有连接发送消息"""
        if run_id not in self.active_connections:
            return
        
        # 转换为 JSON
        message_json = json.dumps(message, ensure_ascii=False)
        
        # 发送给所有连接
        disconnected = set()
        for connection in self.active_connections[run_id]:
            try:
                await connection.send_text(message_json)
            except Exception:
                logger.warning("WebSocket 发送失败，标记连接为已断开", exc_info=True)
                disconnected.add(connection)
        
        # 清理断开的连接
        for conn in disconnected:
            self.active_connections[run_id].discard(conn)
        
        # 如果没有连接了，清理
        if not self.active_connections[run_id]:
            del self.active_connections[run_id]
    
    async def broadcast(self, message: dict):
        """广播消息给所有连接"""
        for run_id in list(self.active_connections.keys()):
            await self.send_message(run_id, message)
    
    def get_connection_count(self, run_id: int = None) -> int:
        """获取连接数"""
        if run_id is not None:
            return len(self.active_connections.get(run_id, set()))
        
        return sum(len(connections) for connections in self.active_connections.values())


# 全局管理器实例
log_manager = LogWebSocketManager()


async def websocket_logs(websocket: WebSocket, run_id: int):
    from app.database import SessionLocal
    from app.auth import get_session

    # 从 cookie 读取 session_id（query param 回退已移除）
    session_id = websocket.cookies.get("session_id")
    if not session_id:
        await websocket.close(code=4001, reason="missing session_id")
        return

    db = SessionLocal()
    try:
        session = get_session(db, session_id)
        if not session:
            await websocket.close(code=4003, reason="invalid session")
            return
    finally:
        db.close()

    await log_manager.connect(websocket, run_id)
    
    try:
        while True:
            # 接收客户端消息（心跳或控制命令）
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                
                # 处理心跳
                if message.get('type') == 'ping':
                    await websocket.send_text(json.dumps({
                        "type": "pong",
                        "timestamp": tz_now().isoformat()
                    }))
                
                
            except json.JSONDecodeError:
                logger.warning("收到来自 WebSocket 客户端的非法 JSON 消息")
            
    except WebSocketDisconnect:
        log_manager.disconnect(websocket, run_id)
    except Exception as e:
        logger.exception(f"WebSocket 错误: {e}")
        log_manager.disconnect(websocket, run_id)


class LogBroadcaster:
    """日志广播器 - 用于后端发送日志"""
    
    @staticmethod
    async def log_step_start(run_id: int, step_id: int, step_description: str):
        """发送步骤开始日志"""
        await log_manager.send_message(run_id, {
            "type": "step_start",
            "timestamp": tz_now().isoformat(),
            "step_id": step_id,
            "message": f"开始执行: {step_description}"
        })
    
    @staticmethod
    async def log_step_complete(run_id: int, step_id: int, status: str, duration: float):
        """发送步骤完成日志"""
        await log_manager.send_message(run_id, {
            "type": "step_complete",
            "timestamp": tz_now().isoformat(),
            "step_id": step_id,
            "status": status,
            "duration": duration,
            "message": f"步骤执行完成，状态: {status}，耗时: {duration:.2f}s"
        })
    
    @staticmethod
    async def log_info(run_id: int, message: str, step_id: int = None):
        """发送信息日志"""
        await log_manager.send_message(run_id, {
            "type": "log",
            "level": "INFO",
            "timestamp": tz_now().isoformat(),
            "step_id": step_id,
            "message": message
        })
    
    @staticmethod
    async def log_warning(run_id: int, message: str, step_id: int = None):
        """发送警告日志"""
        await log_manager.send_message(run_id, {
            "type": "log",
            "level": "WARNING",
            "timestamp": tz_now().isoformat(),
            "step_id": step_id,
            "message": message
        })
    
    @staticmethod
    async def log_error(run_id: int, message: str, step_id: int = None):
        """发送错误日志"""
        await log_manager.send_message(run_id, {
            "type": "log",
            "level": "ERROR",
            "timestamp": tz_now().isoformat(),
            "step_id": step_id,
            "message": message
        })
    
    @staticmethod
    async def log_screenshot(run_id: int, screenshot_path: str, step_id: int = None):
        """发送截图信息"""
        await log_manager.send_message(run_id, {
            "type": "screenshot",
            "timestamp": tz_now().isoformat(),
            "step_id": step_id,
            "screenshot_path": screenshot_path,
            "message": "截图已保存"
        })
    
    @staticmethod
    async def log_run_complete(run_id: int, status: str, total_duration: float):
        """发送测试完成日志"""
        await log_manager.send_message(run_id, {
            "type": "run_complete",
            "timestamp": tz_now().isoformat(),
            "status": status,
            "total_duration": total_duration,
            "message": f"测试执行完成，状态: {status}，总耗时: {total_duration:.2f}s"
        })


# 便捷函数
async def broadcast_log(run_id: int, level: str, message: str, **kwargs):
    """便捷函数：发送日志"""
    log_data = {
        "type": "log",
        "level": level.upper(),
        "timestamp": tz_now().isoformat(),
        "message": message,
        **kwargs
    }
    await log_manager.send_message(run_id, log_data)
