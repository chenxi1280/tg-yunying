# Findings & Decisions

## Requirements

- 解释线上 AI 活群及最近新建任务不发送的根因。
- 设计整体修复方案并完成代码、QA、发布、生产历史积压恢复和线上验证。
- 同时覆盖 AI 活群与浏览任务，不能把 service health 或部署成功当成业务完成。

## Production Findings

- 当前已知生产 SHA 为 `4b080062c1c88f50c0f70e8898dc3394c4c79aa4`，运行时健康；这只证明部署/运行时，不证明履约。
- 多个 AI 任务当前到期量只有数百，但存在数千个 `generation_pending` Action；天津任务还出现大量 observing admissions，首个断点位于 Planner/open obligation 核算与调度。
- 发布后已有部分 AI remote message facts，说明 Gateway/Telegram 路径不是全局中断；问题是到期义务被重复物化且执行节奏被推远。
- 浏览任务发布后存在 ViewRemoteFact 且 remote_fact_gap=0，但绝大多数当前到期 Action 仍 pending；样本 scheduled_at 被推到当日晚间，符合二次 pacing。
- 浏览目标 1000/消息高于当前约 797 个可用账号，属于真实容量缺口；应明确失败/容量不足，不能靠代码伪造。

## Code Findings

- `group_ai_chat._daily_group_due_state()` 用 `_valid_open_daily_send_count()` 扣减开放量。
- `_valid_open_daily_send_count()` 仍通过旧 `GroupBotAdmission` 判断可规划性，而 `fact_first_v3` 当前权威事实是 `TaskGroupBotAdmission`；有效 held/open Action 因此可能被算成 0，Planner 每轮重复补量。
- AI `_schedule_times_for_plan()` 同时使用 pacing template 和 `reserve_task_schedule_times(enforce_task_spacing=True)`，把已由自然到期曲线释放的 debt 再按任务级最小间隔串行化，并缺少 ledger deadline 约束。
- 浏览已经禁用 reservation 的 task spacing 且传 deadline，但 `_view_schedule_times()` 仍对当前 due gap 使用完整 pacing 模板，形成同类二次摊速。
- generation worker 的 per-group pipeline 限制会放大未来调度的影响，但不是重复物化的根因；只加 worker 不能修复。
- `TaskDayLedger.deadline_at` 是 UTC storage；SQLite 会丢失 timezone。直接与 `_now()` 的北京时间 naive 值比较会把北京时间 23:59 误判为越过 UTC 16:00，current due 必须显式按 UTC storage 转成北京 wall-clock。
- fact-first 的 pre-dispatch skip 在无 remote fact 时不会自动投影 coverage/content-mix；新增截止守卫必须当场同步 owner 状态，否则 skipped Action 仍会留下未收口数量身份。

## Technical Decisions

| Decision | Rationale |
|---|---|
| 当前兼容层开放义务以同 TaskDayLedger 的 quantity slot + Action lifecycle 为准 | 不再依赖 legacy GroupBotAdmission，同时避免旧任务日 Action 阻塞新账本；TaskGroupBotAdmission 仍由生成/Gateway准入门禁负责 |
| confirmed 与 valid open obligation 共同抵扣 due，终态失败释放后才能重补 | 避免每轮 Planner 重复物化，同时保留失败恢复能力 |
| 当前到期 debt 使用 earliest-safe 调度，task-global spacing 关闭 | due curve 已完成任务级节奏控制；账号级安全约束仍在 Dispatcher/Gateway |
| 所有 schedule 必须 clamp/拒绝越过 ledger.deadline | 防止当日义务跨日仍占用开放量 |
| cleanup 只触碰 pre-Gateway 且可逆释放的错误义务 | unknown/已开始远端变更不能自动重发 |

## Product Handoff

- 设计文件：`docs/03-feature-designs/production-due-backlog-containment-prd.md`
- design_status：`product_design_complete`
- dev 范围：current AI open owner 计数分流、AI/view earliest-safe due schedule、跨日 AI pre-Gateway 受控恢复入口。
- 明确边界：本次是当前生产 writer 的完整事故止血，不宣称长期 `AiGroupMessageObligation`/浏览 due-unit fleet 接管已完成。
- 发布顺序：代码先上线并证明不再增长，再 preview/apply/readback；apply 后不得回滚旧 writer。

## Issues Encountered

| Issue | Resolution |
|---|---|
| GitHub HTTPS fetch TLS 握手失败 | 不重复同一失败；先完成本地设计/实现，发布阶段改用可用认证/Actions 路径并再次刷新 refs |
| 首次发布 CI 的 5 个历史时间测试被截止守卫跳过 | 测试 helper 原先只冻结 Planner/Generation 时钟；同步冻结 Dispatcher，并将明确使用 SQLite 的 4 项归入 no_postgres，生产逻辑不降级 |

## Resources

- `backend/app/services/task_center/executors/group_ai_chat.py`
- `backend/app/services/task_center/executors/channel_view.py`
- `backend/app/services/task_center/schedule_reservation.py`
- `backend/app/services/task_center/ai_generation_parallel.py`
- `backend/app/services/task_center/ai_generation_worker.py`
- `.github/scripts/task_fulfillment_e4_diagnostics.py`
- `.github/workflows/production-task-monitor.yml`

## Unproven

- production `generation_pending` 中 overdue/lookahead/future 的精确拆分尚未刷新成功。
- 需要发布后新的两轮 typed remote facts 才能声明 `production_fixed`。
- 完整 no_postgres 与 PostgreSQL 分区尚待 CI：本地完整套件触发 60 秒硬超时，本地无真实测试库配置。
