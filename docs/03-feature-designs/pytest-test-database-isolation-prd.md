# Pytest 测试数据库隔离与破坏性重置安全合同

状态：Product Design Complete

事故引用：`incident-2026-09-01-drop-schema-lock-storm`

范围：`backend/tests/conftest.py` 的 PostgreSQL 集成测试初始化

## 1. 问题与目标

测试基础设施需要清空 PostgreSQL 测试库后执行全量迁移，但历史实现允许在缺少
`TEST_DATABASE_URL` 时回退到应用 `DATABASE_URL`，并直接对 `public` 执行
`DROP SCHEMA ... CASCADE`。环境漂移会把测试清理逻辑带到生产库；多个 pytest
进程同时启动时还会形成并发排他锁竞争。

本修复必须让错误配置在破坏性 DDL 之前显式失败，并保证同一测试库同一时刻只有
一个 pytest 会话能执行重置或测试。测试失败必须暴露真实原因，不允许降级到 SQLite、
mock 数据库或跳过 PostgreSQL 集成测试。

## 2. 固定边界

- PostgreSQL 集成测试只接受显式 `TEST_DATABASE_URL`；禁止读取或回退
  `DATABASE_URL`。
- 允许的数据库名只有 CI 临时库 `tg_yunying_test`。不再允许云端或生产实例上的
  共享测试库；新测试库必须先修改本合同和测试，再进入允许名单。
- `APP_ENV` 为 `production` 或 `prod` 时拒绝数据库集成测试。
- 错误只能展示数据库名、错误类型和脱敏说明，不能回显完整 URL、密码或查询参数。
- `no_postgres` 测试继续不连接、不锁定、不重置 PostgreSQL。
- 本专项不修改生产账号权限、生产数据、业务 worker、任务或 Telegram 状态。

## 3. 执行链路

`pytest collection -> 是否存在 PostgreSQL 测试 -> 读取 TEST_DATABASE_URL -> URL 名称允许名单 -> 建立互斥连接 -> pg_try_advisory_lock(固定 key) -> 实际 current_database 复核 -> 同事务 DROP public + CREATE public -> Alembic migration -> 整个 pytest session 持有互斥锁 -> session finish/异常释放连接与锁`

### 3.1 身份校验

URL 解析只用于第一道 fail-fast。建立连接后必须查询 `current_database()`，并满足：

1. 实际数据库名与 URL 目标名完全一致；
2. 实际数据库名属于固定允许名单；
3. 校验发生在 advisory lock 和任何 `DROP/CREATE` 之前。

### 3.2 并发合同

互斥锁为 PostgreSQL session-level advisory lock，key 为代码中的固定命名常量。
获取使用 `pg_try_advisory_lock`，不得排队等待；获取失败立即报告另一个 pytest
会话正在使用该库。锁连接在整个 pytest session 保持，正常结束、collection 失败或
进程退出后由显式关闭或 PostgreSQL 连接回收释放。

并发互斥解决“一个进程测试时另一个进程重新清库”的问题；数据库 `lock_timeout`
和 `statement_timeout` 只作为异常暴露边界，不能替代会话互斥。

### 3.3 重置原子性

`DROP SCHEMA public CASCADE` 与 `CREATE SCHEMA public` 必须在同一显式事务中完成。
任一语句失败时整体回滚，不允许留下已删除但未重建的 `public` schema。重置成功后
才执行现有 Alembic 迁移。

## 4. 失败与释放

- 缺少 `TEST_DATABASE_URL`：collection 以 `pytest.UsageError` 失败。
- URL 非 PostgreSQL、库名不在允许名单、生产环境：DDL 前失败。
- 实际数据库身份不一致：DDL 前失败。
- advisory lock 已占用：DDL 前失败，不等待、不抢占、不终止其他会话。
- reset 或 migration 失败：保留底层异常类型；释放本进程持有的锁。
- pytest 正常结束：释放锁连接和 engine。

## 5. QA 验收

- 仅设置 `DATABASE_URL` 时必须失败，证明没有回退。
- `tgyunying`、`latest_prod`、任意包含 `test` 但未列入名单的名称必须失败。
- `tg_yunying_test` 通过 URL 级校验；已移除的 `xixi_dev` 必须失败。
- 异常文本不得包含密码或完整 URL。
- 单元测试证明身份复核位于 destructive DDL 之前。
- 单元测试证明 advisory lock 使用 try-lock 且 pytest session 结束会释放。
- 单元测试证明重置使用事务，不使用 `AUTOCOMMIT`。
- 定向测试必须在 60 秒内通过；真实 PostgreSQL 并发仍由 CI PostgreSQL shard 验证。

## 6. 发布与回滚

本修复随常规 `master -> release -> Deploy Production` 发布，但运行时代码不会调用测试
conftest；发布价值是避免在生产 checkout、运维容器或误配置环境中再次执行危险测试。
代码回滚会重新开放事故入口，因此不得单独回滚安全合同；若 CI 因合法测试库名称变化
失败，应先更新本合同和精确名单，而不是恢复 `DATABASE_URL` fallback。
