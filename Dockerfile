# ============================================================
# Stage 1: Build frontend
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /build/frontend

# Copy dependency files first for layer caching
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Copy frontend source and build
COPY frontend/ .
RUN npm run build && \
    mkdir -p /build/static && \
    cp -r dist/* /build/static/

# ============================================================
# Stage 2: Python backend + Playwright
# ============================================================
FROM python:3.11-slim

WORKDIR /app

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

# Install Playwright browsers
RUN python -m playwright install chromium && \
    python -m playwright install-deps chromium

# Copy backend source code
COPY app/ app/
COPY core/ core/
COPY agent/ agent/
COPY alembic/ alembic/
COPY alembic.ini voyan_cli.py ./

# Copy built frontend from stage 1
COPY --from=frontend-builder /build/static/ app/static/

# Create required directories
RUN mkdir -p reports logs

# Environment defaults
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8002 \
    DATABASE_URL=sqlite+aiosqlite:///./data/voyantest.db \
    SESSION_SECRET_KEY= \
    DISABLE_CREATE_ALL=false \
    TZ=Asia/Shanghai

# Volumes for persistent data
VOLUME ["/app/data", "/app/reports", "/app/logs"]

EXPOSE 8002

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

# Run with uvicorn
CMD ["sh", "-c", "python -m uvicorn app.main:app --host $APP_HOST --port $APP_PORT --log-level info"]
