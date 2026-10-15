"""035 US4：MCP 服务端路由包（挂载入口在 app/main.py）。"""
from app.routers.mcp.server import (  # noqa: F401
    register_mcp_routes,
    start_mcp_session_manager,
    stop_mcp_session_manager,
)
