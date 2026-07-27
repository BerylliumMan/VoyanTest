<p align="center">
  <em>Write tests in natural language, let AI drive the browser</em>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"></a>
  <a href="#"><img src="https://img.shields.io/badge/node-18%2B-green" alt="Node.js 18+"></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows%20wsl2-lightgrey" alt="Platform"></a>
  <a href="./README.md"><img src="https://img.shields.io/badge/lang-中文-red" alt="中文"></a>
</p>

---

VoyanTest is an **AI-powered Web UI testing platform**. Write test steps in **natural language** (Chinese or English), and the LLM translates them into Playwright MCP commands to drive a real browser — with automatic screenshot verification.

```
"Click the login button, enter username and password, verify redirect to homepage"
  ↓ LLM translates
Playwright: click #login-btn → fill #username → fill #password → click #submit → assert URL
```

## ✨ Features

- **🧠 AI Test Generation**: Upload requirement docs (docx/pdf/md/images); two-phase pipeline extracts fine-grained **test items**, then generates functional or UI-automation cases (normal / exception / boundary)
- **📄 Multimodal docs**: Ordered text + embedded images for docx; chapter-aware chunking (~80% of context window) for long documents
- **🗂️ Generation history**: Stop in-flight analysis; delete only after completed / failed / cancelled; preview, import, xlsx export
- **🗣️ Natural Language Driven**: Write "click login button" in plain language — no Playwright API knowledge needed
- **🖥️ Real Browser**: Controls Chromium via `@playwright/mcp`, supporting navigate, click, fill, screenshot, and more
- **🔍 Expected Result Verification**: Auto-screenshots after execution, LLM compares screenshots to verify results
- **📋 Execution Plan Preview**: Visual preview of LLM's understanding of each step before execution
- **📊 Test Reports**: Detailed logs + step screenshots + summary statistics
- **🌐 Distributed Execution**: Agent mechanism distributes tests to remote machines
- **🔐 Auth & Permissions**: Admin/tester roles, session management, password security
- **🌗 Dark Theme**: Light/dark theme toggle

## 🚀 Quick Start

### Install

```bash
# Linux
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cd frontend && npm install && npm run build && cd ..
```

```powershell
# Windows
python -m venv myenv && myenv\Scripts\activate
pip install -r requirements_win.txt
playwright install chromium
cd frontend && npm install && npm run build && cd ..
```

### Run

```bash
source venv/bin/activate    # Linux
# myenv\Scripts\activate     # Windows
python3 app/main.py
# or uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

Open `http://localhost:8002/` in your browser. Default admin: `admin / Admin@2024`.

## 📖 Workflow

```
Login → Create Project → Add Module → Write Cases → Run Tests → View Reports
                       ↘ AI Generate ↑                   ↓
                         Upload doc → Preview → Import    Remote Agent
```

**Two ways to create test cases:**
1. **Manual** — Create step by step with natural language steps and expected results
2. **AI Generation** — Upload requirement documents; extract test items, generate cases, preview and import; stop analysis from generation history when needed

Configure your LLM in "Settings → AI Config" before running (supports OpenAI and compatible APIs). Fresh installs seed default Agents and prompt templates on startup.

## 🏗️ Architecture

```mermaid
flowchart LR
    subgraph Browser["Browser"]
        CH[Chromium<br/>Screenshots Actions]
    end
    subgraph Backend["Backend FastAPI"]
        direction TB
        LLM[LLM Engine]
        Runner[Test Runner]
        Report[Report Generator]
    end
    subgraph Agent["Distributed Agent"]
        AC[Agent Client<br/>Remote Browser]
    end

    CH <-->|Playwright MCP| Backend
    Backend --> DB[(PostgreSQL)]
    Backend --> UI[Web UI<br/>React + Arco]
    Backend <-->|WebSocket| AC
```

## 🧪 Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + SQLAlchemy async + PostgreSQL 16 |
| Browser Automation | Playwright MCP |
| AI/LLM | OpenAI-compatible API (generation / execution / recording Agents) |
| Frontend | React 18 + Arco Design Pro + Vite |
| Distributed | WebSocket + Custom Agent Protocol |

## 📦 Project Structure

```
VoyanTest/
├── app/          # FastAPI backend
│   ├── gen/      # AI generation (chunking / prompts / pipeline)
│   ├── models/   # Domain models
│   ├── seed_defaults.py  # Default Agents & prompt sync
│   └── routers/  # API routes (including gen/ upload, history, cancel)
├── frontend/     # React frontend (gen / gen-history / ...)
├── core/         # Execution engine
├── agent/        # Distributed agent client
├── reports/      # Test reports & screenshots
├── docs/         # Documentation
└── tests/        # Unit / contract / e2e tests
```

## 📚 Docs

- API Docs: visit `/docs` after starting (Swagger)
- Deployment: see [DEPLOYMENT.md](DEPLOYMENT.md)
- Chinese README: [README.md](README.md)
- Database: PostgreSQL primary; startup adds missing columns / seeds defaults (Alembic optional)

## 📄 License

MIT
