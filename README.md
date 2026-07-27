<p align="center">
  <em>用自然语言编写测试，让 AI 驱动浏览器自动执行</em>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"></a>
  <a href="#"><img src="https://img.shields.io/badge/node-18%2B-green" alt="Node.js 18+"></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey" alt="Platform"></a>
</p>

---

VoyanTest 是一个 **AI 驱动的 Web UI 自动化测试平台**。你只需用**中文自然语言**描述测试步骤，LLM 自动将其翻译为 Playwright MCP 指令并驱动真实浏览器执行，全程自动截图验证预期结果。

```
"点击登录按钮，输入用户名和密码，验证跳转到主页"
  ↓ LLM 翻译
Playwright: click #login-btn → fill #username → fill #password → click #submit → assert URL
```

## ✨ 特性

### 测试执行
- **🗣️ 自然语言驱动**：用中文写「点击登录按钮」「验证页面标题」，LLM 翻译为 Playwright 操作
- **🖥️ Real Browser**：通过 `@playwright/mcp` 控制 Chromium，支持全部浏览器操作
- **🐛 交互式调试**：失败时暂停执行，实时查看浏览器状态，手动选择重试/跳过/编辑步骤继续
- **🔧 自动重试**：步骤级配置重试次数和间隔，自动处理 flaky 测试
- **✅ 步骤断言**：5 种断言类型（URL/文本/元素可见/输入值/元素数量），中文自然语言配置
- **🧠 自愈选择器**：元素定位失败时 AI 自动分析 DOM 找到替代选择器，减少人工维护
- **📹 CDP 录制**：录制真实浏览器操作 → 转换为测试步骤 → 保存为用例 → 一键回放
- **📊 xlsx 导入导出**：测试用例支持 Excel 批量导入导出
- **📈 趋势图**：Dashboard 7 日通过/失败堆叠柱状图（ECharts）
- **🔔 通知中心**：批次运行完成自动通知，支持铃铛图标查看
- **🔑 API Key**：个人 API 密钥管理，支持 CLI/第三方集成
- **🛡️ CSRF 防护**：Double Submit Cookie 模式，所有写入请求自动校验

### AI 驱动
- **📝 AI 用例生成**：上传需求文档（docx/pdf/md/图片），两阶段流水线——先提取细粒度**测试项**，再按项生成功能用例或 UI 自动化用例
- **📄 多模态文档**：docx 按文档顺序保留文字与内嵌图；扫描版 PDF 可按页理解
- **📚 智能分段**：长文档按章节优先切分，超长章节再按上下文窗口约 80% 二次分段，续篇带模块衔接；图片尽量与说明文字同段
- **🤖 生成 Agent**：可选功能用例 / UI 自动化生成助手；提示词模板可在设置中管理，启动时与种子内容同步
- **🗂️ 生成记录**：查看历史、停止分析中任务；仅完成 / 失败 / 已停止后可删除；支持预览、导入与 xlsx 导出
- **🔍 预期结果验证**：执行后自动截图，LLM 比对截图判断预期结果是否达成
- **📋 执行计划预览**：执行前可视化展示 LLM 对每步的理解和计划操作

### 平台能力
- **🔐 项目级权限**：admin 全项目，tester 可限制到指定项目，多团队安全隔离
- **📊 测试报告**：批次聚合报告、趋势分析、统计大盘、JSON 导出
- **🌐 分布式执行**：Agent 机制将测试分发到远程机器并行执行
- **🖥️ CLI 工具**：`voyan run` 命令行执行，支持 CI/CD 流水线集成，退出码标准
- **🌗 深色主题**：亮色/暗色主题切换
- **🎬 CDP 录制回放**：录制真实浏览器操作，一键转换为可执行的测试步骤

## 🚀 快速开始

### 方式一：Docker Compose（推荐）

```bash
cd VoyanTest
docker compose up -d
```

浏览器打开 `http://localhost:8002/`，首次使用需通过 `/setup` 页面配置 PostgreSQL 数据库。

> Docker Compose 已自动配置持久化卷，数据位于：
> - `voyantest-data` — 数据库配置
> - `voyantest-reports` — 测试报告与截图
> - `voyantest-logs` — 应用日志

### 方式二：Docker 镜像

