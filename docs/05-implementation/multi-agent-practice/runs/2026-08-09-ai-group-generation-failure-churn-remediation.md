# 2026-08-09/10 AI 活群失败风暴 + 频道浏览饥饿整体修复运行记录

## Intake Card

- `intake_id`: `intake-2026-08-09-ai-group-generation-failure-churn-001`
- `source`: user
- `raw_input`: 线上 AI 活群任务有时未启动；“西安天上人间”仍拥堵且有大量失败消息；同时要求修复浏览任务问题，由本任务完成设计、代码、发布、线上验证并持续监督到真实修复。
- `created_at`: `2026-08-09`
- `owner_agent`: `product`
- `affected_surface`: `group_ai_chat + channel_view / fact_first_v3 / TaskDayLedger / AI monotonic quantity identity + active due rank / ChannelView peer-message target + due ordinal + active-time clock / Planner / logical source listener / Generation / Dispatcher / Gateway / permanent facts / settlement / Task Center / E4`
- `production_related`: `true`
- `level/lane`: `L3 / ai-group-quality/message-obligation-failure-churn`
- `initial_evidence_level`: `E0-E4 mixed`；既有批次修复有真实 remote-fact 样本，本轮失败风暴整体修复尚未实现
- `next_route`: `product_design_complete -> dev -> qa -> product -> prod-diagnosis`

## Triage Card

- `route`: standard L3；不是 quick fix。
- `release_gate_required`: true。
- `production_verification_required`: true。
- `reason`: 问题跨数量真相源、内容合同、生成重试、Gateway unknown、防重、生命周期 API、迁移、worker、API/UI 与生产接管；局部重试或单字段补丁会继续制造重复 Action 或重复发送风险。
- `design_truth`: `docs/03-feature-designs/ai-group-generation-failure-churn-remediation-prd.md` 与 `docs/03-feature-designs/channel-view-planner-starvation-remediation-prd.md`。
- `superseded_truth`: legacy `TaskGroupDailyMessageSlot/ContentMix` 发送真相、Action-only extra-volume identity、全局签到 10 天去重、waiting 热轮询、先 activation 后 takeover。

## 已确认根因与边界

1. check-in 使用账号级跨 Task/群 10 天正文去重，两个缺面具 coverage scope 在其他 Task/群签到后被反复拒绝；失败发生在 Gateway 前。
2. extra-volume 没有稳定业务义务和 variation identity；新 Action 重置生成上下文，重复命中 `duplicate_message`。
3. 面具进入 `manual_required` 后，任务侧没有 durable waiting/wake，Planner 继续物化相同 blocker。
4. 页面把 terminal pre-Gateway failed/skipped 混入 active backlog，形成“Dispatcher 拥堵”误读。
5. 已上线的 bounded multi-Action Planner 修复只证明“一轮不再固定 1 条”；不等于本轮失败风暴已修，也不等于自然日 4800 完成。
6. 浏览Planner把Task最晚future Action作为新批floor并整体平移；现网Action尾部到23:57后新批全部越过deadline，故0 Attempt/0 ViewRemoteFact，不是Dispatcher backlog。
7. 浏览Task级template 180秒间隔把理论上限压到约480/日；账号池约869/871又小于单消息1000 lifetime identity目标，必须分别修调度并报告structural shortfall。
8. 浏览E4把已物化obligation行数当required且没有source projection，既可能31/1370假绿，也会把healthy-empty与listener故障混为一类。

## Product Design Complete 复核轨迹

- 第一轮产品设计已闭合 stable obligation、aggregate content allocation、intent/variation、scoped check-in、durable wake、legacy additive alias 与基础 E4。
- 实现就绪复核重新发现管理命令旁路、三层 route/epoch、TaskGroupDailyTarget identity/due、Gateway call-issued 崩溃窗、remote fact projector 无 durable drain、takeover 顺序/分类、wake ownership、成功事实唯一与 CAS 顺序等 P0，因此设计状态降回 `partial`。
- 当前修订已逐项写入专项 PRD，并同步主 PRD、DF-193D 与旧 AI 群日专项 supersede。
- 2026-08-10最终独立fresh reviewer已对冻结快照明确给出`product_design_complete pass`，未发现阻断实现、迁移、发布或生产E4的P0/P1；review结果只开放dev，不替代实现或线上证据。

## Product Handoff 范围

### E0：设计与基线

