# Progress

## 2026-08-20

- 用户明确授权完成 v2.16 代码、测试、生产发布和迁移；发布后先迁移 2 个账号验收，再决定全量。
- 已启用 safe-production-change、release-recovery 和 planning-with-files 合同。
- 已读取 checkout、worktree、生产运行、mutation 和 release gate 基线。
- 已在独立 worktree 完成 canary 核心的 additive schema、迁移状态机、MY worker、OSS 不可覆盖对象副本、全局 inventory sequence、receipt 幂等重放、恢复探针、设备清理 48 小时本地判定、账号详情和业务调用隔离。
- 生产只读盘点确认 391 个 App C/SV 迁移候选：276 个已有 App A 主 + App B standby_1，可从账号 24/25 进入 canary；115 个为 App B 主 + App A standby_repair，必须先经真实 Telegram 身份探测转正。
- 新增正式生产脚本：DR runtime preview/apply、迁移 preview/approve/readback、SV redundancy repair preview/apply；不使用手工 SQL 迁移 Session。
- 定向 no-PostgreSQL 测试当前 19 passed；真实 PostgreSQL Alembic、完整相关回归、前端 build、CI、部署和 Telegram E4 尚未完成。
