<p align="center">
  <em>用自然语言编写测试，让 AI 驱动浏览器自动执行；成功后固化为 Playwright 脚本，下次零 LLM 直跑</em>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"></a>
  <a href="#"><img src="https://img.shields.io/badge/node-18%2B-green" alt="Node.js 18+"></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey" alt="Platform"></a>
</p>

---

VoyanTest 是一个 **AI 驱动的 Web UI 自动化测试平台**。你用**中文自然语言**描述整案目标与步骤，默认走 **nl_goal**（观察 → 行动 → journal）；成功后用 Playwright codegen 同源算法固化定位，合成可重放脚本入库。下次优先直跑脚本，失败再回退 AI。

```
自然语言用例 / 清单步骤
  ↓ nl_goal（LLM + 快照 / MCP / hybrid）
成功 journal + codegen locator
  ↓ 合成 Playwright async 脚本（校验后入库）
下次：compiled_script 零 LLM 直跑 → 失败再 fall back nl_goal
```

## ✨ 特性

### 测试执行
- **🗣️ 自然语言整案（nl_goal，默认）**：整案目标循环，步骤作 checklist；支持 hybrid（MCP + browser-use）
- **⚡ Playwright 脚本固化**：成功后写入 `compiled_script`；优先 codegen `get_by_*` 定位，无 ephemeral ref
- **🔁 脚本优先 / 失败回退**：有有效脚本则直跑；失败可回退 nl_goal，并按策略清理或重固化
- **🧩 初始化用例**：批量执行可勾选登录等 init 用例，同一浏览器会话复用登录态（后续用例跳过 BASE URL）
- **⏸️ 批量控制**：执行中支持暂停、继续、停止
- **🖥️ Real Browser**：Playwright MCP / 共享 CDP Chromium；客户端 GUI 有头执行
- **🐛 交互式调试**：失败时可暂停，查看浏览器状态后重试 / 跳过 / 改步骤
- **🔧 自动重试**：步骤级重试次数与间隔
- **✅ 步骤断言**：URL / 文本 / 元素可见 / 输入值 / 元素数量等
- **🧠 自愈选择器**：定位失败时 AI 分析 DOM 找替代（逐步路径）
- **📹 CDP 录制**：录制操作 → 转步骤 → 入库 → 回放
- **📊 xlsx 导入导出**：用例批量导入导出
- **📈 趋势图**：Dashboard 近 7 日通过 / 失败（ECharts）
- **🔔 通知中心**：批次完成通知
- **🔑 API Key**：个人密钥，便于 CLI / 第三方
- **🛡️ CSRF**：Double Submit Cookie

### AI 驱动
- **📝 AI 用例生成**：上传需求（docx/pdf/md/图片），先提测试项再生成功能 / UI 用例
- **📄 多模态文档**：docx 图文顺序；扫描 PDF 可按页理解
- **📚 智能分段**：长文档按章节 / Token 预算切分与衔接
- **🤖 生成 Agent**：功能 / UI 助手；提示词可在设置中管理
- **🗂️ 生成记录**：历史、停止、预览导入、xlsx 导出
- **🔍 预期结果验证**：截图 + LLM 比对（配置开启时）
- **📋 执行计划预览**：执行前展示 LLM 对步骤的理解

### 平台能力
- **🔐 项目级权限**：admin / tester 隔离
- **📊 测试报告**：批次报告、趋势、统计、JSON
- **🌐 分布式 Agent**：WebSocket 将任务派到远程机器（GUI / CLI）
- **🖥️ CLI**：`voyan run` 等，便于 CI
- **🌗 深色主题**
- **🎬 CDP 录制回放**

## 🚀 快速开始

### 方式一：Docker Compose（推荐）

```bash
cd VoyanTest
docker compose up -d
```

浏览器打开 `http://localhost:8002/`，首次使用经 `/setup` 配置 PostgreSQL。

> 持久化卷：`voyantest-data`（配置）、`voyantest-reports`（报告）、`voyantest-logs`（日志）

### 方式二：Docker 镜像

```bash
gunzip -c voyantest-docker.tar.gz | docker load

docker volume create voyantest-data
docker volume create voyantest-reports
docker volume create voyantest-logs

docker run -d -p 8002:8002 \
  --name voyantest \
  -e SESSION_SECRET_KEY="your-secret-key" \
  -v voyantest-data:/app/data \
  -v voyantest-reports:/app/reports \
  -v voyantest-logs:/app/logs \
  voyantest:latest
```

访问 `http://localhost:8002/setup` 填写 PG。也可用绑定挂载替代 named volume。

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

默认 `admin / Admin@2024`，进入仪表盘。

### 2. 创建项目 → 编写用例 → 执行测试

```
登录 → 创建项目 → 添加模块 → 编写测试用例 → 客户端 / 服务端执行 → 查看报告
                  ↘ AI 生成 ↑              ↘ 初始化用例 + 批量
```

**编写：**
- **手动** — 自然语言步骤与预期
- **AI 生成** — 上传需求，提取测试项并生成用例

