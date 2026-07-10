# Search Rank Deboost Hardening Design Plan

## Goal

完成多个降权专用账号分组、全账号选择硬隔离、真实 Telegram Gateway、分组代理绑定生命周期、逐点击限流和全入口用途策略的正式设计与代码实现，并同步项目真相源。

## Scope

- 产品与专项设计：账号用途、分组生命周期、任务选择、Gateway 契约、状态机、失败语义、迁移和验收。
- 数据流：账号创建/迁移、普通任务候选、降权任务 Planner/Dispatcher/Gateway、代理绑定和点击统计。
- 文档同步：总 PRD、专项设计、结构索引、数据流索引、生产运行约束。
- 实现同步：账号用途策略、多个降权组、分组 runtime proxy、真实 Gateway、reservation、前端账号选择和索引证据。
- 当前阶段不直接部署生产；生产恢复必须另取 E4 证据。

## Phases

- [x] Phase 0: 用户确认采用全量真实执行方案 A。
- [x] Phase 1: 复核当前账号分组、任务选择、代理绑定和 Telethon search_join 参考实现。
- [x] Phase 2: 编写专项设计，锁定字段、API、状态机和模块边界。
- [x] Phase 3: 同步总 PRD、结构索引、数据流索引和生产约束。
- [x] Phase 4: 做占位符、矛盾、范围和可验收性自检。
- [x] Phase 5: 设计文档已提交并进入实现计划。
- [x] Phase 6: 完成代码实现与子代理 review 修复。
- [ ] Phase 7: Release gate 全量验收；no-PostgreSQL 已通过，当前仍被 migration offline SQL 和 PostgreSQL 并发证据阻断。

## Success Criteria

- 多个 `rank_deboost` 分组可新建、重命名、禁用，账号迁移原子同步用途。
- 普通任务和所有普通操作入口都排除降权专用账号。
- 降权任务的“全部账号”只选择所有启用降权分组中的可用账号。
- 每个分组持久复用一个代理绑定，任务不拥有绑定生命周期。
- 真实 Gateway 通过同一代理完成出口探测、搜索和逐按钮点击，并返回逐点击事实。
- 点击配额在外部点击前原子占用，统计只由真实 Gateway 结果生成。
- 文档中的 API、字段和状态名彼此一致且没有 TBD/TODO。
- 定向 rank、前端契约和全量 no-PostgreSQL 已通过；PostgreSQL 并发和生产 E4 通过后，才允许声明 production-fixed。

## Errors Encountered

- PRD §2.8 例外条款首次补丁因原文末尾为“原约束”而非预期的“硬约束”未匹配；已读取准确原文后改用精确补丁，不重复原失败命令。
- 全量 `pytest -q -m no_postgres` 初始失败已修复；最新结果 `1090 passed, 775 deselected, 5 warnings in 28.54s`。
- `alembic upgrade head --sql` 被旧迁移 `0002_developer_app_pool.py` offline inspect 阻断；当前记录为 migration evidence blocker。
- PostgreSQL 专项文件 `tests/test_search_rank_deboost_postgres.py` 不存在，Postgres 并发 evidence 记录为 blocked/unavailable。
