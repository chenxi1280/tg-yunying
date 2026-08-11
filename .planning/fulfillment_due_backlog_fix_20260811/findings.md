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

## 2026-08-11 18:38 生产剩余缺口刷新

- 生产仍为 `1565164718f9adf40971f6be9e64956af7be1551`，release 与六个核心容器健康。
- AI 不发送故障已恢复：五个任务发布后分别存在数百到上千条 successful Attempt + 非空 remote_message_id。
- 低目标任务接近 current due：郑州楼凤 due/confirmed=`613/604`，郑州师范=`615/606`；剩余主要是 admission、FloodWait、权限、session 与 unknown 账号级结果。
- 高目标任务无法在本自然日追平：郑州大学=`2752/1300`，西安天上人间=`3306/1141`，郑州学生会=`3306/1590`。它们仍持续发送，但 current gap 分别为 1452、2165、1716。
- AI generation 现场有 21 条 claim，集中在郑州大学；Action 查询出现 32–53 秒 active、约 66 秒 transaction age、DataFileRead/BufferIO，但 blocking edge=0。首个剩余技术断点是 generation 候选查询/公平 claim/内容漏斗吞吐，不是数据库锁或 Gateway 全局故障。
- `4fc393df...` 已被置为 failed，last_error 为目标实体无法解析；但 source 仍 active，且已有 `post_release_remote_fact_count=5915`、`remote_fact_gap=0`。代码 `_abandon_unusable_fact_first_account()` 会把任意账号 `PEER_INVALID` 直接升级为 Task terminal，已确认违反现有合同；该生产个案是否确属误终结仍待同 peer 的失败后成功 fact 或权威 reprobe。
- `4fc393df...` 当前 required/materialized/confirmed=`9516/6567/6566`，Task 停止后 materialization gap=2949。
- `fa75ca69...` 仍 running，required/materialized/confirmed=`1384/1378/1136`，post-release ViewRemoteFact=1127，remote_fact_gap=0；当前物化只差 6，但最终 lifetime 目标仍超过 distinct 账号容量。
- 本次 monitor run `31483093633` 因真实 blocker 返回 failure；它已完整输出逐任务 E4 summary。一个历史浏览 Task ID 输入已不存在，需从权威 Task 列表重新解析，不能猜 ID。

## 2026-08-11 Release A Gate 0 与实现结论

- `4fc393df...` 同一秒内账号 947/949 返回目标实体无法解析，而账号 946/948 对同一 Task/频道消息产生成功浏览；这已闭合“目标仍可用”的生产反证，允许进入误终态修复，不需要破坏性 reprobe。
- 根因不是 Telegram 频道全局失效，而是 dispatcher 将任意 fact-first `PEER_INVALID` 直接调用 `_terminalize_fact_first_target()`，把账号 cache/session 视角失败错误升级为 Task terminal。
- 第二个 owner 闭合缺口是 fact-first finalizer 在无 typed remote fact 时提前返回，pre-Gateway 失败/跳过可能留下派生 obligation 绑定；修复后所有结果都会投影 derived owner。
- 频道 obligation 的释放不能只看 Action terminal：无 Attempt/未进 Gateway 视为安全未执行；已进 Gateway 必须读取 `GatewayRequestEvidenceJournal`，仅 `false` 释放，`true|unknown` 保持 unknown。
- 生产恢复不修改历史 Action/Attempt/ViewRemoteFact。preview 必须证明短窗口 PEER_INVALID journal 全为 false、Task false-terminal shape 成立、terminal 后存在 ViewRemoteFact；apply 只恢复 Task running、再增 lifecycle epoch、清理错误 terminal 投影并写 AuditLog，由正常 Planner 释放旧 terminal binding 和创建新 Action。

## 计划复核发现

- 原计划把 `4fc...` 的生产根因写得过强：代码违反既有 PEER_INVALID 合同是已证实事实，但该生产个案是否确为误终结仍需同 peer 的失败后成功 fact 或权威 reprobe。
- 原恢复计划只有 Task epoch/state hash，缺少失败 Action/Attempt、target lifecycle/source revision、Gateway/unknown 集合与 obligation binding 守恒；直接恢复可能留下旧 epoch writer 或重复物化。
- 原 AI 验收“每 5 分钟 confirmed_delta >= due_delta”对自然抖动过严，又不能证明历史 debt 可在 deadline 前完成；应改为 15 分钟趋势窗口、catch-up ETA，以及下一完整自然日 settlement。
- 21 个 generation claim 集中单 Task 是风险而非公平性根因证明；慢查询、claim 公平、内容质量漏斗必须先定首断点后分支实现。
- 当前自然日的大 gap 含发布前 outage debt，不能用突发补发、跨日搬债或放宽质量门禁追平。
- Batch C 混合了账号运维事实、remote reconcile 和 slot mapping 代码缺陷，必须拆开所有权与发布路径。
- 浏览详情已有容量字段，但仍以 Action 行数推导，不符合 current TargetSet/DueSet/MaterializedSet/ViewRemoteFact 合同；Batch D 的重点应是 read model 真相源，而不是单纯增加 UI。
