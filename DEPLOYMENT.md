# 部署指南

## 系统要求

- **操作系统**: Linux (Ubuntu 20.04+), macOS, Windows WSL2
- **Python**: 3.11+
- **Node.js**: 18+（MCP 服务器通过 `npx` 运行）
- **内存**: 至少 4GB RAM
- **磁盘**: 至少 10GB 可用空间

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/BerylliumMan/VoyanTest.git
cd VoyanTest
```

### 2. 安装依赖

```bash
# Python 依赖
pip install -r requirements.txt

# Windows 环境使用
pip install -r requirements_win.txt

# Playwright 浏览器（首次）
playwright install chromium
```

### 3. 构建前端

```bash
cd frontend && npm install && npm run build && cd ..
```

### 4. 配置

创建 `.env` 文件（可选，项目有合理默认值）：

```env
DATABASE_URL=sqlite:///./uitest.db
APP_HOST=0.0.0.0
APP_PORT=8002
SESSION_EXPIRE_MINUTES=30
MAX_LOGIN_ATTEMPTS=5
LOCK_DURATION_MINUTES=15
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=Admin@2024
```

### 5. 启动

```bash
# 开发模式（含热重载）
python3 app/main.py
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

浏览器访问 `http://localhost:8002/`。

**默认管理员**：`admin` / `Admin@2024`（首次登录后请修改密码）

## 生产部署

### uvicorn 生产模式

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8002 --workers 4
```

### systemd 服务

```ini
# /etc/systemd/system/voyantest.service
[Unit]
Description=VoyanTest - AI-Driven UI Testing Platform
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/VoyanTest
ExecStart=/opt/VoyanTest/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8002 --workers 4
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable voyantest
sudo systemctl start voyantest
```

### Nginx 反向代理

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> **注意**：WebSocket 实时日志需要 `proxy_set_header Upgrade` 和 `Connection "upgrade"`。

## 配置说明

### LLM 配置

LLM 配置存储在数据库中，**不从环境变量或配置文件读取**。

**配置入口**：
- Web 管理界面 → 设置 → AI 配置
- API：`GET /api/config/ai`（读取）/ `PUT /api/config/ai`（更新）

**首次启动迁移**：若 `ai_configs` 表为空且磁盘存在 `config.json`，会自动迁移一次。迁移后 `config.json` 保留作为墓碑，不再被代码读取。

**Fail-fast**：若数据库缺少 `model` 或 `api_key`，启动直接报错，无默认值或环境变量回退。

### 数据库

项目使用 SQLite 作为默认数据库，零配置即可运行。数据库文件 `uitest.db` 自动创建在项目根目录。

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | 数据库连接字符串 | `sqlite:///./uitest.db` |
| `APP_HOST` | 监听地址 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8002` |
| `SESSION_EXPIRE_MINUTES` | 会话过期时间 | `30` |
| `MAX_LOGIN_ATTEMPTS` | 最大登录尝试次数 | `5` |
| `LOCK_DURATION_MINUTES` | 账号锁定时间 | `15` |
| `DEFAULT_ADMIN_USERNAME` | 默认管理员用户名 | `admin` |
| `DEFAULT_ADMIN_PASSWORD` | 默认管理员密码 | `Admin@2024` |

## 项目结构

```
VoyanTest/
├── app/                        # FastAPI 应用
│   ├── main.py                 # 入口（uvicorn 启动）
│   ├── config.py               # 统一配置（pydantic-settings）
│   ├── database.py             # 数据库连接与会话管理
│   ├── db_models.py            # SQLAlchemy ORM 模型
│   ├── auth.py                 # 认证逻辑（密码哈希、Session 管理）
│   ├── websocket.py            # WebSocket 实时日志推送
│   ├── routers/                # API 路由
│   ├── static/                 # 前端构建产物
│   └── templates/              # SPA 入口页面
├── frontend/                   # React 前端源码
├── core/                       # 核心测试引擎
│   ├── runner.py               # 测试执行引擎
│   ├── llm_wrapper.py          # LLM 适配器
│   ├── playwright_manager.py   # Playwright MCP 管理
│   └── step_executor.py        # 步骤执行器
├── agent/                      # 分布式测试 Agent
├── tests/                      # 测试（unit / contract / e2e）
├── scripts/                    # 工具脚本
├── reports/                    # 测试报告和截图
└── requirements.txt            # Python 依赖
```

## API 概览

| 模块 | 端点 | 说明 |
|------|------|------|
| 项目 | `GET/POST /api/projects/` | 列表 / 创建 |
| 模块 | `GET/POST /api/projects/{id}/modules/` | 项目下模块列表 / 创建 |
| 环境 | `GET/POST /api/projects/{pid}/environments` | 项目下环境列表 / 创建 |
| 测试用例 | `POST /api/testcases/` | 创建（含步骤） |
| | `GET /api/testcases/search` | 搜索用例 |
| | `POST /api/testcases/{id}/run` | 执行单个用例 |
| | `POST /api/testcases/preview-plan` | 预览 AI 执行计划 |
| 报告 | `GET /api/reports/statistics` | 测试统计 |
| | `GET /api/reports/batches` | 批次列表 |
| | `GET /api/reports/batches/{id}` | 批次详情（含 runs + steps） |
| 认证 | `POST /api/auth/login` | 登录 |
| | `POST /api/auth/logout` | 登出 |
| | `GET /api/auth/me` | 当前用户 |
| | `POST /api/auth/change-password` | 修改密码 |
| 用户 | `GET/POST /api/users/` | 用户列表 / 创建 |
| | `PUT /api/users/{id}/reset-password` | 重置密码 |
| Agent | `GET /api/agents` | Agent 列表 |
| | `POST /api/agents/register` | 注册 Agent |
| | `POST /api/agents/{id}/heartbeat` | 心跳 |
| 审计日志 | `GET /api/audit-logs/` | 审计日志列表 |
| 定时任务 | `GET/POST /api/schedules/` | 定时任务列表 / 创建 |
| | `POST /api/schedules/{id}/toggle` | 启用/禁用 |
| AI 配置 | `GET/PUT /api/config/ai` | AI 模型配置 |
| 健康检查 | `GET /health` | 健康检查 |
| WebSocket | `WS /ws/logs/{run_id}?session_id=xxx` | 实时日志推送 |

## 分布式测试（Agent）

远程机器上的 Agent 客户端接收服务器下发的测试指令，在本地通过 Playwright 执行浏览器测试。

### 启动 Agent

```bash
# 1. 安装依赖（仅需 websockets）
pip install websockets

