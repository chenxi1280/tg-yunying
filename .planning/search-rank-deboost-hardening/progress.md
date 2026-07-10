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