```bash
# 1. 加载镜像
gunzip -c voyantest-docker.tar.gz | docker load

# 2a. 创建持久化卷（推荐）——数据不会随容器删除而丢失
docker volume create voyantest-data  # 数据库配置
docker volume create voyantest-reports  # 测试报告与截图
docker volume create voyantest-logs  # 应用日志

# 3a. 启动（使用持久化卷）
docker run -d -p 8002:8002 \
  --name voyantest \
  -e SESSION_SECRET_KEY="your-secret-key" \
  -v voyantest-data:/app/data \
  -v voyantest-reports:/app/reports \
  -v voyantest-logs:/app/logs \
  voyantest:latest

# 访问 http://localhost:8002/setup 填写 PG 连接

# --- 或者使用绑定挂载（无需先创建卷）---
# 2b. 创建本地目录
mkdir -p ./voyantest-data ./voyantest-reports ./voyantest-logs

# 3b. 启动（使用绑定挂载）
docker run -d -p 8002:8002 \
  --name voyantest \
  -e SESSION_SECRET_KEY="your-secret-key" \
  -v $(pwd)/voyantest-data:/app/data \
  -v $(pwd)/voyantest-reports:/app/reports \
  -v $(pwd)/voyantest-logs:/app/logs \
  voyantest:latest
```

> **持久化说明**：容器删除后，持久化卷或本地目录中的数据仍会保留。
> 重新创建容器时挂载相同卷/目录即可恢复配置、报告和日志。

### 方式三：源码启动

```bash
# Linux
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cd frontend && npm install && npm run build && cd ..
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

```powershell
# Windows
python -m venv myenv && myenv\Scripts\activate
pip install -r requirements_win.txt
playwright install chromium
cd frontend && npm install --ignore-scripts && npm run build && cd ..
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

浏览器打开 `http://localhost:8002/`，默认管理员 `admin / Admin@2024`。

## 🧭 主流程

### 1. 登录

输入用户名密码（默认 `admin / Admin@2024`），进入仪表盘。

| 登录 | 仪表盘 |
|------|--------|
| ![登录](docs/images/01_login.png) | ![仪表盘](docs/images/02_dashboard.png) |

### 2. 创建项目 → 编写用例 → 执行测试

```
登录 → 创建项目 → 添加模块 → 编写测试用例 → 执行测试 → 查看报告
                  ↘ AI 生成 ↑                   ↘ Agent 远程执行
```

**两种方式编写用例：**
- **手动** — 在「测试用例」页逐条创建，自然语言描述步骤和预期结果
- **AI 生成** — 上传需求文档（docx/pdf/md/图片），AI 提取测试项并生成用例（每测试项覆盖正常 / 异常 / 边界）

| 测试用例管理 | 执行报告 |
|-------------|---------|
| ![测试用例](docs/images/05_testcases.png) | ![报告](docs/images/06_reports.png) |

### 3. CDP 录制回放

在「录制回放」页录制真实浏览器操作，自动转换为可执行的测试步骤。支持**保存到用例库**、**历史录制管理**和**一键回放**：

| 录制控制 | 事件与转换 |
|---------|-----------|
| ![录制](docs/images/03_recordings.png) | 输入 URL → 开始录制 → 操作浏览器 → 停止 → 转换为测试步骤 → 保存/回放 |

### 4. AI 用例生成

上传需求文档（docx/pdf/md/图片），选择生成 Agent 后开始分析：

1. **解析文档** — 多文件合并；docx 保留图文顺序  
2. **提取测试项** — 按章节 / Token 预算分段提取并合并去重  
3. **生成用例** — 按测试项分批生成（功能用例或 UI 自动化）；分析中可在「生成记录」停止  
4. **预览导入** — 勾选后导入到目标项目；记录可导出 xlsx  

执行前需在「系统设置 → AI 模型配置」填写 LLM（含上下文窗口），并在「提示词 / Agent」中确认生成相关模板已激活。

| AI 生成页面 |
|------------|
| ![AI生成](docs/images/07_gen.png) |

### 5. AI 配置

执行与生成前需在「系统设置 → AI 模型配置」填写 LLM 信息（支持 OpenAI 及兼容 API）。新部署启动时会自动补种默认 Agent 与提示词；若库内活跃提示词与种子不一致，会创建新版本并激活。

| AI 配置 |
|---------|
| ![设置](docs/images/08_settings.png) |

### 分布式 Agent

将测试分发到远程机器执行：

```powershell
# 方式一：Python 源码
$env:PLAYWRIGHT_BROWSERS_PATH = "$env:USERPROFILE\AppData\Local\ms-playwright"
python agent/client.py --server http://<服务端IP>:8002

# 方式二：编译版
.\agent\dist\VoyanTest-Agent.exe
```

按提示输入服务端地址，Agent 会自动连接并等待测试任务。

