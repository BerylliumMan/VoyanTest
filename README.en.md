<p align="center">
  <em>Write tests in natural language; AI drives the browser. Successful runs solidify into Playwright scripts for zero-LLM replays.</em>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"></a>
  <a href="#"><img src="https://img.shields.io/badge/node-18%2B-green" alt="Node.js 18+"></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey" alt="Platform"></a>
  <a href="./README.md"><img src="https://img.shields.io/badge/lang-中文-red" alt="中文"></a>
</p>

---

VoyanTest is an **AI-powered Web UI testing platform**. Describe cases in **natural language**; the default **nl_goal** loop observes and acts. On success, Playwright codegen–compatible locators are solidified into a `compiled_script` for the next zero-LLM run.

```
Natural-language case / checklist
  ↓ nl_goal (LLM + snapshot / MCP / hybrid)
journal + codegen locators
  ↓ synthesize Playwright async script
Next run: compiled_script first → fall back to nl_goal on failure
```

## ✨ Features

### Execution
- **nl_goal (default)**: whole-case goal loop with checklist steps; hybrid MCP + browser-use
- **Playwright solidify**: persist `compiled_script` with codegen `get_by_*` locators (no ephemeral refs)
- **Script-first / AI fallback**: run script when valid; clear/re-solidify per policy on failure
- **Init cases**: batch can run a login (or similar) init case and reuse the browser session
- **Batch controls**: pause / resume / stop
- **Real browser**: Playwright MCP / shared CDP Chromium; headed GUI client
- Debug pause, retries, assertions, healer, CDP record/replay, xlsx, dashboard trends, notifications, API keys, CSRF

### AI & platform
- Requirement → test items → functional / UI cases; multimodal docs; generation history
- Project RBAC, reports, distributed Agent (GUI / CLI), dark theme

## 🚀 Quick Start

### Docker Compose (recommended)

```bash
cd VoyanTest
docker compose up -d
```

Open `http://localhost:8002/`; first visit may go through `/setup` for PostgreSQL.

### From source

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

Default admin: `admin / Admin@2024`.

## 🧭 Main flow

1. Log in → create project / module → write or AI-generate cases  
2. Start GUI/CLI Agent → batch run (optional **init case** for shared login)  
3. First run often **nl_goal**; success writes **compiled_script**  
4. Later runs prefer the script; failures fall back to AI  

Configure LLM under Settings. Execution backend defaults to **nl_goal** (`data/execution_backend.json`).

### Windows Agent package

```powershell
.\install_and_build.bat
# Output: dist\VoyanTest-Agent\VoyanTest-Agent.exe (onedir)
```

## 🏗️ Architecture

```mermaid
flowchart LR
    subgraph Client["Client Agent"]
        GUI[GUI / CLI]
        MCP[Playwright MCP]
        CDP[Shared CDP]
        PY[compiled_script]
    end
    subgraph Backend["FastAPI"]
        NL[nl_goal]
        SYN[synth / codegen]
        RUN[orchestration]
        REP[reports]
    end
    GUI <-->|WebSocket| RUN
    RUN → NL
    NL → SYN
    RUN → PY
    Backend → PG[(PostgreSQL)]
    Backend → UI[React]
```

## 🧪 Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + SQLAlchemy async + PostgreSQL |
| Browser | Playwright MCP, CDP, Playwright Python scripts |
| Locators | playwright-selector-generator (IIFE) |
| Frontend | React 18 + Arco Design Pro + Vite |
| Agent build | PyInstaller onedir (`install_and_build.bat`) |

## 📦 Structure

```
VoyanTest/
├── app/          # FastAPI
├── core/         # nl_goal, codegen, synth, compiled_script
├── agent/        # GUI / CLI client
├── frontend/
├── scripts/      # build_codegen_iife.mjs
├── install_and_build.bat
└── tests/
```

Rebuild codegen asset after Playwright upgrades: `node scripts/build_codegen_iife.mjs`

## 📚 Docs

- API: `/docs` · Deploy: [DEPLOYMENT.md](DEPLOYMENT.md) · Chinese: [README.md](README.md)

## 📄 License

MIT