- tenant AI-message fleet policy/逐Task inventory item负责legacy allowlist/default current，且不污染Dispatcher runtime contract；task enrollment一旦preparing永久fence legacy。quantity ordinal只承担永不复用identity，`effective_due_rank`才进入DueSet；目标down→up或stop-safe→start时retired rank可由更高ordinal重占，boundary owner为protected overage且不能补低rank缺口；stop未重启不缩target，deadline空rank为known shortfall。每个obligation最多一个bound quantity fact，第二真实fact仍append为typed unbound conflict。AI user-start固定Fleet/Inventory→Enrollment→Task→TaskStartOperation锁序；automatic rollover区分normal-running关闭旧route与takeover-closed只读旧closed route两种source mode，均不得绕settlement/blocker或重开旧route。
- aggregate ContentAllocationPlan/RequirementAssignment跨多个最多20条技术批次守住reply/material/act-type；旧Cycle不是current owner。check-in只完成原数量/coverage义务，不伪造normal内容完成。
- waiting 由 durable revision/time subscription 唤醒；recovery 使用 partial-index keyset + 逐行 CAS，不恢复退役锁模型。
- channel_view由tenant+peer+message target+due ordinal持有业务量，账号只作pre-Gateway绑定；route accrual segments只累计running时间。整个ledger把Gap与未用lifetime identity×账号slot做稳定最大匹配，禁止Task future-tail/180秒全局间隔；所有CAS/readback/replay/Release Gate hash共用`channel_view_contract_hash_v1`规范serializer并持久version。legacy/current Planner与Tx A共用数据库唯一LifetimeIdentityOwner，safe pre-transport才释放。logical source event/delta/subscription、activation-gated target expiry、Action/fact single-owner binding、永久ViewRemoteFact+append-only observation、blocker occurrence、activation-gated settlement、bootstrap/adoption/fleet/final-manifest takeover均有durable owner；same remote identity重放不造第二fact，不同identity对同due unit的第二fact保留unbound conflict。
- dev 必须从合并时最新`master`新建干净工作树；2026-08-10已知`origin/master=6db995cb2cc5c94b805b6647219cbd060269a59a`且新增独立`worker-material-cache`，开工前仍须再次fetch。

### E1：实现与数据库证明

- additive models/migration、AI typed state/FOP与rank allocator/route/lifecycle/Gateway/projector/wake/settlement；浏览target/due assembler、active-time segments、账号slot匹配、logical source delta/fanout/expiry、Action/fact binding+tombstone、full bootstrap/adoption/settlement/fleet/takeover；typed API/UI与E4工具完成。
- 同一候选 SHA 通过 pure regression、真 PostgreSQL unique/CAS/crash/deadlock/takeover/API tests；migration upgrade 与生产规模 EXPLAIN 通过。
- generic retry/reset 对 current route 返回 typed 409，不能把 failed/unknown Action 原地 pending。

### E2：QA 与产品接受

- QA 分别报告 pure、PostgreSQL、frontend、migration、worker-role 与 diagnostic 结果；blocked collection 不是 pass。
- Product 对数量、内容、等待、unknown、lifecycle、迁移守恒与页面四层读模型验收；`qa_pass` 不自动等于 `product_accepted`。

### E3/E3.5：发布与接管

- canonical `master -> release -> Deploy Production`；核对同一SHA、migration revision、AI role的`ai_message_enrollment_fence_v1`、AI依赖producer的`ai_message_dependency_producer_v1`、浏览backend/planner/全部dispatcher/recovery/listener的`channel_view_due_unit_fence_v1`及浏览source writer的`channel_view_source_event_producer_v1`与逐writer bitset/heartbeat，旧实例/lease=0；浏览兼容基线还必须完成legacy/current共用LifetimeIdentityOwner的fact/Gateway/open-Action backfill与零缺行count/hash readback，才允许mixed fleet Dispatcher继续。
- 新route默认未激活；受保护workflow执行rough preview → enrollment/route preparing + source fence → pre-call quiescence → final immutable manifest → chunk checkpoint apply + allowed delta → conservation readback → class-specific atomic active/closed/current。same-period running/paused/stopped最终CAS还必须以数据库时间证明仍在`[period_start,deadline)`；跨过或恰等deadline保持preparing并supersede为settling-closed/rollover-eligible manifest，禁止激活过期route。AI接管closed route结算完成后只能由`takeover_closed` bootstrap source mode建下一ledger；浏览zero-history、live-settling、rollover与terminal-settling/retired必须恰好命中一类，terminal永不自动start。
- takeover 失败保持 preparing/blocked+paused；绝不恢复 legacy writer。
- canary E4 后逐 Task 接管冻结 legacy inventory；所有 resumable Task enrollment/readback 完整后才切 fleet active contract。

### E4：真实生产闭环

- “西安天上人间”取得`Task/enrollment/ledger/route/target -> allocation/assignment -> obligation/intent/variation -> Action -> Attempt -> remote fact -> quantity binding(timeliness) -> target/coverage/read-model -> settlement`。
- 同 blocker basis 不新增 Action/GenerationJob；check-in 跨 Task/群不互阻；extra-volume 有真实远端成功；unknown 无 replacement；projector/wake heartbeat 与 lag 可对账；UI 与数据库一致。
- 浏览canary取得`Task/enrollment/route -> logical source projection -> peer-message target/DueSet -> legacy/current LifetimeIdentityOwner -> ActionBinding/Action -> Attempt -> permanent ViewRemoteFact/RemoteFactObservation/single-owner binding -> immutable settlement`；同identity重放与不同identity/同due冲突均有typed observation/binding+blocker occurrence且不双确认。31 obligations/1370 due必须失败；仅在同一冻结资格/Session/slot fixture确为869/1000时，才要求物化869并明确131 structural shortfall。global owner/fact/Gateway/open-Action分类count/hash、source fanout/expiry/bootstrap/adoption/settlement/projector各owner的heartbeat、lag、lease、count/hash和fleet分类同时闭合。
- `production_fixed` 与 `natural_day_target_met` 分开；自然日 4800 未结束或未达标时继续 `unproven`。

## 当前状态

- `product_design`: `product_design_complete_pass`
- `implementation`: `not_started`
- `qa_pass`: `not_done`
- `product_accepted`: `not_done`
- `release_gate`: `blocked_until_implementation_and_qa`
- `production_effect`: `unproven`
- `natural_day_target_met`: `unproven`
- 本记录没有修改业务代码、生产配置或生产数据，也没有触发发布。