# 2. 安装 MCP 和 Chromium
python3 agent/setup_mcp.py

# 3. 启动
python3 -m agent.client --server ws://<server-ip>:8002 --name "Agent-名称" [--headless]
```

### 离线安装（Windows 无外网机器）

详见 `agent/README.md`。

## 日志

### 执行日志

```
reports/
└── run_{case_id}_{timestamp}/
    ├── run.log          # 完整运行日志
    ├── report.json      # 结构化报告（thinking、action、success、screenshot_path）
    └── screenshots/     # 失败步骤截图
```

### 实时日志

测试运行时，前端通过 WebSocket 实时接收步骤日志：

```
WS /ws/logs/{run_id}?session_id=<your-session-id>
```

消息格式：
```json
{"type": "step_start", "step_id": 1, "message": "开始执行: 点击登录按钮"}
{"type": "step_complete", "step_id": 1, "status": "passed", "duration": 2.5}
{"type": "run_complete", "status": "passed", "total_duration": 15.3}
```

### 应用日志

保存在 `logs/` 目录下。

## 故障排除

### 应用无法启动

```bash
# 检查 Python 版本
python3 --version  # 需要 3.11+

# 检查端口占用
lsof -i :8002

# 重新安装依赖
pip install -r requirements.txt --force-reinstall
```

### 浏览器无法启动

```bash
# 重新安装 Playwright 浏览器
playwright install chromium

# 使用无头模式
# 在测试用例中设置 headless: true
```

### LLM API 调用失败

通过 Web 管理界面 → 设置 → AI 配置检查：
1. Model 是否正确
2. API Key 是否有效
3. API Base URL 是否可访问

### 数据库问题

```bash
# 检查数据库文件权限
ls -la uitest.db

# 修复权限
chmod 666 uitest.db
```

### WebSocket 连接失败

1. 确认已登录（需要 session_id cookie）
2. 确认 Nginx 配置了 `proxy_set_header Upgrade` 和 `Connection "upgrade"`
3. 前端会自动降级到 HTTP polling（每 3 秒）

## 安全建议

1. **修改默认密码**：首次登录后立即修改 admin 密码
2. **HTTPS**：生产环境使用 HTTPS（通过 Nginx）
3. **API 密钥保护**：LLM API Key 存储在数据库中，不暴露给前端
4. **输入验证**：所有用户输入通过 Pydantic 校验
5. **Session 管理**：会话自动过期，失败登录锁定

## 更新维护

```bash
# 更新代码
git pull origin master

# 更新依赖
pip install -r requirements.txt --upgrade

# 重新构建前端
cd frontend && npm install && npm run build && cd ..

# 重启服务
sudo systemctl restart voyantest
```

### 数据库备份

```bash
# 手动备份
cp uitest.db backups/uitest_$(date +%Y%m%d_%H%M%S).db
```

定时备份（crontab）：
```
0 0 * * * cp /opt/VoyanTest/uitest.db /opt/VoyanTest/backups/uitest_$(date +\%Y\%m\%d_\%H\%M\%S).db
```
