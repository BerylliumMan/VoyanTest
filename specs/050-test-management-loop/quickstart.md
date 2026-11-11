# Quickstart: 测试管理闭环

## 前置

- 后端依赖已装：`pip install -r requirements.txt`
- 前端依赖已装：`cd frontend && npm install`
- 测试数据库可用（契约测试用 sqlite，无需外部服务）
- 真机验证需要一个已启动的实例（`uvicorn app.main:app` 或容器）和可执行用例

## 1. 后端单测

```bash
cd /home/lzl/git-repos/uitest-work
pytest tests/unit/test_test_plan.py tests/unit/test_test_cycle.py \
       tests/unit/test_case_review.py tests/unit/test_case_comments.py -q
pytest tests/contract/test_plans_api.py tests/contract/test_comments_api.py \
       tests/contract/test_case_reviews_api.py -q
```

## 2. 全量回归

```bash
pytest tests/unit -q
pytest tests/contract -q
```

基线：049 后全量 2161 passed（单测）；契约 16 passed（049 新增部分）。

## 3. 前端构建

```bash
cd frontend
npx tsc --noEmit
npm run build
```

产物入口：`frontend/dist/index.html` 引用的 `assets/index.<hash>.js`。

## 4. 真机流程（UI）

1. 登录（admin/Admin@2024），进入项目 47。
2. 打开「测试计划」→ 新建计划「验收计划」→ 加入 2 条用例（一条应通过、一条应失败）。
3. 计划项设置负责人、截止日期。
4. 「创建轮次」→ 选择环境 → 开始；等待批次结束。
5. 检查：轮次 completed、通过/失败数正确；计划项状态同步；报告链接可打开。
6. 「我的待办」：对应负责人能看到任务；把截止日期改成昨天，逾期筛选出现。
7. 用例详情：发表评论 `@<用户名>`；被提及者通知中心出现通知。
8. 用例详情：「提交评审」给另一用户；该用户「待评审」通过；发起人收到通知，状态「已通过」。

## 5. API 冒烟（curl）

```bash
BASE=http://127.0.0.1:8002
# 登录拿 cookie
curl -c /tmp/c.txt -s -X POST $BASE/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Admin@2024"}'

# 建计划
curl -b /tmp/c.txt -s -X POST $BASE/api/plans \
  -H 'Content-Type: application/json' \
  -d '{"project_id":47,"name":"验收计划","due_date":"2026-12-31"}'

# 加用例（替换 case_ids）
curl -b /tmp/c.txt -s -X POST $BASE/api/plans/1/items \
  -H 'Content-Type: application/json' -d '{"case_ids":[752,753]}'

# 建轮次并启动
curl -b /tmp/c.txt -s -X POST $BASE/api/plans/1/cycles \
  -H 'Content-Type: application/json' -d '{"name":"第 1 轮"}'
curl -b /tmp/c.txt -s -X POST $BASE/api/cycles/1/start
```

## 6. 验收清单

- [ ] 计划 CRUD 与批量加用例（去重）通过
- [ ] 轮次快照不受计划项后续增删影响
- [ ] 同一计划不能同时 running 两个轮次
- [ ] 批次收尾后轮次统计 = 批次统计；计划项状态同步
- [ ] 重跑失败归属同一轮次
- [ ] 我的待办跨项目聚合、逾期筛选正确
- [ ] 评论 @ 提及通知；权限 403 正确
- [ ] 评审 pending 唯一、结论通知、历史可查
