# Progress

## 2026-08-20

- 用户明确授权完成 v2.16 代码、测试、生产发布和迁移；发布后先迁移 2 个账号验收，再决定全量。
- 已启用 safe-production-change、release-recovery 和 planning-with-files 合同。
- 已读取 checkout、worktree、生产运行、mutation 和 release gate 基线。
- 已在独立 worktree 完成 canary 核心的 additive schema、迁移状态机、MY worker、OSS 不可覆盖对象副本、全局 inventory sequence、receipt 幂等重放、恢复探针、设备清理 48 小时本地判定、账号详情和业务调用隔离。
- 生产只读盘点确认 391 个 App C/SV 迁移候选：276 个已有 App A 主 + App B standby_1，可从账号 24/25 进入 canary；115 个为 App B 主 + App A standby_repair，必须先经真实 Telegram 身份探测转正。
- 新增正式生产脚本：DR runtime preview/apply、迁移 preview/approve/readback、SV redundancy repair preview/apply；不使用手工 SQL 迁移 Session。
- 首次标准发布已进入真实 PostgreSQL gate，但 `0157` 在 CI 的“当前模型先建表、再执行全量 Alembic”路径重复建表而失败；流水线在构建和部署前停止，生产仍保持旧 SHA，未发生半发布。
- 已补充 `0157` 完整目标 schema 幂等识别和对应回归；定向迁移测试 13 passed，完整相关 no-PostgreSQL 回归 270 passed。真实 PostgreSQL CI 复验、部署和 Telegram E4 尚未完成。
- 第二轮流水线 `32345671294` 已越过重复建表，确认 `0157` 实际执行问题已修复；随后暴露两处旧 migration-head 断言和 8 个旧账号安全合同用例，构建/部署仍被 gate 阻止，生产未变化。
- 已将旧用例同步到设备清理 v2 合同，并把硅谷普通安全批次请求 `standby_2` 改为明确 `manual_required`，避免无 MY 耐久化事实时假成功；同时修复零目标成功时误报 `partial_failed`。原失败场景 9 passed，完整相关 no-PostgreSQL 回归 273 passed。
