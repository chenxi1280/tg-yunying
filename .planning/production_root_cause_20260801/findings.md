# Findings

## Requirements
- 解释当前线上任务异常的原因。
- 通过生产证据和独立代码审查交叉确认。
- 不修改生产、代码或任务状态。

## Research Findings
- 2026-08-01 10:38 前序采样：线上 `87fe0bf0`，核心容器 healthy，当前 DB 无锁等待。
- 三个 AI 活群最后成功均早于约 09:35 的新版本容器启动；上线后尚无新增业务确认。
- PostgreSQL 在 10:04、10:13 出现 Dispatcher allocation/claim 相关 deadlock。
- 搜索点击从 305 增至 311，说明 Gateway/验证链并非完全停摆。
- 实际两个 Dispatcher 都配置 `ACCOUNT_SHARD_TOTAL=2`；backend/planner/recovery/AI generation 为默认 `ACCOUNT_SHARD_TOTAL=1`。
- 实时共享调度窗口在相邻分钟出现 `shard_total=1`、`shard_total=2` 或两者跨 epoch 混存；这不是静态推断，而是生产 DB 实际状态。
- 搜索执行路径会用默认 `shard_total=1` 提前创建/分配共享窗口，而 Dispatcher 只能精确消费自身 `(shard_total=2, shard_index)` 的 reservations。
- 搜索严格需求常占 25–28 个容量位，target admission 等严格类别继续占位；普通 AI 虽有大量已生成且 overdue 的 ready Action，却没有活跃 claim。
- 跨 epoch 保留旧 allocations 会把旧 `unclaimed_allocated_count` 继续计入容量；1 分片 writer 抢先后，2 分片 Dispatcher 的可用容量进一步被压缩，并反复重建窗口。
- AI 文本生成正常，多个 Action 在 10:12–10:25 已达到 `generation_ready`；共同停点在 claim/allocation，不是模型生成或 Telegram Gateway。
- deadlock 日志证明锁环涉及 `dispatch_claim_scopes FOR UPDATE` 与 `dispatch_claim_task_allocations` 插入触发的 `tasks` 外键 KeyShare 锁；需继续定位另一侧 Task→scope 的具体调用事务。
- 当前最强共因是同一共享窗口的分片拓扑不一致和容量争抢；deadlock 是该冲突的并发现象和放大器，不是唯一原因。
- 线上 Dispatcher 参数为 `DISPATCHER_SCOPE_CAPACITY=52`、每个 worker `DISPATCHER_CONCURRENCY=20`、`DB_POOL_SIZE=5`、`DB_MAX_OVERFLOW=10`；代码将单 worker 有效并发压到 13，因此两 worker 合计最多约 26 个执行槽，却可预分配 52 个 60 秒窗口容量。这会放大 search assignment expiry。
- 10:57 复采样：搜索 confirmed 已由 311 增至 329；三个 AI 仍为 83/80/59，继续零增长。10:14 后 PostgreSQL 未新增 deadlock，因此 deadlock 不是 AI 持续不发的唯一原因。
- 10:57 AI 当前 open 队列均无 active claim：郑州大学 pending/due/ready-text=104/101/86，郑州师范=139/137/86，郑州楼凤=205/200/99。
- 新版加入 `group_content_scope_v1` 硬合同；历史 Action 缺 scope 时即使已有正文也在生成 guard / Gateway 前被拒绝。当前 pending missing-scope 数为 1/25/77，是滚动发布兼容断层，但不能解释郑州大学绝大多数合法 ready Action 也完全无人领取，因此它是第二层阻塞而非唯一共因。
- 郑州师范当天 blocker 已实证：`context_freshness_unproven` 214、`context_expired` 115、`reply_target_missing` 83、`post_send_intercepted` 45；楼凤主要为 `group_bot_admission_wait` 1864、legacy intercepted 278、duplicate 303、生成输出数量不匹配 63。
- deadlock 精确锁序：allocation 事务先锁 scope，再 flush task allocation INSERT 并因 FK 请求 Task KeyShare；AI quality/finalize 事务先显式 `Task FOR UPDATE`，随后 release dispatch claim 请求 scope，形成 Scope→Task 与 Task→Scope 的逆序。
- 评论任务当前 140 个 Action 全是未来计划，最早 11:00（Asia/Shanghai），10:56 时 due=0；此前把“141 个 obligation pending”描述为停滞不准确，当前没有证据证明评论阻塞。
- 点赞任务中天津新闻、郑州吃瓜、郑州精品均 pending=0/due=0，属于 dynamic_new 没有待执行输入；郑州楼凤仍在 10:48 成功，阿哥日记有 3 条 due 且无 active claim，后者与共享调度拥塞一致。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 分析 Task→Action→Attempt→remote fact | 避免把任务状态和业务完成混为一谈 |
| 对照部署前后时间线 | 判断问题是历史债还是新版本相关回归 |
| 不把当前 merge commit 直接定为引入点 | `87fe0bf0` 的 tree 与第一父提交相同，需按具体历史改动和实时行为归因 |
| 把 scope 合同断层与调度共因分层 | 合法 ready Action 同样无 claim，不能把全部 AI 停发只归因于历史 Action 迁移 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| release recovery skill is placeholder | 使用项目 AGENTS.md、生产文档和真实环境证据 |

## Resources
- `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`
- `backend/app/services/task_center/dispatcher.py`
- `backend/app/services/task_center/dispatch_reservations.py`
- `.github/scripts/diagnose_ai_group_blockers.py`