**执行（推荐客户端 Agent）：**
1. 启动 GUI / CLI Agent 并连上服务端  
2. 在用例页批量运行；可勾选**初始化用例**（如登录）保留会话  
3. 首跑多为 **nl_goal**；成功后固化 Playwright 脚本  
4. 再跑优先 **compiled_script**；失败回退 AI  

### 3. CDP 录制回放

「录制回放」页：输入 URL → 录制 → 转换步骤 → 保存 / 回放。

### 4. AI 用例生成

上传需求 → 选 Agent → 提取测试项 → 生成 → 预览导入。需在「系统设置 → AI 模型配置」配置 LLM。

### 5. AI / 执行后端配置

「系统设置」中配置 LLM；执行后端默认 **nl_goal**，可选 `compiled_script` / `legacy_hybrid` / `legacy_mcp` / `browser_use`（见 `data/execution_backend.json`）。

### 分布式 Agent（Windows 客户端）

```powershell
# 源码 CLI
$env:PYTHONPATH = "D:\path\to\VoyanTest"
python -m agent.cli_entry --server ws://<服务端IP>:8002 --name my-agent --username admin --password <密码>

# GUI 打包（推荐）：仓库根目录
.\install_and_build.bat
# 产物：dist\VoyanTest-Agent\VoyanTest-Agent.exe（onedir，启动快）
# 将该目录旁放置 chromium / node.exe / node_modules（与 release\VoyanTest-Agent 布局一致）
```

> 打包会排除 `agent\dist` 旧 exe，避免体积滚到 600MB+。勿再整包嵌入历史产物。

## 📖 工作流程（固化）

```
nl_goal 成功
  → journal.replay（含 playwright_locator / checklist 真值）
  → 模板或 LLM 合成 Playwright 脚本
  →（单用例）无头校验通过才入库；批量 init→main 可跳过阻塞 dry-run 以免拖慢下一例
  → 下次优先 compiled_script；共享 CDP 时保留浏览器会话
```

定位约定：
- 优先 `get_by_placeholder` / `get_by_role` / `get_by_text` / codegen locator  
- 禁止 ephemeral ref（`e12` / `f5e…`）与臆造业务树 CSS  
- 依赖 Playwright **自动等待** + `set_default_timeout`；单位下拉等异步列表用 `expect(...).to_be_attached()` 等列表就绪后再筛选  

离线部署见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 🏗️ 架构

```mermaid
flowchart LR
    subgraph Client["客户端 Agent"]
        GUI[GUI / CLI]
        MCP[Playwright MCP]
        CDP[共享 CDP Chromium]
        PY[compiled_script]
    end
    subgraph Backend["后端 FastAPI"]
        NL[nl_goal 循环]
        SYN[脚本合成 / codegen]
        RUN[执行编排 / 批量]
        HEAL[自愈 / 断言]
        REP[报告]
    end
    subgraph Data["数据"]
        PG[(PostgreSQL)]
    end

    GUI <-->|WebSocket| RUN
    RUN → NL
    NL <--> MCP
    NL <--> CDP
    NL → SYN
    SYN → PG
    RUN → PY
    PY → CDP
    RUN → REP
    Backend → PG
    Backend → UI[Web React]
```

## 🧪 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | FastAPI + SQLAlchemy async + PostgreSQL |
| 浏览器 | Playwright MCP、CDP、Playwright Python（固化脚本） |
| 定位固化 | playwright-selector-generator（IIFE 注入） |
| AI | OpenAI 兼容 API |
| 前端 | React 18 + Arco Design Pro + Vite + ECharts |
| 实时 | WebSocket（执行 / 调试 / Agent） |
| 客户端打包 | PyInstaller onedir（`install_and_build.bat`） |

## 📦 项目结构

```
VoyanTest/
├── app/                 # FastAPI（路由 / 模型 / 生成 / 设置）
├── core/                # 执行与固化
│   ├── goal_agent_loop.py     # nl_goal 决策与 checklist
│   ├── codegen_locator.py     # codegen IIFE 注入 / 解析
│   ├── replay_resolve.py      # journal.replay 合并
│   ├── script_templates.py    # 按步拼 Playwright 脚本
│   ├── script_synthesize.py   # LLM 合成 / 修复
│   ├── compiled_script.py     # 入库 / hash / 清理
│   ├── browser_use_exec.py    # browser-use 回退
│   ├── runner/                # 报告落库等
│   └── assets/codegen_locator.iife.js
├── agent/               # Agent（manager / client_core / GUI）
├── frontend/            # React
├── scripts/
│   ├── build_codegen_iife.mjs # 重建 codegen 资产
│   └── codegen-iife/          # npm 依赖（selector-generator）
├── install_and_build.bat      # Windows Agent GUI 打包
├── voyan_cli.py
├── tests/
└── docs/
```

重建 codegen 资产（Playwright 大版本升级后）：

```bash
node scripts/build_codegen_iife.mjs
```

## 📚 文档

- API：`/docs`（Swagger）
- 离线部署：[DEPLOYMENT.md](DEPLOYMENT.md)
- 英文：[README.en.md](README.en.md)
- 数据库：PostgreSQL；启动时补齐列与种子（无强制 Alembic）

## 📄 许可证

MIT
