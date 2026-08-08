# Task Plan: AI extra-volume mask recovery production fix

## Goal
修复 `fact_first_v3` AI 活群 extra-volume 错选缺面具/非当日已确认账号造成的发送前失败循环，并通过 `master -> release -> GitHub Actions` 发布后取得新的 Telegram E4。

## Current Phase
Phase 4

## Phases

### Phase 1: Product Design Complete
- [x] 复核用户症状与生产第一断点
- [x] 补齐 PRD、专项设计和数据流/结构索引
- [x] 明确失败、并发、幂等、迁移、回滚与 QA 口径
- **Status:** complete

### Phase 2: Reproduction and implementation
- [x] 先写失败回归，覆盖候选资格与单账号不饿死其他账号
- [x] 实现最小修复并清理本次产生的无用代码
- [x] 更新结构索引
- **Status:** complete

### Phase 3: QA and release gate
- [x] 运行定向 no_postgres 测试与静态检查；PostgreSQL 分区交由 Actions
- [x] 运行受影响测试集合、git diff --check
- [x] 完成 Release Gate 记录
- **Status:** complete

### Phase 4: Master and release promotion
- [ ] 提交不可变候选 SHA
- [ ] 推送 master
- [ ] 本地 merge master 到 release 并推送 release
- [ ] 监控 GitHub Actions Deploy Production
- **Status:** in_progress

### Phase 5: Production verification
- [ ] 核对生产 SHA、容器与 heartbeat
- [ ] 复核目标任务不再生成 `account_mask_evidence_missing` 循环
- [ ] 取得新 Action、成功 Attempt、非空 remote_message_id 与 coverage/群总量推进
- **Status:** pending

## Key Questions
1. extra-volume 候选的权威资格是否为当日 confirmed coverage + active mask + 当前准入/在线？
2. 当部分账号缺面具时，是否明确跳过该账号并让其他合格账号继续，而不是发送签到或终止整个任务？
3. 如何防止同一不合格账号每 tick 重入，同时保持缺面具恢复状态可见？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 使用独立 clean worktree | 主 release 工作区存在大量用户改动 |
| 基于 origin/master 489d41eb 开发 | origin/master 与 origin/release 树相同，历史分叉但无树差异 |
| 不允许缺面具账号承担 extra-volume | 专项面具 PRD 与既有测试明确该边界 |
| 修复候选资格而非放宽 Dispatcher 门禁 | Dispatcher fail-closed 正确，根因在 Planner 候选层 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 生产聚合查询对 JSON outcome 分组失败 | 1 | 显式转为文本并改用压缩聚合 |
| 生产聚合查询覆盖统计作用域错误 | 1 | 拆分状态与 blocker 查询 |
| 远端分支无法 fast-forward | 1 | 证明两端树相同，计划用 master 候选后 merge 到 release |
| 两个协作模板文件名与猜测不一致 | 1 | 使用目录中的 agent-handoff / validation-report 实际模板 |

## Notes
- 生产基线：`9d99319f`。
- 目标任务：`a52e84f2...`，最后远端成功 20:57，22:48 已有 92 个发送前失败。
- 所有生产验证保持 Task -> ledger -> Action -> Attempt -> typed remote fact。
