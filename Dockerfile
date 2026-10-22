# ============================================================
# 可选：浏览器缓存（避免重复下载 ~622MB Chromium）
# 默认 python 基础镜像（空目录，走正常下载）；
# 打包时可用 --build-arg BROWSER_CACHE_IMAGE=voyantest:latest 复用现有镜像缓存。
# ============================================================
ARG BROWSER_CACHE_IMAGE=python:3.11-slim
FROM ${BROWSER_CACHE_IMAGE} AS browser-cache
RUN mkdir -p /root/.cache/ms-playwright

# ============================================================
# Stage 1: Build frontend
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /build/frontend

# Copy dependency files first for layer caching
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline

# Copy frontend source and build
ARG BUILD_DATE
RUN test -z "$BUILD_DATE" || echo "Build: $BUILD_DATE"
COPY frontend/ .
RUN npm run build && \
    mkdir -p /build/static && \
    cp -r dist/* /build/static/
FROM python:3.11-slim

WORKDIR /app

# 先取前端产物：强制后端阶段等待前端构建完成，避免 BuildKit 并行跑
# npm build 与 apt/pip/playwright 安装，在 4 核机器上互相抢 CPU
# （实测：前端单独构建 49s，并行被挤到 547s）。app/static 在 .dockerignore
# 中排除，后面的 COPY app/ 不会覆盖这里。
COPY --from=frontend-builder /build/static/ app/static/

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libu2f-udev \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright 浏览器：优先复用 BROWSER_CACHE_IMAGE 里的缓存（若存在）
COPY --from=browser-cache /root/.cache/ms-playwright /root/.cache/ms-playwright

# Install Playwright browsers
RUN python -m playwright install chromium && \
    python -m playwright install-deps chromium

# Server launches @playwright/mcp via local cli.js (offline, no registry lookup).
# final stage is python:3.11-slim with no node, so install Node + the MCP package
# at build time and pin the CLI path for air-gapped deployments.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @playwright/mcp@0.0.83 && \
    ln -sf "$(npm root -g)/@playwright/mcp/cli.js" /usr/local/bin/vt-playwright-mcp
ENV VT_MCP_CLI=/usr/local/bin/vt-playwright-mcp

# Copy backend source code
# NOTE: app/static is in .dockerignore — frontend must come from frontend-builder only.
# Copying a stale repo app/static here would wipe menus like 执行后端 / AI Agent.
COPY app/ app/
COPY core/ core/
COPY agent/ agent/
COPY alembic/ alembic/
COPY alembic.ini voyan_cli.py ./

# Create required directories
RUN mkdir -p data reports logs

# Environment defaults — DATABASE_URL 通过 docker-compose 或 /setup 页面配置
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8002 \
    APP_WORKERS=1 \
    SESSION_SECRET_KEY= \
    DISABLE_CREATE_ALL=false \
    TZ=Asia/Shanghai

# Volumes for persistent data
VOLUME ["/app/data", "/app/reports", "/app/logs"]

EXPOSE 8002

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

# Run with uvicorn（多 worker + 生产参数）
CMD ["sh", "-c", "python -m uvicorn app.main:app --host $APP_HOST --port $APP_PORT --workers $APP_WORKERS --timeout-keep-alive 65 --backlog 2048 --limit-max-requests 10000 --log-level info"]
