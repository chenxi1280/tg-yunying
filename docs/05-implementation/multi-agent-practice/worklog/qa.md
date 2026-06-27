# Worklog: qa

## 2026-06-27

- message_id: 2026-06-27-docs-practice-qa-001
- action: 独立验收四 Agent 文档级演练材料
- input: 2026-06-27-docs-practice-devcomplete-001
- output: pass
- evidence: agent registry、五个模板、四个 worklog、完整 runs 记录均已存在；业务代码未修改
- decision: status=pass
- next_agent: prod-diagnosis
- unresolved: 真实生产问题复核不适用于本次文档级演练

## 2026-06-28 AI 活群话题老师连发配置 QA

- message_id: 2026-06-28-ai-group-topic-teacher-burst-qa-001
- action: 对 dev complete 的 AI 活群话题/老师/连发/Web/TG bot 配置做定向自动化验收
- input: 2026-06-28-ai-group-topic-teacher-burst-devcomplete-001
- output: qa_pass
- evidence: no_postgres 定向后端测试 13 passed, 97 deselected；frontend `npm run build` 成功；`git diff --check` 成功
- decision: schema 校验、planner payload、同账号连发、TG bot admin 权限与保存、Web 编译通过
- next_agent: product
- unresolved: CI / release deploy / production verification unproven

## 2026-06-28 hard-hourly min 10 QA

- message_id: 2026-06-28-hard-hourly-min-10-qa-001
- action: 对 AI 活群硬小时默认 10 的 schema、迁移、前端常量和文档同步做定向验收
- input: 2026-06-28-hard-hourly-min-10-devcomplete-001
- output: qa_pass
- evidence: no_postgres 定向后端测试 13 passed, 97 deselected；frontend `npm run build` 成功；`git diff --check` 成功
- decision: 低于 10 被拒绝，旧默认 60 迁移到 10，前端 build 通过
- next_agent: product
- unresolved: CI / release deploy unproven
