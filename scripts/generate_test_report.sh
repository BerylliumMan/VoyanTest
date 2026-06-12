#!/bin/bash
# 生成测试报告 — JUnit XML + HTML 覆盖率 + Markdown 摘要
set -e
mkdir -p reports

echo "=== 运行全量离线测试 ==="
pytest tests/ -v -m "not integration and not e2e" \
    --junitxml=reports/junit.xml \
    --cov=app --cov=core \
    --cov-report=html:reports/coverage \
    --cov-report=term \
    --no-header -q 2>&1 | tee reports/pytest.log

PASSED=$(grep -oP '\d+(?= passed)' reports/pytest.log || echo 0)
FAILED=$(grep -oP '\d+(?= failed)' reports/pytest.log || echo 0)
TOTAL=$((PASSED + FAILED))
PCT=$(python3 -c "print(f'{$PASSED / max($TOTAL, 1) * 100:.1f}%')" 2>/dev/null || echo "N/A")

cat > reports/summary.md << EOF
# 测试报告

**时间**: $(date '+%Y-%m-%d %H:%M:%S')
**分支**: $(git branch --show-current)

## 结果

| 指标 | 值 |
|------|-----|
| 总测试数 | $TOTAL |
| 通过 | $PASSED |
| 失败 | $FAILED |
| 通过率 | $PCT |

## 文件

- JUnit XML: \`reports/junit.xml\`
- HTML 覆盖率: \`reports/coverage/index.html\`
- 测试日志: \`reports/pytest.log\`

EOF

echo "=== 报告已生成 ==="
echo "  reports/summary.md"
echo "  reports/junit.xml"
echo "  reports/coverage/index.html"
