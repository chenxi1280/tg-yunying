# Existing Account Reauthorization Release Gate

- message_id: 019ffb9f-4fd0-7722-a637-f2cc68b8eba6
- intake_id: existing-account-reauthorization-routing-20260814
- from_agent: dev
- to_agent: qa, prod-diagnosis
- level: L2
- release_mode: github_actions
- release_owner: Codex / repository release workflow
- rollback_owner: repository release owner
- status: pending

## 上线范围

- 已有同租户账号再次从新增入口提交时，返回结构化 409 并按原账号进入重新授权。
- `Session失效` 在账号列表显示“继续登录”。
- 不迁移数据、不新建重复账号、不改原账号分组、用途、任务/群关系或历史。

## 必须满足

- ci_or_build: `git diff --check` 通过；前端 `npm run build` 通过。
- backend_tests: 5 个账号登录/重授权定向测试通过（隔离 PostgreSQL）。
- frontend_build: 14 个前端数据流静态测试和生产构建通过。
- migration_impact: 无迁移。
- worker_impact: 无 worker、调度或任务履约路径变更。
- external_platform_impact: 仅在运营显式选择 code / QR 后调用既有 Telegram 登录；409 响应不发码。
- rollback_plan: 回退到本次发布前的不可变 `release` SHA；无数据回滚步骤。
- observe_window: GitHub Actions 发布完成后核对部署 SHA、运行健康和公开静态资源；真实重新授权仅在获授权的账号上验证。

## 发布后复核

- production_probe: 待部署后填写；不得把页面可打开或容器健康等同于 Telegram 授权恢复。
- logs_or_actions: 待本次 Deploy Production run。
- owner: prod-diagnosis
