# Progress

## 2026-07-10

- 用户选择方案 A，并要求所有审查问题一起修复。
- 完成账号分组、任务选择、代理绑定、Runtime、Dispatcher、Telethon Gateway 和 `search_join` 参考实现复核。
- 确认设计采用统一账号用途策略、多个降权分组、全降权账号选择、分组持久代理绑定、真实 Gateway 和逐点击配额。
- 已新增 `docs/03-feature-designs/search-rank-deboost-hardening-design.md`，覆盖字段、API、账号用途矩阵、分组绑定、真实 Gateway、逐点击 reservation、状态机、迁移、验收和回滚。
- 已同步总 PRD、`search-click-boost-prd.md`、结构索引、数据流索引和生产运行约束；文档明确当前真实 Gateway 未落地，避免把测试替身写成生产事实。
- 已将“全部专用账号”定义为所有启用降权组中的一致可用账号，并移除与该语义冲突的 10 账号代码硬上限设计。
- 已完成设计一致性自检：`git diff --check` 通过，专项设计未发现 `TODO`、`TBD`、`FIXME`，并清理了索引中的过期“待创建”和路由数量表述。
- 已运行降权任务、账号中心权限及前端权限定向回归：`274 passed in 5.68s`。
- 设计状态为 `design_complete`；生产代码尚未修改，实施与生产状态分别为 `implementation_not_started`、`production_unproven`。

## 2026-07-10 Implementation Continuation

- 已按实施计划落地账号用途策略、多个 `rank_deboost` 黑账号组、分组 runtime proxy binding、真实 Telethon Gateway、逐点击 reservation、Planner/Runtime factual outcome 状态机和前端账号选择契约。
- 已完成子代理 review；发现的 P1 reservation 问题已修复：预执行 skip 释放 reservation，retry 不再错误重排 consumed/unknown，released reservation 只在显式 retry 前 reopen。
- 已保留兼容导出 `_rank_deboost_pool_accounts`，修复旧 hard-boundary 测试 collection 入口。
- 已将 `search_rank_deboost_runtime.py` 的代理出口告警 helper 拆出到 `search_rank_deboost_runtime_alerts.py`，runtime 文件降至 483 行，符合项目 500 行限制。
- 验证通过：rank 定向集成套件 `363 passed in 10.12s`；前端契约 / 权限 / task dataflow `160 passed in 1.15s`；全量 no-PostgreSQL `1090 passed, 775 deselected, 5 warnings in 28.54s`；`frontend npm run build` 通过，只有 Vite chunk size warning；`py_compile` 与 `git diff --check` 已通过。
- Release gate 收敛：旧 fixture / strict usage / runtime proxy readiness 造成的全量 no-PostgreSQL 失败已修复，显式保留 legacy unpooled normal 账号为普通用途，dedicated identity 仍 fail-closed。
- Migration evidence 阻断：`alembic upgrade head --sql` 被旧迁移 `0002_developer_app_pool.py` 的 offline `sa.inspect(MockConnection)` 阻断；不是本迁移单点通过证据。
- PostgreSQL 并发证据阻断：仓库当前没有 `backend/tests/test_search_rank_deboost_postgres.py`，命令 exit 4；未取得 Postgres reservation 并发验收。
- 生产状态保持 `production_unproven`；本轮未做生产部署，未写 `production_fixed`。
