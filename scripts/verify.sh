#!/usr/bin/env bash
# VoyanTest 变更后容器验证脚本. 用法: ./verify.sh [--seal|--help]
# 环境变量: VOYANTEST_CONTAINER (默认 voyantest), VOYANTEST_PORT (默认 8002)

# ── 配置 ────────────────────────────────────────────────────────────────────
CONTAINER="${VOYANTEST_CONTAINER:-voyantest}"
PORT="${VOYANTEST_PORT:-8002}"
HEALTH_URL="http://localhost:${PORT}/health"
SMOKE_URL="http://localhost:${PORT}/api/setup/status"
EVIDENCE_LOG=".omo/verify-log.jsonl"
MAX_RETRIES=10
RETRY_DELAY=3

# ── 颜色输出 ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
pass() { echo -e "  ${GREEN}PASS${NC} $*"; }
fail() { echo -e "  ${RED}FAIL${NC} $*"; }
info() { echo -e "  ${BOLD}→${NC} $*"; }

# ── 帮助 ────────────────────────────────────────────────────────────────────
print_help() {
    cat <<EOF
用法: $(basename "$0") [--seal|--help]

VoyanTest 变更后容器验证 — 检测未提交变更、部署到容器、验证健康状态。

模式:
  (无参数)     L1 快速验证 — 部署变更、重启容器、健康检查
  --seal       L2 完整验证 — L1 + 冒烟测试 + docker commit
  --help       打印此帮助信息

环境变量:
  VOYANTEST_CONTAINER  容器名称 (默认: voyantest)
  VOYANTEST_PORT       服务端口   (默认: 8002)
EOF
    exit 0
}

# ── 参数解析 ────────────────────────────────────────────────────────────────
MODE="L1"
case "${1:-}" in
    --seal)     MODE="L2" ;;
    --help|-h)  print_help ;;
    "")         ;;
    *)          echo "未知参数 '$1'，使用 --help 查看用法" >&2; exit 2 ;;
esac

# ── 初始化 ──────────────────────────────────────────────────────────────────
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
[ -z "$REPO_ROOT" ] && { echo "错误: 不在 git 仓库内" >&2; exit 1; }
cd "$REPO_ROOT"
mkdir -p "$(dirname "$EVIDENCE_LOG")"

# ── 全局状态 ────────────────────────────────────────────────────────────────
CHANGED_FILES=()
CHECKSUM_OK=true
HEALTH_OK=false
SMOKE_OK=false
TS=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')

