# 变更后验证规则 — VoyanTest

## 决策逻辑

| 条件 | 级别 |
|------|------|
| CSS/前端静态文件 (.tsx, .css, .html) | L1 |
| 后端 Python (routers, crud, models, services) | L1 |
| 核心执行引擎 (`core/`) | L2 |
| 数据库模型或迁移 | L2 |
| Docker 配置或依赖 | L2 |
| 提交到 `master`/`main` | L2 |

L2 包含 L1 全部步骤。

## L1 — 快速验证 (< 30s)

```bash
CHANGED=$(git diff --name-only HEAD~1)
for f in $CHANGED; do
  docker cp "$f" voyantest:/app/"$f"
  docker exec voyantest sha256sum /app/"$f"
  sha256sum "$f"
done
docker restart voyantest
curl -f --max-time 5 --retry 6 --retry-delay 5 http://localhost:8002/health
```

### L1 通过条件：sha256sum 一致 + 重启成功 + 健康检查返回 ok

---

## L2 — 完整密封 (< 2min)

```bash
# === L1 全部步骤 ===
CHANGED=$(git diff --name-only HEAD~1)
for f in $CHANGED; do
  docker cp "$f" voyantest:/app/"$f"
  docker exec voyantest sha256sum /app/"$f"
  sha256sum "$f"
done
docker restart voyantest
curl -f --max-time 5 --retry 6 --retry-delay 5 http://localhost:8002/health

# === API 冒烟测试 ===
curl -f http://localhost:8002/api/setup/status

# === 密封镜像 ===
docker commit voyantest voyantest:latest
```

### L2 附加通过条件

- API 冒烟测试返回 200
- `docker commit` 无报错

---

## 证据账本

每次验证追加一行到 `.omo/verify-log.jsonl`：

```bash
echo "{\"timestamp\":\"$(date -Iseconds)\",\"level\":\"L1\",\"checksum_ok\":true,\"health_ok\":true}" \
  >> .omo/verify-log.jsonl
```

L2 示例：
```bash
echo "{\"timestamp\":\"$(date -Iseconds)\",\"level\":\"L2\",\"checksum_ok\":true,\"health_ok\":true,\"smoke_ok\":true,\"docker_commit_sha\":\"$(docker inspect --format '{{.Id}}' voyantest)\"}" \
  >> .omo/verify-log.jsonl
```

---

## 自动化

- `scripts/verify.sh` — 一键运行上述流程（按 `--level L2` 参数切换级别）
- `.git/hooks/pre-push` — 自动触发 `scripts/verify.sh --level L2`，若失败则阻止推送
