# AI 活群历史引用查询生产 CPU 事故修复计划

## Goal

在保持“AI 活群只能引用同 tenant、同 Task、同群、具有成功 Attempt 和非空远端消息 ID 的我方历史消息”语义不变的前提下，消除多 worker 对 14 万级 `actions` 历史的并发放大查询，完成代码、QA、正式发布和生产 E4 复核。

## Success criteria

1. 生产故障边界由 SSH 日志、资源快照、数据库活动和查询计划共同证明。
2. PRD 明确性能/并发合同、回滚边界和 E2/E3/E4 验收口径。
3. 查询先按 tenant/Task/group/success 有界选取候选，再只读取候选 Action 的最新成功 Attempt；不得进行全局 Attempt 聚合或无界 Action 历史扫描。
4. SQLite 语义回归和 PostgreSQL 查询计划/性能回归通过；变更文件符合项目复杂度限制。
5. 通过 `master -> release -> GitHub Actions Deploy Production` 发布，生产 SHA、容器和健康检查一致。
6. 发布后 PostgreSQL/Planner 不再持续压满 4 核，AI 活群与频道浏览分别按 Action/Attempt/typed remote fact 复核；没有 E4 时保持 `production_unproven`。

## Phases

| Phase | Status | Verification |
| --- | --- | --- |
| 1. 生产只读诊断与查询计划 | complete | SSH 系统/容器/PG 证据；EXPLAIN 无写入 |
| 2. Product Design Complete / Release Gate | complete | PRD、数据流与结构索引覆盖性能合同和验收 |
| 3. 开发实现与红绿测试 | complete | 定向测试、PostgreSQL plan test、compile、diff check |
| 4. QA 与产品验收 | complete | 回归证据和原始需求逐条接受 |
| 5. master/release 正式发布 | complete | GitHub Actions 成功，生产 SHA/运行时回读 |
| 6. 生产 E4 与资源复核 | complete | AI send fact 与有源 ViewRemoteFact 通过；无源浏览任务保持 unproven |

## Guardrails

- 不修改、drain、重排、重启或手工修复生产数据。
- 不触碰主工作区未提交内容；只在独立 worktree 修改。
- 不把健康检查、容器 healthy、CI 或发布成功当成业务修复。
- 不改变引用来源、任务排期、reply 比例、unknown 防重或浏览事实口径。

## Errors encountered

| Error | Resolution |
| --- | --- |
| 本机 Clash TUN 对任意目标端口提供透明接管，导致 TCP 探测假阳性和 SSH banner timeout | 临时关闭 TUN 完成 SSH 取证后已恢复；后续 SSH 检查需绕开 TUN |
| 早期 `pg_stat_activity` 命令引用转义失败 | 改用生产容器内固定数据库只读查询，未产生写入 |
| 首次迁移测试把 SQLite 主键自动索引误算成业务索引 | 改为只断言目标索引存在/移除，保留自动索引 |
| PostgreSQL 性能测试首次未显式建立租户外键和 Attempt 租户 | 测试数据按生产外键合同显式绑定 tenant；业务实现不受影响 |