# ── 路径映射: app/*.py→/app/app/*.py  core/**/*.py→/app/core/**/*.py
#              frontend/dist/**→/app/app/static/**
map_to_container() {
    case "$1" in
        app/*)           echo "/app/$1" ;;
        core/*)          echo "/app/$1" ;;
        frontend/dist/*) echo "/app/app/static/${1#frontend/dist/}" ;;
        *)               echo "" ;;
    esac
}

# ── 文件过滤: 仅 .py (app/, core/) 和 frontend/dist/ ────────────────────────
is_target() { [[ "$1" =~ ^app/.*\.py$ ]] || [[ "$1" =~ ^core/.*\.py$ ]] || [[ "$1" =~ ^frontend/dist/ ]]; }

# ── 前置检查: 容器必须运行 ──────────────────────────────────────────────────
if ! docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
    echo "容器 '$CONTAINER' 未运行，请先 docker start $CONTAINER" >&2
    exit 1
fi

# ── 写入证据日志 (JSON Lines) ───────────────────────────────────────────────
write_evidence() {
    local files_json="["
    local sep="" f
    for f in "${CHANGED_FILES[@]}"; do
        files_json+="${sep}\"$f\""; sep=","
    done
    files_json+="]"
    printf '{"ts":"%s","level":"%s","files":%s,"checksum_ok":%s,"health_ok":%s,"smoke_ok":%s}\n' \
        "$TS" "$MODE" "$files_json" "$CHECKSUM_OK" "$HEALTH_OK" "$SMOKE_OK" >> "$EVIDENCE_LOG"
}

# ── 步骤总数 ────────────────────────────────────────────────────────────────
TOTAL_STEPS=2; [ "$MODE" = "L2" ] && TOTAL_STEPS=3

# ═══════════════════════════════════════════════════════════════════════════════
# 步骤 1: 变更检测 + 部署 + sha256sum 校验
# ═══════════════════════════════════════════════════════════════════════════════
echo -e "\n${BOLD}[1/${TOTAL_STEPS}] 变更检测 + 部署 + 校验${NC}"

all_files=$(git diff --name-only HEAD 2>/dev/null)
if [ -z "$all_files" ]; then
    info "无变更文件 (git diff --name-only HEAD)"
else
    while IFS= read -r f; do
        [ -z "$f" ] || ! is_target "$f" || ! [ -f "$f" ] && continue

        dst=$(map_to_container "$f")
        [ -z "$dst" ] && continue
        CHANGED_FILES+=("$f")

        docker exec "$CONTAINER" mkdir -p "$(dirname "$dst")" 2>/dev/null || true

        if ! docker cp "$f" "$CONTAINER:$dst" 2>/dev/null; then
            fail "docker cp 失败: $f"; CHECKSUM_OK=false; continue
        fi

        local_sha=$(sha256sum "$f" | awk '{print $1}')
        container_sha=$(docker exec "$CONTAINER" \
            sh -c "sha256sum '$dst'" 2>/dev/null | awk '{print $1}')

        if [ "$local_sha" = "$container_sha" ] && [ -n "$local_sha" ]; then
            pass "$f"
        else
            fail "checksum 不匹配: $f"; CHECKSUM_OK=false
        fi
    done <<< "$all_files"

    [ ${#CHANGED_FILES[@]} -gt 0 ] && info "已部署 ${#CHANGED_FILES[@]} 个文件" \
        || info "所有变更被跳过 (非 .py / dist 目标)"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 步骤 2: 重启容器 + 轮询健康检查 (最长 30s)
# ═══════════════════════════════════════════════════════════════════════════════
echo -e "\n${BOLD}[2/${TOTAL_STEPS}] 重启 + 健康检查${NC}"

docker restart "$CONTAINER" >/dev/null 2>&1 || {
    fail "容器重启失败"; write_evidence; exit 1
}

info "轮询 ${HEALTH_URL} (${MAX_RETRIES} x ${RETRY_DELAY}s) ..."
for i in $(seq 1 "$MAX_RETRIES"); do
    if curl -s --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -q '"ok"'; then
        pass "健康 (${i}x${RETRY_DELAY}s)"; HEALTH_OK=true; break
    fi
    sleep "$RETRY_DELAY"
done

[ "$HEALTH_OK" != "true" ] && {
    fail "健康检查超时"; write_evidence; exit 1
}

# ── L1 证据 + 结果 ──────────────────────────────────────────────────────────
write_evidence
echo ""
$HEALTH_OK && pass "L1 验证通过" || { fail "L1 验证失败"; exit 1; }

# ═══════════════════════════════════════════════════════════════════════════════
# 步骤 3: L2 冒烟测试 + Docker Commit (仅 --seal)
# ═══════════════════════════════════════════════════════════════════════════════
if [ "$MODE" = "L2" ]; then
    echo -e "\n${BOLD}[3/${TOTAL_STEPS}] 冒烟测试 + Docker Commit${NC}"

    http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$SMOKE_URL" 2>/dev/null || echo "000")
    if [ "$http_code" = "200" ]; then
        pass "冒烟测试通过 (HTTP 200)"; SMOKE_OK=true
    else
        fail "冒烟测试失败 (HTTP $http_code)"
    fi

    docker commit "$CONTAINER" voyantest:latest >/dev/null 2>&1 \
        && pass "Docker commit: voyantest:latest" \
        || fail "docker commit 失败"

    write_evidence
    echo ""
    $SMOKE_OK && pass "L2 验证通过" || { fail "L2 验证失败"; exit 1; }
fi

echo ""
info "证据已写入 ${EVIDENCE_LOG}"
exit 0