## 📖 工作流程

```
登录 → 创建项目 → 添加模块 → 编写测试用例 → 执行测试 → 查看报告
                      ↘  AI 生成 ↑                   ↓
                        上传文档 → 预览/编辑 → 导入    Agent 远程执行
```

**两种编写方式：**
1. **手动** — 逐条创建，每用例包含步骤（自然语言）和预期结果
2. **AI 生成** — 上传需求文档，提取测试项并生成用例，预览编辑后批量导入；生成记录支持停止分析与完成后的删除 / 导出

> [!TIP]
> 离线环境部署：从 GitHub Releases 下载离线包，见 [DEPLOYMENT.md](DEPLOYMENT.md)

执行前需在「设置 → AI 配置」填写 LLM 信息（支持 OpenAI 及兼容 API）。

## 🏗️ 架构

```mermaid
flowchart LR
    subgraph Browser["浏览器"]
        CH[Chromium<br/>截图 操作]
        CDP[CDP 录制<br/>事件捕获]
    end
    subgraph Backend["后端 FastAPI"]
        direction TB
        LLM[LLM 引擎]
        Runner[测试执行器]
        Healing[自愈选择器]
        Assert[断言引擎]
        CDPConv[CDP 转换器]
        Report[报告生成]
    end
    subgraph Agent["分布式 Agent"]
        AC[Agent 客户端]
    end
    subgraph CLI["CLI 工具"]
        VY[voyan CLI]
    end

    CH <-->|Playwright MCP| Backend
    CDP -->|CDP Protocol| CDPConv
    Backend --> DB[(PostgreSQL)]
    Backend --> UI[Web 界面<br/>React + Arco]
    Backend <-->|WebSocket<br/>调试协议| UI
    Backend <-->|WebSocket| AC
    CLI -->|调用| Backend
```

## 🧪 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | FastAPI + SQLAlchemy async + PostgreSQL 16 |
| 浏览器自动化 | Playwright MCP |
| AI/LLM | OpenAI 兼容 API（生成 / 执行 / 录制 Agent） |
| 前端 | React 18 + Arco Design Pro + Vite + ECharts |
| 实时通信 | WebSocket（执行日志 + 调试协议 + Agent） |
| 分布式 | WebSocket + 自定义 Agent 协议 |
| CLI | argparse + asyncio（退出码标准） |

## 📦 项目结构

```
VoyanTest/
├── app/              # FastAPI 后端
│   ├── gen/          # AI 生成引擎（分段 / 提示词 / Pipeline）
│   ├── middleware/    # 中间件（CSRF 等）
│   ├── models/       # 领域模型（auth / project / testcase / batch / recording / notification 等）
│   ├── services/     # 服务层（通知、报告等）
│   ├── seed_defaults.py  # 默认 Agent / 提示词种子与同步
│   └── routers/      # API 路由（含 gen 上传/状态/历史、run-debug、录制、通知）
├── core/             # 执行引擎
│   ├── runner/            # 测试执行器（含重试/暂停/自愈）
│   ├── assertions.py      # 步骤断言引擎（5 种类型）
│   ├── self_healing.py    # AI 自愈选择器
│   ├── llm_wrapper.py     # LLM 客户端封装
│   ├── step_executor.py   # MCP 步骤执行
│   ├── cdp_session.py     # CDP 录制会话引擎
│   └── cdp_converter.py   # CDP 事件→测试步骤转换
├── frontend/         # React 前端
│   └── src/pages/
│       ├── gen/           # AI 用例生成
│       ├── gen-history/   # 生成记录（停止 / 删除限制）
│       ├── recordings/    # CDP 录制回放
│       ├── run-debug/     # 交互式调试执行
│       ├── testcases/     # 用例管理
│       ├── reports/       # 报告
│       ├── agents/        # Agent 管理
│       └── settings/      # 系统设置（AI / 提示词 / Agent）
├── agent/            # 分布式 Agent 客户端
├── voyan_cli.py      # CLI（voyan run / list / run-single）
├── tests/            # 单元 + 契约 + E2E 测试
├── reports/          # 测试报告与截图
└── docs/             # 文档
```

## 📚 文档

- API 文档：启动后访问 `/docs`（Swagger）
- 离线部署：见 [DEPLOYMENT.md](DEPLOYMENT.md)
- 英文说明：[README.en.md](README.en.md)
- 数据库：生产以 PostgreSQL 为主；启动时自动补齐缺失列 / 默认种子（无强制 Alembic）

## 📄 许可证

MIT
