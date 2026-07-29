#!/usr/bin/env bash
# 增量打包服务端镜像：更新 Python 代码 + 重新构建前端静态资源。
# 禁止用仓库里过期的 app/static 覆盖镜像前端（会导致设置/AI Agent 菜单消失）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT_TAR="${1:-/vol1/1000/tools/voyantest/voyantest-docker.tar.gz}"
IMAGE_TAG="${IMAGE_TAG:-voyantest:latest}"

echo "[1/4] Building frontend..."
(cd frontend && npm ci --prefer-offline && npm run build)

echo "[2/4] Staging fresh static..."
STAGE=$(mktemp -d /tmp/voyantest-static.XXXXXX)
trap 'rm -rf "$STAGE" /tmp/voyantest-incr-ctx' EXIT
mkdir -p "$STAGE/static"
cp -a frontend/dist/. "$STAGE/static/"
ENTRY=$(grep -oE 'assets/index\.[a-f0-9]+\.js' "$STAGE/static/index.html" | head -1 || true)
echo "Frontend entry: $ENTRY"

echo "[3/4] Incremental image build (stale app/static excluded)..."
rm -rf /tmp/voyantest-incr-ctx
mkdir -p /tmp/voyantest-incr-ctx
rsync -a \
  --exclude 'static' \
  "$ROOT/app" "$ROOT/core" "$ROOT/agent" "$ROOT/alembic" \
  /tmp/voyantest-incr-ctx/
cp -a "$ROOT/alembic.ini" "$ROOT/voyan_cli.py" /tmp/voyantest-incr-ctx/
cp -a "$STAGE/static" /tmp/voyantest-incr-ctx/static

cat > /tmp/voyantest-incr-ctx/Dockerfile <<'DOCKER'
FROM voyantest:latest
WORKDIR /app
# Playwright MCP 需要 npx/@playwright/mcp；旧镜像可能缺 Node
RUN if ! command -v npx >/dev/null 2>&1; then \
      apt-get update -qq \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates gnupg \
      && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs \
      && rm -rf /var/lib/apt/lists/* \
      && node -v && npm -v; \
    else node -v && npm -v; fi
COPY app/ app/
COPY core/ core/
COPY agent/ agent/
COPY alembic/ alembic/
COPY alembic.ini voyan_cli.py ./
# 显式写入本次前端构建，避免沿用/覆盖成仓库里过期的 static
COPY static/ app/static/
DOCKER

docker build -t "$IMAGE_TAG" /tmp/voyantest-incr-ctx

echo "[4/4] Export $OUT_TAR ..."
docker save "$IMAGE_TAG" | gzip -1 > "$OUT_TAR"
ls -lah "$OUT_TAR"

# Sanity: image must serve the new entry bundle
docker run --rm "$IMAGE_TAG" sh -c \
  "grep -E '执行后端|execution-backend|agent_definitions' /app/app/static/assets/index.*.js >/dev/null && head -c 300 /app/app/static/index.html && echo"
echo "PACK_OK entry=$ENTRY"
