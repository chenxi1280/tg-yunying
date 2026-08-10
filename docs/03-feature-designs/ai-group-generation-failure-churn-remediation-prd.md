# AI 活群生成失败风暴整体修复 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| Intake ID | `intake-2026-08-09-ai-group-generation-failure-churn-001` |
| 问题级别 | L3：生产 AI 活群大量 pre-Gateway 失败反复物化，任务吞吐和完成判断失真 |
| 设计状态 | `product_design_complete`；2026-08-10最终独立fresh复核零P0/P1，允许按本冻结合同进入dev；实现、迁移、QA、发布与生产E4仍未开始 |
| 适用任务 | current `group_ai_chat + fact_first_v3` |
| 首个生产验收对象 | “西安天上人间”当前 Task；不得删除重建、重置当日目标或改写历史成功 |
| 产品目标 | 消除相同业务欠额在事实未变化时重复创建失败 Action；保留内容质量、准入、账号安全、Gateway unknown 与真实远端事实合同 |
| 非目标 | 不改变自然日 pacing、20 条 Planner 有界批次、4800 目标、内容质量阈值、Dispatcher 并发或 Telegram 门禁 |

本文补齐以下 current-contract 实现缺口，并 supersede 所有与之冲突的历史实现描述：

- `签到` 只按 `(task_id,group_id,account_id,task_day_ledger_id)` 取得一次业务资格，不进入普通正文账号级滚动 10 天去重；
- coverage 与 extra-volume 均先取得稳定的 AI 消息义务，禁止无 coverage 时退化为 Action 自身身份；
- pre-Gateway 质量失败只终结当前尝试，不创建新的业务欠额；新的 Action 必须复用同一义务并使用可证明的新 variation；
- `content_capacity_gap` 和 `quality_waiting_context` 是等待事实，不是可立即领取状态，也不是成功或目标缩减；
- legacy `TaskGroupDailyMessageSlot/ContentMixCycleSlot` 不恢复为 `fact_first_v3` 真相源；reply、关系和素材要求改由 current AI content intent 冻结承接；
- 历史失败 Action 保留审计，但不得继续被页面解释为实时队列拥堵。

## 2. 生产事实与根因分组

2026-08-09 21:44 +08 的只读生产快照显示：最近 60 分钟成功 52 条、失败 140 条，成功均有 Gateway/远端消息事实，失败均未进入 Gateway；Dispatcher active 0/26，只有一条未来 pending，因此不是执行队列或 Gateway 堵塞。

### RC-1：签到去重作用域错误，形成无限 coverage 重建

- 两个缺 active 面具的 coverage 账号在其他 Task、其他群当天已有真实 `签到` 成功；
- 当前实现按 `tenant_id + account_id + raw_text=签到 + 10 天窗口` 查询，跨 Task/群阻断当前 scoped check-in；
- 失败后 coverage 被释放回 `ready`，`next_eligible_at=NULL`，Planner 在下一轮再次创建相同失败 Action；
- 当日累计 `direct_check_in_10d_duplicate` 327 次，最近 60 分钟 100 次，集中于两个账号；
- 这批失败没有 ExecutionAttempt/Gateway，不代表 Telegram 拥堵。

### RC-2：extra-volume 没有稳定义务和 variation，重复生成同类内容

- 最近 60 分钟 `duplicate_message` 39 次，全部为 extra-volume；
- 这些 Action 均无 `content_variation_key`，同一账号可产生多个 generation ID 却持续命中同一 duplicate reference；
- 当前 `_with_content_variation_key()` 对无 coverage 的 Action 提前返回，新 Action 身份让生成流程误以为是新欠额，并重置内容尝试上下文；
- 当日累计 `duplicate_message` 258 次，全部在 Gateway 前被质量门正确拒绝。

### RC-3：账号面具资产已进入人工处理，但任务侧没有稳定等待语义

- 两个相关账号的面具生成均在 4 次尝试后进入 `manual_required`，分别记录 provider timeout/unavailable；
- 没有 active 面具时不得生成普通正文或领取 extra-volume；合法 direct coverage 可使用一次 scoped check-in；
- scoped check-in 资格用尽后应形成可审计缺口，不能继续制造失败 Action。

### RC-4：页面把终态尝试历史呈现为执行拥堵

- 当前“已执行”筛选等于所有非 open Action，pre-Gateway failed/skipped 与真实远端成功混在一起；
- 运营看到的是高频终态失败历史，不是 active backlog；
- 页面需要同时展示业务义务、当前等待和尝试历史，不能只展示 Action 数。

## 3. 产品目标与验收边界

### 3.1 必须达成

1. 同一业务欠额只有一个稳定 AI 消息义务；相同事实版本下最多物化一个 Action 尝试。
2. 所有可执行 coverage 和 extra-volume obligation 都有非空、可审计的 allocation assignment 与 current content intent；正常正文必须另有非空 variation identity。确定性 check-in 不创建伪 variation，但必须冻结 scoped check-in claim、message-memory reservation 与 Gateway request identity。settlement 为从未物化的到期 rank 创建的 `terminal_shortfall(deadline_unmaterialized|deadline_unmaterialized_after_stop)` 是唯一不带 assignment/current intent 的终态审计例外，永远不可重新进入执行链。
3. check-in 跨 Task/群互不阻断；同 Task/群/账号/任务日最多一个 open/Gateway/unknown/confirmed。
4. duplicate/质量失败保留 fail-closed；只有新 variation basis 成立时才重新开放原义务。
5. 容量缺口和依赖等待不占 Generation/Dispatcher 槽，也不按固定 120 秒制造 Action。
6. Gateway-started/unknown 不重发；只有 typed remote fact 完成群日数量和可选 coverage。
7. 页面分别回答“还欠多少”“当前执行多少”“为什么等”“历史尝试多少”。

### 3.2 明确不做

- 不删除或改写已有失败 Action、Attempt、message memory 或 remote fact；
- 不通过缩短 Planner 间隔、提高并发、放松重复门、追加模板正文或伪造成功来提速；
- 不重新启用 legacy quantity slot/ContentMix 作为 current `fact_first_v3` 发送真相源；
- 不改变 `natural_full_day due_by_now`、quiet-hours 纠偏、单 Task 单次最多 20 条有界批次或任务轮转公平；
- 不把“失败风暴停止”声明成“自然日 4800 已完成”。

## 4. 核心对象与真相源

### 4.1 `AiGroupMessageObligation`：稳定消息义务

新增 typed obligation；每条当前到期、需要真实发送的消息单位只有一行。它是 current `fact_first_v3` AI 数量欠额的业务身份，`Action` 只是该身份的一次物化。

| 字段 | 合同 |
| --- | --- |
| `id/tenant_id/task_id/task_day_ledger_id/group_id` | 不可变归属；不得跨 Task、群或任务日改绑 |
| `obligation_subtype` | `coverage` 或 `extra_volume`；coverage 同时绑定唯一 `coverage_ledger_id` |
| `coverage_ledger_id` | coverage subtype 必填且唯一；extra-volume 为空 |
| `target_operation_target_id/quantity_ordinal` | 任务日目标内单调且永不复用的业务身份；不同 Planner decision 也不能重复 |
| `effective_due_rank/due_rank_state/rank_retired_reason/rank_retired_at` | 当前effective target内的到期位置；`due_rank_state=active|protected_overage|retired`，只有active参与当前DueSet。retired行保留原rank、`target_reduction|task_lifecycle_stop|scope_revision`原因与数据库时间，历史rank可由新identity重占但quantity ordinal永不复用 |
| `due_unit_key` | `hash(task_day_ledger_id,target_operation_target_id,quantity_ordinal)`，不可变 |
| `creation_decision_id/decision_slot_index` | 仅审计首次创建来源，不承担跨 decision 唯一性 |
| `target_revision_created/current_effective_target_revision` | 目标 CAS 分配与缩放审计；对应`TaskGroupDailyTarget.effective_planned_target_revision`，已 Gateway 单位不改写 |
| `account_id/account_binding_version` | coverage 账号不可变；extra-volume 只允许在未进 Gateway 且原绑定已明确释放时 CAS 换号 |
| `due_at/deadline_at` | 由 natural-full-day pacing 和不可变 task-day 边界冻结；future unit 不提前领取 |
| `route_epoch/task_lifecycle_epoch` | 每次物化冻结当前 task-day route 与 Task lifecycle；后续写必须匹配 |
| `state` | 见 §5；决定能否物化，不从 Action 数反推 |
| `active_action_id/materialization_version` | 同一时刻至多一个非终态 Action；每次合法再物化递增版本 |
| `active_generation_job_id` | 唯一当前 GenerationJob；waiting/terminal 必须为空 |
| `current_assignment_id/current_assignment_revision` | 当前aggregate allocation assignment指针；所有可执行义务必填并以deferred FK验证assignment反向绑定同一obligation，settlement-only终态例外为空 |
| `current_content_intent_id/current_intent_revision` | 当前不可变 intent 指针；所有可执行义务必填，任何 Action/GenerationJob/Gateway 写都必须匹配；仅 settlement 创建的 deadline-unmaterialized 终态例外为空 |
| `generation_epoch/generation_epoch_basis_hash/variation_sequence` | 外部事实基础与 variation 单调版本；同 basis 不重置 3+3 |
| `materialization_mode/check_in_trigger_reason/check_in_handoff_id` | `normal_body|deterministic_check_in|settlement_shortfall`；trigger仅`mask_missing|normal_generation_exhausted`。六轮耗尽分支必须指向immutable handoff，不能靠内存回调或新义务重跑；`settlement_shortfall`只能由SettlementTargetItem创建且不能被业务worker领取 |
| `created_by_settlement_target_item_id/terminal_shortfall_reason` | 正常可执行义务为空；settlement-only终态必填精确item，reason仅`deadline_unmaterialized|deadline_unmaterialized_after_stop`，用于证明assignment/intent空指针例外 |
| `blocker_code/blocker_stage/blocker_basis_hash` | 当前等待原因及其事实版本 |
| `deadline_at/version` | deadline 收口与所有状态 CAS |
| `confirmed_remote_fact_id/remote_effect_at/confirmation_time_basis/confirmation_timeliness` | 只有 typed remote fact 可写；basis=`remote_event_time|same_attempt_atomic_gateway_success|unproven`，timeliness=`on_time|late|unproven`，terminal unit可保存fact但不重开 |

数据库唯一性：

```text
UNIQUE(task_day_ledger_id, coverage_ledger_id)
  WHERE coverage_ledger_id IS NOT NULL

UNIQUE(task_day_ledger_id, target_operation_target_id, quantity_ordinal)
UNIQUE(due_unit_key)

UNIQUE(task_day_ledger_id, target_operation_target_id, effective_due_rank)
  WHERE due_rank_state IN ('active','protected_overage')

UNIQUE(remote_mutation_key_hash,gateway_request_hash,fact_kind)
  ON fulfillment_remote_facts
```

数据库CHECK必须把空指针例外写死：只有`state='terminal_shortfall' AND materialization_mode='settlement_shortfall' AND terminal_shortfall_reason IN ('deadline_unmaterialized','deadline_unmaterialized_after_stop') AND created_by_settlement_target_item_id IS NOT NULL`时，`current_assignment_id/current_assignment_revision/current_content_intent_id/current_intent_revision/active_generation_job_id/active_action_id`允许为空；该行必须从Planner、Generation、handoff、wake与Gateway全部claim predicates排除。除此之外，义务在提交时必须已有唯一active assignment，并由deferred FK验证current assignment与current intent都确实反向绑定同一obligation/revision；不能用nullable列放宽普通执行路径。

canonical `FulfillmentRemoteFact` 必须按 request/mutation/fact identity append，不能在 fact 表对 obligation 做 success unique，否则第二个真实远端副作用会因约束回滚而丢失 typed fact。fact additive保存 `remote_effect_at` 与 `confirmation_time_basis=remote_event_time|same_attempt_atomic_gateway_success|unproven`：只有Telegram/adapter权威远端事件时间，或同一Attempt成功回执与Gateway成功确认原子落库时间可作basis；普通`created_at/updated_at/reconcile/projected_at`永远不能替代。新增 `AiGroupMessageQuantityFactBinding`：每个 remote fact 唯一一行，保存 `remote_fact_id/requested_obligation_id/bound_obligation_id nullable/target_operation_target_id/binding_state=bound|unbound_obligation_conflict|orphan/conflict_with_fact_id/confirmation_timeliness=on_time|late|unproven/version`；`UNIQUE(remote_fact_id)`，并以 `UNIQUE(bound_obligation_id) WHERE binding_state='bound'` 保证每个 obligation 最多一个 quantity-confirming fact。ledger区间为`[period_start_at,deadline_at)`；只有basis权威且`remote_effect_at < ledger.deadline_at`为on_time，等于或晚于deadline均为late；时间/basis不可证明为unproven，不能计on-time。

每个canonical fact 另冻结 `projection_contract_version/required_projection_kinds/required_projection_count/required_projection_set_hash`，当前registry为 `ai_group_fact_projection_v1`。live current obligation必含`obligation,action,task_read_model,ai_group_quantity_target`；有coverage lineage再含`ai_group_coverage`，有claim/memory副作再含`ai_group_content_memory`；已归档删除Task的late fact只用`deleted_task_remote_tombstone`。kinds按UTF-8/C顺序去重并用规范serializer计算set hash，不得依赖当前代码默认列表猜测。Tx C在落fact的同一事务insert全部required `FulfillmentFactProjectionState`；唯一 `(fact_id,projection_kind)`。只写部分rows、count/hash不等或未登记kind时Tx C整笔失败，不能留下“缺行即完成”的假空集。

projector 对第一条合法 fact 创建 bound binding并 CAS obligation `confirmed_remote_fact_id`；同 obligation 的第二条真实 fact 仍正常 append，binding 写 `unbound_obligation_conflict` 指向首 fact，计入 typed `observed_overage_conflict_count`，并在同一事务打开enrollment-scope持久 contract blocker、永久禁止当前与后续任务日 replacement，但不确认第二个 obligation、不确认 coverage。TaskGroupDailyTarget 同时区分 raw observed remote count、bound obligation confirmed count与 unbound conflict count；冲突解决只能 additive adjudication，不能删除 fact或把原 request谎绑另一 ordinal。这样 fact真相源不丢，正常路径仍满足每 obligation至多一个成功绑定。

冲突裁决由 append-only `AiGroupMessageQuantityConflictAdjudication` 承担，数据库唯一 `(unbound_binding_id,decision_revision)`，保存 remote fact/binding/bound obligation/route identity、`conflict_snapshot_hash`、`decision=acknowledged_overage|validated_distinct_remote_identity`、独立远端证据ref/hash、operator、`approval_ref`、supersedes id与created_at；binding保存只读current adjudication pointer/version，但原`binding_state=unbound_obligation_conflict`、fact和计数永不改写。受保护 `POST /api/ops/ai-group/quantity-conflicts/{binding_id}/adjudications` 或同合同CLI使用现有权限 `system.manage`，要求独立`approval_ref + expected binding/route/source/read-model hash`并只append裁决；permission middleware与handler/service都必须校验该权限，tenant外对象统一404、同tenant无权限403，普通Task retry/reset和UI不得代做。

新增受保护 `POST /api/ops/ai-group/enrollments/{enrollment_id}/contract-reopen` /同合同CLI与 `operation=contract_reopen`，同样使用现有 `system.manage`，但必须使用**不同于裁决操作**的第二个 `approval_ref`。请求明确blocker IDs/scope、可选origin/current route，并提交expected enrollment+各route count/revision、blocker/adjudication/source/read-model hashes；tenant隔离仍为404/403且完整AuditLog。它只允许active enrollment，且Task/route处于current paused|stopped，或跨period latest route=`active+closed`并有current epoch adoption baseline；enrollment-scope runtime blocker的origin route必须曾`activated_at IS NOT NULL`。takeover `preparing|blocked` route及其migration-scope blocker只能回到同一takeover manifest修复，永远不能借contract_reopen激活。

owner-aware reopen逐blocker按registry校验current有效裁决/resolution：enrollment-scope以expected enrollment version/count/revision CAS `open -> resolved`、`open_contract_blocker_count-count,contract_blocker_revision+1`；只有registry明确`resolution_channel=contract_reopen`且origin route已激活的route-scope blocker才可同时以expected route count/revision收口，当前registry的takeover migration blockers不允许。所有未列blocker仍open；请求完成后两层count必须各自等于open rows readback，任一并发新blocker/A类事实/冲突先提交使整笔CAS失败。它不改confirmed/coverage/raw/unbound，不建义务/Action/ledger或调用Gateway。count归零后才可按既有resume/TaskStartOperation/bootstrap恢复；reopen本身不把closed route改running。这样一次真实双发可经审计恢复Task，但永远不会被伪装成第二个目标完成。

`TaskGroupDailyTarget` 为 current route additive 增加 `target_operation_target_id`、`legacy_target_id`、`base_target_revision/base_planned_target`、`effective_planned_target_revision/effective_planned_target`、`next_quantity_ordinal`、`raw_observed_remote_count`、`unbound_observed_remote_count`、`on_time_confirmed_message_count`、`late_confirmed_message_count`、`confirmation_time_unproven_count`、`post_settlement_confirmed_count`、`unresolved_terminal_shortfall_count`、`settlement_status/settled_at/settled_target_count/settled_on_time_confirmed_count/settled_late_confirmed_count/settled_time_unproven_count/settled_unknown_quantity_count/settled_known_shortfall_count/settled_shortfall_count/settled_protected_overage_count`、各settled quantity集合hash、`settled_rank_set_hash/settlement_snapshot_hash`、`version`、`pacing_snapshot_hash` 和 `target_effective_at`。base字段只在ledger bootstrap冻结；动态coverage/allowed observed overage只CAS effective revision/target、row version与read-model version，不改base。现有 `confirmed_message_count` 不新增同义总量列；其 current-route唯一语义冻结为 `count(AiGroupMessageQuantityFactBinding WHERE binding_state='bound')` 的幂等投影，包含on-time、late与time-unproven。恒等式为 `confirmed_message_count = on_time_confirmed_message_count + late_confirmed_message_count + confirmation_time_unproven_count`、`raw_observed_remote_count = confirmed_message_count + unbound_observed_remote_count`，unbound再由binding state细分 conflict/orphan。

为承载可执行结算，target还必须additive保存 `settled_coverage_target_count/settled_on_time_coverage_count/settled_late_coverage_count/settled_unproven_coverage_count/settled_unknown_coverage_count/settled_known_coverage_shortfall_count/settled_coverage_shortfall_count`及各coverage集合hash；这些与上述settled字段只由SettlementOperation final CAS写一次，普通projector只能更新current late/unproven/post-settlement history列。settlement时已存在的late/unproven与settlement后新到的late/unproven必须分别通过immutable settled列和current history列读取，不能事后从持续增长的fact表重算deadline快照。

任务日deadline settlement以当时已投影的bound facts写一次 immutable `settlement_status=met|missed|closed_with_unknown_shortfall` 及 settled counts/hash；后续任何fact都不能改这些settled字段或把missed改met。`natural_day_target_met`只认settlement snapshot中的on-time quantity与on-time coverage，不认总`confirmed_message_count`；`TaskAccountDailyCoverage`同样additive保存 `confirmation_timeliness/remote_effect_at/on_time_confirmed_at/late_confirmed_at`，late/unproven coverage只修历史且不能补deadline SLA。deadline后首次bound按上述authoritative basis分类：on-time、late或unproven分别进独立列；若投影发生在settled_at后另增post-settlement count；原unit已有terminal/unknown shortfall时减少`unresolved_terminal_shortfall_count`，但`settled_shortfall_count/settlement_status`保持不变。UI/API必须同时展示settled shortfall、late、time-unproven与post-settlement correction，禁止用修正后的总confirmed/coverage伪装按时完成。

结算公式使用route线性化事务的同一database snapshot。对每个target先冻结`SettledRankSet={(target,rank,active_obligation_id nullable) | 1<=rank<=effective_planned_target}`；每个rank映射其唯一active owner，当前没有active owner时identity明确为空，随后由settlement按同rank分配新quantity identity并写`terminal_shortfall(deadline_unmaterialized)`。`settled_target_count=|SettledRankSet|=effective_planned_target`；Task stop只终结并retire当时的安全执行identity，不缩小effective target/SLA：若同任务日未再次start，该空rank在deadline仍被物化为`terminal_shortfall(deadline_unmaterialized_after_stop)`并归known shortfall；旧cancelled identity自身仍不改成shortfall。`Q_on`只能是SettledRankSet中有active identity、binding=`bound`且timeliness=`on_time`的distinct rank；`Q_late`与`Q_unproven`分别是bound且对应timeliness的rank；`Q_unknown_hold`只包含已call-issued但无bound result的unknown/reconcile-only rank，绝不含unproven binding。`protected_overage|retired` identity及rank超出effective target的fact/hold全部排除在Q集合外并单列overage/history hash，不能替代低rank缺口。`Coverage_due`是SettledRankSet中deadline前仍属于权威required scope、未在deadline前合法abandon的distinct coverage keys；`Coverage_on/Coverage_late/Coverage_unproven/Coverage_unknown_hold`分别按同一互斥口径分类。则：

```text
settled_on_time_confirmed_count = min(|Q_on|, settled_target_count)
settled_late_confirmed_count = |Q_late|
settled_time_unproven_count = |Q_unproven|
settled_shortfall_count = max(settled_target_count - settled_on_time_confirmed_count, 0)
settled_coverage_target_count = |Coverage_due|
settled_on_time_coverage_count = |Coverage_on|
settled_late_coverage_count = |Coverage_late|
settled_unproven_coverage_count = |Coverage_unproven|
settled_coverage_shortfall_count = |Coverage_due - Coverage_on|
settled_unknown_quantity_count = |Q_unknown_hold|
settled_unknown_coverage_count = |Coverage_unknown_hold|

target_status = closed_with_unknown_shortfall
  if settled_time_unproven_count + settled_unproven_coverage_count
     + settled_unknown_quantity_count + settled_unknown_coverage_count > 0
  else met
  if settled_shortfall_count = 0 and settled_coverage_shortfall_count = 0
  else missed

ledger_status = closed_with_unknown_shortfall if any target_status is closed_with_unknown_shortfall
  else met if every target_status is met
  else missed
```

late/unproven不进on-time；late造成shortfall，unproven同时进unknown优先级。SettledRankSet中的每个rank必须且只能归入`on_time|late|unproven|unknown_hold|known_shortfall`之一，coverage key亦使用同样互斥分类；`settled_known_shortfall_count=settled_target_count-|on_time|-|late|-|unproven|-|unknown_hold|`，coverage同理。deadline前因effective target下调而retired的高rank已落在`rank > effective target`之外；因stop退休但仍在effective范围内的identity不进入Q集合，其空rank按上一段归known shortfall。active terminal-shortfall同样进入known shortfall。`protected_overage`只增加raw/history/overage计数，绝不进入on-time target或coverage抵扣；`min`只是额外防止计数为负，不能用高rank成功掩盖低rank缺口或unknown。settlement snapshot hash必须包含规范SettledRankSet及nullable active owner映射、protected/retired identity集合、target/coverage key集、effective revision、quantity与coverage五个互斥集合的count+set hash、projection barrier hash和上述全部counts/status，API与E4按同hash回读。

结算由持久 `AiGroupTaskDaySettlementOperation` 唯一拥有，数据库唯一 `task_day_ledger_id`，新ledger在bootstrap同事务创建，存量ledger在takeover apply按下文创建，保存 `enrollment_id/route_id/target_set_hash+version/deadline_at/activation_ready/state=pending|processing|blocked|completed/target_cursor/missing_unit_cursor/projection_barrier_hash`、跨target aggregate的quantity/coverage五类counts+set hashes、protected-overage count/hash、`settlement_snapshot_hash/next_retry_at/lease_owner/lease_epoch/lease_expires_at/version`。每个target另有唯一 `AiGroupTaskDaySettlementTargetItem`，保存 `operation_id/target_operation_target_id/state/missing_due_count/terminalized_count/settled_rank_set count+hash`、quantity与coverage的`on_time/late/unproven/unknown_hold/known_shortfall` count+set hash、protected-overage count+set hash、projection barrier hash、status、settled_at、snapshot_hash、lease与version；这些结果字段只在completed final CAS写一次。`recovery`无论Task为running/paused/stopped/closed都只领取`activation_ready=true AND next_retry_at<=database_now`的operation，按`(next_retry_at,deadline_at,id) WHERE activation_ready=true AND state IN ('pending','processing')` keyset drain；blocked不进入热索引并必须有typed alert/contract blocker。

deadline到达后，唯一业务收口owner是settlement target chunk；time-due subscription/deadline wake只以expected version把同一SettlementOperation置为可领取，绝不直接终结obligation、FOP或handoff。chunk先按§7.5锁 enrollment→route/ledger→SettlementOperation/TargetItem→target，再收口该target已存在的open/waiting/pre-call unit；遇到`check_in_ready`必须继续按obligation→FOP→`AiGroupCheckInHandoff` expected-version顺序把`pending|claimed` handoff转`superseded`、清claim lease，再把同一unit转shortfall，双worker/lease接管只能有一个CAS winner。Planner从未创建的volume due差额不能消失：item以target effective revision/version CAS预留缺少ordinal，分块插入同一due-unit identity的`terminal_shortfall(deadline_unmaterialized)` obligation与已消费deadline guard，不创建Action/GenerationJob；未完成coverage ledger必须已有stable coverage obligation，否则打开`canonical_conservation_breach`而不是伪造完成。每个chunk只推进operation/item cursor，崩溃按唯一ordinal幂等续跑。

最终target/ledger结算前必须取得route行写锁作为Tx C/projector线性化点。Tx C与projector也必须先锁同route：在settlement取得route锁前已提交的canonical fact对当前事务可见；结算查询该ledger所有可见fact的required ProjectionState，只有全部`projected`且pending/retryable-failed/live-or-expired lease=0、quantity binding/target/coverage投影恒等式通过时才可继续。ProjectionState additive保存`failure_class=retryable|poison|nonretryable_contract_error`：pending、retryable failed或任何待回收lease只令settlement回pending+next_retry并触发同一projector drain，不打开contract blocker；只有poison/nonretryable contract error才令settlement blocked并打开enrollment blocker，绝不能写immutable missed/met。route锁后才提交的Tx C等待结算提交，随后只按late/post-settlement历史投影。final事务按target ID C顺序写各item/target settled fields，再写ledger operation completed与read-model version；任一CAS失败整笔重试。automatic rollover必须匹配settlement=`completed`；未完成时只唤醒/创建同一settlement request并让出，不能自行关闭旧ledger或创建新ledger。

settlement 不能只查现有ProjectionState。它必须对每个fact根据其冻结`projection_contract_version + required_projection_kinds`生成规范required set，与实际ProjectionState做anti-join，并同时校验required count/set hash；任一缺行、多出未登记kind或hash不等都为`projection_contract_incomplete`，保持pending/必要时打开nonretryable blocker，绝不得将空集判为all projected。takeover对legacy fact按实际lineage生成`ai_group_fact_projection_v1`映射，幂等补全pending rows后重投影，不伪造projected。

poison恢复不能靠清error或generic retry。新增append-only `AiGroupProjectionPoisonResolution(projection_state_id,resolution_revision,old_error_hash,new_projector_contract_version,deployed_sha,approval_ref,expected_state_version,decision=retry_with_fixed_contract,state=approved|applied,result_hash,created_at)`，唯一 `(projection_state_id,resolution_revision)`，受保护操作使用`system.manage`与独立approval。批准事务只CAS精确poison row为pending、推进projection retry epoch/version并保留旧error审计；修复版projector成功投影且全部恒等式通过时，才按 route→settlement operation/item→contract blocker 顺序把resolution applied、resolve精确`projection_poison` enrollment blocker，并对引用该poison的精确blocked settlement item/operation做expected reason/snapshot/version CAS；已无其他poison时转`pending`、清lease、设`next_retry_at=database_now`并bump read-model，仍有其他poison时保持blocked。再次失败写新error/version，原resolution不得伪装成功。

DDL 必须先新增nullable列/索引，再drop现有全表 `uq_task_group_daily_target`，并用两个互斥partial unique替代：legacy `UNIQUE(tenant_id,task_id,group_id,target_date) WHERE target_operation_target_id IS NULL`；current `UNIQUE(task_day_ledger_id,target_operation_target_id) WHERE task_day_ledger_id IS NOT NULL AND target_operation_target_id IS NOT NULL`。legacy predicate故意包含现存“ledger已填但target operation为空”的历史行，禁止为了满足新check回写它们；新增CHECK只禁止 `target_operation_target_id IS NOT NULL AND task_day_ledger_id IS NULL`。current insert必须两者均非空；`target_date`只作兼容展示，不能作为current identity。同local date允许多个不同TaskDayLedger current row并与一条legacy row共存。takeover绝不原地给legacy row填新列：新建current row，`legacy_target_id`只作additive lineage，所有current计数由typed fact/binding/coverage重投影，旧row逐列hash不变。迁移readback验证每个legacy row仍命中legacy unique、每个current row命中current unique、无非法target-nonnull/ledger-null半行。current route发现ledger或target operation缺失时进入route-scope blocker`contract_migration_blocked`，不得回退到naive target_date。

`target_set_hash` 只允许使用规范serializer `ai_group_target_set_v1`，且只代表不可变target membership/reference与ledger冻结base合同，不代表动态effective数量。payload固定为 `{contract_version, ledger:{logical_task_id,period_start_at,deadline_at,timezone_snapshot,timezone_revision,day_phase,applied_next_ledger_revision}, targets:[...]}`；每个target元素固定包含 `target_operation_target_id,base_target_revision,group_id,target_peer_type,target_peer_id,base_planned_target,pacing_snapshot_hash,target_effective_at`。targets按规范化target operation ID的UTF-8 bytewise/C顺序排列；object key同样按UTF-8 bytewise排序，字符串NFC+UTF-8、整数十进制无前导零、boolean/null使用JSON字面量、timestamp统一UTC RFC3339六位微秒`Z`，禁止float、locale、数据库默认JSON序列化或本地时区。最终值为 `sha256("ai_group_target_set_v1\n" + canonical_json_bytes)` 小写hex，并在route/bootstrap/read-model/manifest/cursor旁同时保存`target_set_hash_version=ai_group_target_set_v1`。动态account/profile/coverage/online/admission、`effective_planned_target_revision/effective_planned_target`变化不改hash，只推进target row version与ledger read-model version使旧cursor失效；current ledger的base target/pacing集合不可PATCH，只有next-ledger revision被bootstrap消费时为新ledger生成新hash。所有API/worker/takeover共用同一serializer与golden vectors，不得各自拼字符串。

`quantity_ordinal` 从 1 开始，只承担planned due-unit的不可变业务身份，不承担当前目标位置。`TaskGroupDailyTarget` 以 `next_quantity_ordinal + effective_planned_target_revision + version` 单行 CAS 分配新identity；并发 Planner 即使来自不同decision也只能取得不同ordinal，相同`due_unit_key`的insert-on-conflict只能返回同一义务。当前数量位置另由`effective_due_rank`承担：对effective target，规范rank空间始终是`{1..effective_planned_target}`，每个rank在同一时刻至多一个`active|protected_overage` owner。

目标增长时按rank升序补齐当前空间：已有active owner直接回读；该rank有带remote boundary的`protected_overage`时先以expected target/obligation version CAS恢复同一owner为active；否则为该rank分配新的更高`quantity_ordinal`并插入义务。目标下调时只对`rank > new effective target`处理：可证明未call-issued且符合scope合同的unit转`cancelled_by_target_revision + due_rank_state=retired + rank_retired_reason=target_reduction`；Gateway-started/unknown/confirmed或其他不可撤销boundary转`protected_overage`并作为overage审计，不进入缩小后的DueSet。后续再增长必须先恢复同rank的protected owner；只有retired/从未建立的rank才创建新identity。因此identity ordinal永不复用，而rank只有在旧owner已不可逆retired后才可由新identity重用，不会出现“目标下调再上调后新ordinal永不到期”。Task stop的safe adoption使用同一rank释放规则：同事务终结Action/FOP并把obligation转`cancelled_by_task_lifecycle + due_rank_state=retired + rank_retired_reason=task_lifecycle_stop`；任何call-issued/unknown/confirmed/protected owner不得退休。start-after-stop不复活旧行；Planner把当前`RankDueNow`内已无active/protected owner的rank归入M，并以更高ordinal创建新active identity。若未start到deadline，settlement仍按§4.1为空rank记known shortfall，stop不是隐式target revision。

每个义务的`due_at`只计算一次：使用其`effective_due_rank`、`TaskDayLedger.period_start_at/planning_anchor_at/deadline_at/timezone_snapshot`、创建时target revision/effective time与冻结`natural_full_day` pacing snapshot，取该rank在曲线中首次到期的数据库时刻，并与`target_effective_at`取较晚者；结果与deadline写入obligation。rank owner从protected恢复active沿用原due_at和remote boundary；新identity即使重用retired rank，也按本次增长effective time计算新的due_at，禁止追溯到增长前。所有due/confirmed读取只认TaskDayLedger边界、active rank owner与typed remote fact，不再使用naive `target_date 00:00`、Action `executed_at`或message-memory status。

现有通用 `FulfillmentObligationProjection` 继续保存 `(obligation_type='ai_group_message', obligation_id)` 的最小执行投影；现有 `Action.uq_actions_open_obligation` 和 `GenerationJob.uq_generation_jobs_open_obligation` 继续提供非终态数据库唯一性。AI 专属 blocker、wake、variation 和 content intent 不塞入通用投影。

FOP 状态必须与 typed obligation 按 §5 精确映射；只有 typed `open` 投影为 FOP `open` 并进入 claim-ready 索引。wake subscription 到期或事件版本增长时，先 CAS typed obligation，再将同一 FOP 恢复为 open；不能把 waiting 留在 FOP open 后依赖 Action 唯一冲突限流。

coverage subtype 是群日数量单位和覆盖义务的同一次候选发送。它进入 `content_capacity_gap` 后仍是既有 due unit，不删除、不改成 extra-volume，也不创建替代数量单位；deadline 前持续显示数量/coverage 欠额，deadline 后只由同一任务日settlement target chunk把同一行转 `terminal_shortfall`。这样不会用其他账号补量掩盖必达账号未完成，也不会在该账号恢复后产生无审计超发。

### 4.2 `AiGroupContentAllocationPlan`：跨技术批次的内容合同所有者

稳定数量义务只回答“需要几条”，不能替代 reply、素材、行为类型与比例合同。新增 aggregate allocation plan，按逻辑 Cycle 冻结内容分配；一个逻辑 Cycle 可跨多个最多 20 条的 Planner 技术批次，技术切批不得重置比例或 minimum。

| 字段 | 合同 |
| --- | --- |
| `task_day_ledger_id/target_operation_target_id/content_cycle_seq` | aggregate plan 唯一业务范围 |
| `content_contract_version/config_revision` | 本 Cycle 的内容政策快照 |
| `scope_total_units/allocation_seed` | 逻辑 Cycle 单位数与确定性分配种子 |
| `reply_min_required/direct/reply_count` | reply minimum 与关系分配守恒 |
| `material_requirement_counts` | image/sticker/custom emoji/normal-text-emoji 等 policy minimum |
| `act_type_counts` | context_reply/short_react/topic_shift/question 等行为分配 |
| `allocated/confirmed/shortfall_count` | 内容构成读模型；不得代替数量 remote fact |
| `state/version` | open/allocated/settled_with_content/settled_with_shortfall + CAS |

`scope_total_units = min(resolved_logical_turn_count_for_current_mode, remaining_planned_target_units)`，`reply_min_required = min(reply_min_per_round, scope_total_units)`。`resolved_logical_turn_count_for_current_mode` 复用现有 mode-aware Cycle 解析结果；不得直接读取 raw `messages_per_round`，因为其 `1` 在现有模式中具有“按参与账号解析”的特殊语义。最多 20 条只是技术物化批次，消费同一 Cycle assignment，不重置比例、minimum 或 Cycle cursor。

`content_cycle_seq` 由 task-day content cursor 单行 CAS 分配，数据库唯一 `(task_day_ledger_id,target_operation_target_id,content_cycle_seq)`。plan 冻结完整 deterministic allocation vector/hash 和 aggregate counts，但 `AiGroupContentRequirementAssignment` 按最多 20 个技术 unit 懒创建/领取；plan 的 `next_plan_unit_ordinal + version` CAS 防止技术批次重复分配。assignment 至少保存 `plan_unit_ordinal/relation_kind/reply_target_scope/reply_source_key+observed_revision/material_requirement/material_dependency_key+observed_revision/act_type/state/reclassification_revision/obligation_id/version`，状态为 `available|reserved|bound|confirmed|content_shortfall|cancelled`。数据库唯一 `(allocation_plan_id,plan_unit_ordinal)`；`reserved|bound` 时 obligation partial unique，且一个 obligation 只允许一个 active assignment。reservation 与 obligation insert 同事务，崩溃恢复按 owner/version 释放；confirmed/Gateway-bound 不释放。reclassification 只允许 current policy compatibility matrix 明确列出的 pre-Gateway 转换，更新 aggregate count 后才能提交；否则进入 content shortfall。

replacement Action、variation 和 extra-volume→coverage 转换复用原 assignment，不重新抽比例。reply target 失效、账号能力或素材不足时，只能按现有显式 reclassification 规则 CAS 变更 assignment revision，或投影 content shortfall；任何 aggregate count 改变均在同一 plan version 下守恒并留审计。该 plan 是 current 内容合同所有者，但不拥有数量成功、Gateway 或 remote fact，因此不是恢复 legacy ContentMix 双账本。

current reclassification matrix 固定为：`relation_kind` 与 `act_type` 不可降级；reply 只能在同 tenant/Task/群、同 own-message scope 内换另一条仍有远端事实的 reply target，仍无候选则订阅冻结的`reply_source` revision并等待/到deadline shortfall，不能转 direct/check-in；required material 不可删除或跨类型替换，只能按冻结 co-load matrix 增加兼容共载，缺资产则订阅`material_asset` dependency key/version等待/shortfall；wake后只重评估同一assignment，不能另建unit或降低requirement。extra-volume 仅在未物化且 coverage 账号满足原 assignment 全部能力时转 coverage，否则另建 coverage unit；账号换绑也必须满足原 assignment。check-in 只完成数量/可选 coverage，原 reply/material/act-type assignment 记 `content_shortfall(check_in_quantity_only)`，不伪装内容构成已完成。所有 reclassification 保存 old/new snapshot hash、reason、policy revision、operator/source 与 plan version。

### 4.3 `AiGroupContentIntent`：不可变单元内容要求

移除 legacy ContentMix 真相源不等于删除内容合同。每个可执行 message obligation 必须有一个 current content intent；唯一例外是§4.1由settlement直接创建、永不可物化的`settlement_shortfall`终态。可执行intent至少冻结：

- `relation_kind` 与 reply/direct 资格；reply 不得转 check-in；
- 当前 Cycle/批次的 `reply_min_per_round` 分配依据；
- `act_type`、话题方向、老师目标和真人化要求；
- 素材类型、policy minimum、允许共载矩阵和规则版本；
- own-message reply scope、初始 reply target/revision 与失效处理；
- prompt、内容安全、面具和 contract version；
- `intent_revision` 与 `intent_snapshot_hash`。

content intent 必须引用唯一 allocation assignment；数据库唯一 `(obligation_id,intent_revision)` 与 `(obligation_id,intent_snapshot_hash)`。可执行obligation保存 `current_content_intent_id/current_intent_revision`，该复合指针以 deferred FK 在事务提交时验证确实指回同一 obligation/revision；禁止用 `max(intent_revision)` 猜 current。`settlement_shortfall`没有content intent/assignment，也不得随后补建或改回可执行状态。

创建/重分类固定为：先以 expected plan/assignment version CAS 合法 reclassification，再以 expected obligation version 分配 `current_intent_revision+1` 并冻结新 intent id/hash，随后插入 immutable intent，全部同一事务提交；snapshot hash 已存在时只能回读同一 intent，revision/pointer 冲突则整笔回滚重评估。可执行obligation创建事务同样必须在提交前完成 assignment、intent 与 current pointer，外部不能看到“可物化但无 intent”的行；只有settlement target chunk可按§4.1 CHECK直接插入无assignment/intent的终态shortfall。一经 Action 绑定不可原地改写；上下文变化只允许按现有合同创建新 intent revision，reply→direct 或素材转派必须先改变 aggregate assignment revision并通过 plan 守恒，再生成新 intent，禁止为解除 blocker 静默降级。Action 与 GenerationJob 结构化冻结 intent id/revision/hash；Gateway Tx A/Tx B 除实时资格外还必须核对 obligation current pointer完全一致，已绑定后要换 intent 必须先证明无 call-issued 并安全终结/释放旧 materialization。

### 4.4 `AiGroupContentVariation`：内容尝试身份

每次生成变化写独立 variation：

| 字段 | 合同 |
| --- | --- |
| `obligation_id/content_intent_id/intent_revision/variation_sequence` | 每义务、current intent 下单调且唯一；数据库 unique `(obligation_id,intent_revision,variation_sequence)` |
| `content_variation_key` | 对所有正常 coverage/extra-volume 必填 |
| `account_id/account_binding_version` | 防止换号后复用旧账号语义 |
| `context_snapshot_version/mask_snapshot_hash` | 生成事实基础 |
| `forbidden_semantic_set_hash` | 已拒绝 duplicate reference/语义集合 |
| `generation_epoch/provider_round` | 主/备用模型各最多 3 轮的现有合同不得被新 Action 重置 |
| `state/rejection_code/duplicate_reference_id` | planned/generating/accepted/rejected/abandoned 及类型化证据 |

`content_variation_key` 由 obligation、intent revision、variation sequence、账号绑定、上下文、面具和禁用语义集合共同生成；不能是随机字符串，也不能仅使用 Action ID。normal body 的 Action/GenerationJob/memory 必须绑定 variation；确定性 check-in 不进入 GenerationJob/normal variation 表，Action/Attempt 改为结构化冻结 `check_in_scope_claim_id + check_in_memory_id + reservation_version + intent_id/revision`，Gateway guard逐项核对，禁止填 dummy variation key。`reservation_version` 归属唯一 current check-in memory/claim reservation，不是 Action 自增字段。

`normal_generation_exhausted`必须经过持久handoff，不能让Generation越权创建签到Action，也不能让Planner重新调用Provider。新增`AiGroupCheckInHandoff(obligation_id,generation_epoch,trigger_reason,generation_job_id,generation_job_version,intent_id,intent_revision,six_round_evidence_hash,state=pending|claimed|consumed|superseded,claim_lease_*,version,created_at)`，数据库唯一`(obligation_id,generation_epoch,trigger_reason)`。Generation在同一事务必须同时证明主/备用各3轮预算已终结、义务subtype=`extra_volume`、该账号coverage已由bound fact完成、relation=`direct`、active/usable面具仍匹配intent、scoped check-in当前可尝试且`database_now < deadline_at`，才可终结current GenerationJob claim，insert-or-read同一handoff，把obligation/FOP转`check_in_ready`并推进read-model；任一条件不满足均不得handoff，未完成coverage的normal六轮失败只能进入typed content gap。Planner只从`check_in_ready` claim索引领取，复核全部五项资格与handoff/intent/epoch/deadline后CAS scoped claim+memory，创建ready签到Action并把handoff consumed、义务转`action_bound`；它不得再创建GenerationJob或调用Provider。scoped claim被其他owner占用时按§6进入confirmed projection、hold或`content_capacity_gap`。`mask_missing`仍仅由未完成coverage义务走Planner直接分支，不伪造六轮handoff。

每个 generation epoch 绑定不可变 `generation_epoch_basis_hash`：

```text
hash(
  context_revision,
  external_message_memory_revision,
  active_profile_lineage_and_version,
  prompt_and_content_policy_revision,
  account_binding_version,
  allocation_assignment_revision,
  reply_source_revision,
  generation_contract_or_model_revision
)
```

只有上述外部 basis 中至少一个版本改变才允许 `generation_epoch + 1` 并重新取得主/备用各 3 轮预算；单纯换 Action ID、worker、provider 健康恢复、lease 过期或重读相同 facts 都不能重置。一次 duplicate 拒绝把参照加入本 epoch 的禁用集合并消耗一个 provider round；禁用集合/角度变化只产生同 epoch 的下一 variation，不新开 3+3。当前 epoch 轮次耗尽后进入 waiting，直到外部 basis 真正变化。

### 4.5 durable wake clock 与 subscription

waiting 状态不得留在 FOP 的 `state=open` 热索引，也不得只写 blocked 后永不恢复。新增 `AiGroupWakeClock(wake_key,current_version,drained_version,row_version,subscription_fence,dirty,dirty_seq,lease_owner,lease_epoch,lease_expires_at)`；`dirty_seq` 来自独立数据库 sequence，只用于 durable keyset 排队，不作为业务事实版本。所有可唤醒业务事实在与自身状态变更同一数据库事务中以 expected row_version CAS `current_version+1,row_version+1,dirty=true,dirty_seq=nextval(...)`；Redis/pubsub 只能作加速，不能成为唯一事件源。每个等待义务持久化一至多个 event/time wake subscription；此外每个义务无论是否 waiting，都必须有独立 deadline subscription：

| wake type | scope/version 来源 | 唤醒条件 |
| --- | --- | --- |
| `group_context` | tenant+群共享 context watermark | 新真人上下文版本大于 observed version；Task prompt/filter另走policy revision |
| `voice_profile` | tenant+account active mask lineage/version | active/usable 面具版本增加 |
| `account_eligibility` | AccountEligibilityEvent/准入/Session/在线事实 | 对应事实版本改变且重新可发 |
| `task_day_extra_capacity` | Task-day+target aggregate capacity clock | 任一候选的 coverage/profile/online/admission/scope 资格改变 |
| `material_asset` | tenant+material rule/capability/asset/cache revision | required material 新增、启停、版本或 cache 变为 ready |
| `reply_source` | tenant+Task+群 own-message source revision | 可引用 remote fact 绑定、引用删除或失效版本改变 |
| `provider_dependency` | active Provider config/health epoch | key、模型或健康 epoch 改变 |
| `dedupe_expiry` | message memory `dedupe_expires_at` | 数据库时间到达精确 expiry |
| `policy_revision` | prompt/content/admission policy revision | 新 revision 大于 observed version |
| `deadline` | obligation deadline | 只幂等激活同一任务日的 SettlementOperation；不得在 subscription/wake 入口终结 unit |

obligation 创建事务强制插入 `wake_type=deadline,wake_at=deadline_at,is_terminal_guard=true,state=pending`，数据库唯一 `(obligation_id,wake_type) WHERE wake_type='deadline'`；义务与deadline schedule任一insert失败则整笔回滚。普通event wake成功只能supersede同 blocker revision 的非deadline订阅，永不supersede deadline guard。义务在deadline前因confirmed/cancelled等业务终态关闭时可同事务消费guard；否则time-due consumer只按expected subscription/SettlementOperation version把同一任务日SettlementOperation置为`pending,next_retry_at=database_now`并消费本guard，然后返回typed `deadline_elapsed_settlement_activated`。它不得改obligation/FOP/handoff/Action/Attempt/Gateway hold，也不得直接写`terminal_shortfall`或`remote_reconcile_only`；这些状态只由§4.1 settlement target chunk在同一immutable snapshot内收口。deadline consumer使用独立time-due索引/lease，crash后重复激活同一operation是幂等no-op。

订阅协议必须关闭 event-before-subscribe 竞态：

1. 评估第一阻断边界并读取 durable clock 的业务版本 `V` 与行版本 `R`；wake_key 首次出现时先 insert-on-conflict 后重读，不能用 worker 内存默认 0；
2. 同一订阅事务按规范层级先以expected `obligation.state/version/blocker_basis_hash`把obligation CAS为对应waiting并持有行锁，再把FOP CAS为waiting；随后才执行 `UPDATE WakeClock SET subscription_fence=subscription_fence+1,row_version=row_version+1 WHERE wake_key=? AND current_version=V AND row_version=R RETURNING row_version`并插 subscription(`observed_version=V`)。任一CAS/insert失败整笔回滚到原obligation/FOP状态并重评估；成功后WakeClock行写锁保持到事务提交。多clock依赖在obligation/FOP之后按wake_key C序取得。`subscription_fence` 只关闭可见性窗口，不递增业务 `current_version`、不制造假 wake；
3. 提交后事件型 wake worker 从 clock partial index `(dirty,dirty_seq,wake_key) WHERE dirty=true` 取 clock，时间型 worker 独立从 subscription due index读取 `wake_at <= database_now`；两者都能立即处理已经先发生的事件；
4. 消费时用 `(obligation_id,state,blocker_basis_hash,version, database_now < deadline_at)` CAS waiting→open并标记普通subscription consumed；若数据库时间已到deadline，本入口不得改obligation/FOP/handoff，只以expected version激活同一SettlementOperation并返回typed deadline elapsed，业务终结留给settlement target chunk；乱序、重复事件只成功一次；
5. 唤醒后重新评估全部门禁，若命中下一 blocker，再按新的 wake key/version 订阅。

未绑定账号的 extra-volume 统一订阅 `task_day_extra_capacity:{task_day_ledger_id}:{target_id}`。但共享 profile/online/Session/material 源事务不得同步更新“所有受影响 Task-day aggregate”；单账号或素材可属于多个 Task，直接 fan-out 会形成无界写放大与多 clock 锁环。共享源事务只递增自身单 scope clock，并同事务 append 一条 `AiGroupDependencyFanoutEvent(source_kind,source_scope_key,source_version,source_payload_hash,dirty_seq,state,scan_upper_bound,cursor,lease_*,version)`，数据库唯一 `(source_kind,source_scope_key,source_version)`；Task-scoped admission/coverage 等本来只影响一个 target 的事务也可走同一协议。时间类 subscription 只在精确 `wake_at` 到期时进入 due 索引，不按固定 120 秒轮询。没有新 version/time 时 Planner 看不到该义务，Action 和 GenerationJob 增量都必须为 0。

`recovery` 是 fan-out projector 唯一 owner：按 `(state,dirty_seq,id) WHERE state IN ('pending','draining')` keyset+lease CAS 领取 event，以权威 active account/target scope、material requirement 或 reply-source binding 关系做有界 keyset 扫描，并为每个受影响目标 insert `AiGroupDependencyFanoutItem(event_id,task_day_ledger_id,target_operation_target_id,source_membership_revision,state,version)`，数据库唯一 `(event_id,task_day_ledger_id,target_operation_target_id)`。item `pending -> applied` CAS与目标aggregate WakeClock `current_version+1/dirty_seq`必须在同一事务、按wake_key C顺序提交；clock CAS失败整笔回滚，item仍pending，绝不先标applied再丢wake。重复event/item只回读，不二次bump。event必须扫完冻结upper bound且所有item applied/retired后才complete；worker崩溃按event cursor/item state重放。扫描期间新加入的target/account/material binding由其membership/config事务自己append `target_scope_changed` fan-out event，故不会依赖旧event重新扩集合；Task/ledger已终结的item转retired，不唤醒已终态义务。

该两层协议关闭 source-event-before-aggregate-subscribe：源事实先提交时，未完成 event 最终推进 aggregate；fan-out 先完成时，订阅事务读到新 aggregate version 并按最新源事实评估；订阅与 item apply 仍使用 §4.5 clock row fence。event completion/readback 必须满足 `discovered=applied+retired` 和 cursor upper bound，heartbeat 发布 pending/draining event、pending item、oldest lag、last complete 与每 source kind 计数。禁止 listener/profile/material/online 事务查询全部 Task 或直接锁多条 task-day clock。

所有 generation basis 与 wake version 都来自数据库单调 revision，不使用 `updated_at`、`max(id)` 或 worker 内存猜测。`AiProvider/TenantAiSetting` additive 保存 config revision 与 health epoch；message-memory 维护 account scope revision。当前义务自身 reservation/rejection 引起的 memory revision 在同一事务更新其 observed revision，只改变 forbidden set/variation sequence，不得借此开启新 generation epoch；只有随后独立提交的外部 memory 集合变化才是新 basis。

生产者/消费者 ownership 冻结如下；表中 clock 增量与源事实写入必须同事务，遗漏任一 writer 即 Release Gate 失败：

| 事实 | 事务 owner/当前入口 | clock key/revision | consumer |
| --- | --- | --- | --- |
| 群上下文 | `group_listener_context_writer.py` 写 tenant+group context snapshot/watermark | `group_context:{tenant}:{group}` 单 clock；不按 Task fan-out | `recovery` wake drain |
| active/usable 面具 | `account_voice_profiles.py` / profile activation 提交 lineage+version+唯一 fan-out event | `voice_profile:{tenant}:{account}`；aggregate 由 fan-out projector 推进 | `recovery` fan-out + wake drain |
| 账号在线、Session、Task-scoped admission | `account_scope.py`、`task_group_bot_admission_*.py` 及账号在线事实写事务；共享事实 append 唯一 fan-out event | shared `account_eligibility:{tenant}:{account}` 或 scoped admission key；aggregate 由 fan-out projector推进 | `recovery` fan-out + wake drain |
| coverage/remote success/check-in claim | typed fact projector 与 scoped claim transition，append/回读对应 fan-out event | coverage/account/reply-source key；target aggregate 由同 target 直写或 fan-out item 唯一推进 | 同事务投影后 fan-out/wake drain |
| Provider 配置、密钥、模型、健康 | `api/routers/ai_config.py`、`ai_generation_runtime_config.py`、健康检查与 quota-exhausted 写事务 | `provider_dependency:{tenant}:{provider}` config revision/health epoch | `ai-generation` reconcile + `recovery` wake |
| Task prompt/content/admission policy | `service.py` typed settings revision transition | `policy_revision:{task}:{task_day}` | `recovery` wake drain |
| 正文 memory reservation/terminal/expiry | `ai_message_memory*.py` | `message_memory_scope:{tenant}:{account}` + exact expiry | `recovery` wake drain |
| 素材资产/能力/cache | 素材创建/启停/版本、`material_cache` ready/failed→ready 源事务递增 tenant+asset/rule revision并 append 唯一 fan-out event | `material_asset:{tenant}:{material_scope}`；target aggregate由fan-out推进 | `recovery` fan-out + wake drain |
| 可引用 own-message | bound remote fact projector、引用删除/失效事务 | `reply_source:{tenant}:{task}:{group}` | `recovery` wake drain |
| deadline/dedupe expiry | 数据库 `wake_at`，无外部 producer | `(wake_at,id)` due index | `recovery` wake drain |

所有会提交上述源事实的进程必须发布独立 capability=`ai_message_dependency_producer_v1`；只部署consumer或统一route guard不足以通过Gate。实现时以入口测试生成并冻结下列“writer入口→runtime role/container”矩阵，任何新增writer未登记即契约测试失败：

| writer入口/事实族 | 必须带producer capability的runtime |
| --- | --- |
| `group_listener_context_writer.py`、listener context/cursor提交 | `worker-listener` |
| voice profile generation、activate/rollback/rebuild及lineage提交 | `worker-voice-profile`；对应写API所在`backend` |
| account online/Session/security/authorization状态提交 | `worker-account-online`、`worker-account-security`；对应写API所在`backend` |
| message memory reservation/release/terminal/expiry | `worker-planner`、全部`worker-ai-generation-*`、全部`worker-dispatcher-*`、`worker-recovery`、`worker-ai-memory` |
| material/source-media创建、启停、版本与cache状态 | `backend`、`worker-listener`、独立`worker-material-cache`；`worker-account-security`只在最新代码入口审计证明仍写material source/cache事实时才纳入该writer bitset，不得以旧拓扑猜测 |
| Task policy、admission、coverage/claim、bound fact/reply-source | `backend`、`worker-planner`、全部`worker-dispatcher-*`、`worker-recovery` |
| Provider config/model/key/health/quota状态 | `backend`、全部`worker-ai-generation-*`、`worker-recovery` |

Release workflow从实际compose manifest和writer inventory计算期望实例集合，逐实例核对image SHA、role、`ai_message_dependency_producer_v1`与writer-family bitset。schema发布后、inventory bootstrap前必须停止/替换所有旧producer，等待旧instance heartbeat/lease过期并证明进程清单中旧SHA为0，再让新producer提交一个受控版本并由consumer readback clock/outbox；此时仍无enrollment/route，不会发送业务消息。任一入口、实例、capability或受控readback缺失时fleet policy保持不存在或building，禁止preview/activation。API写路径与worker路径同等纳入，不能用worker健康代替。

共享依赖的 `AiGroupDependencyFanoutEvent/Item` 必须先由 recovery 按各自 partial index 收敛到 target aggregate clock；下述 clock drain 只消费 direct/shared/aggregate clock，不在 drain 中临时查询并 fan-out 全部 Task。

`recovery` 的事件 drain 固定为：按 `(dirty_seq,wake_key)` 有界 keyset 取 dirty clock；以 expected row_version 逐行 CAS lease、`row_version+1` 并冻结 `claimed_version=current_version`；再用 subscription index `(wake_key,state,observed_version,obligation_id)` 分页扫完 `state=pending AND observed_version < claimed_version`，逐行 CAS 唤醒。扫完后以 expected row_version CAS `drained_version=claimed_version,row_version+1` 并释放 lease；仅当 `current_version=claimed_version` 时清 `dirty=false`，期间 producer 又递增时保留 producer 写入的新 dirty_seq，下一轮继续。订阅 fence、producer 与 clear-dirty都更新同一 row_version：订阅 CAS 先成功则 producer/consumer 等其提交后再推进；事件先成功则订阅 CAS 失败重评估；drain 先持有 row version 时订阅在清 dirty提交后重读，因而不存在“drain 看不到未提交旧版本订阅却先清 dirty”的窗口。consumer 崩溃只回收过期 clock/subscription lease，同一 `claimed_version` 可幂等续扫。时间型 drain 独立按 `(state,wake_at,id) WHERE state=pending` 有界 keyset 领取 subscription。两类 drain 均逐行 `UPDATE ... WHERE state/version/lease 条件 RETURNING`，不使用 `FOR UPDATE/SKIP LOCKED`、OFFSET、全表 subscription join 或跨表显式锁链。

同一业务事务触达多个 wake_key 时，producer、subscriber fence 和任何 multi-clock recovery 必须统一调用一个 helper：先去重，再按规范化 wake_key UTF-8 bytewise/C collation升序逐行 CAS；禁止沿调用方、账号或 subscription输入顺序更新。单 key协议不变。这样同时更新 specific+aggregate clock的事实事务与订阅多个依赖的义务不会形成 A→B/B→A 行锁环。

消费、typed/FOP CAS、旧订阅 terminal 与新订阅创建同事务。一个义务多订阅时，首次成功唤醒只将同一 blocker revision 的其余非deadline订阅置 `superseded`；deadline guard仅能由业务terminal或deadline drain消费。晚到事件幂等no-op。heartbeat分别发布dirty clock backlog/oldest dirty age、time-due backlog（区分deadline/普通timer）/oldest due age、lease recovery和last successful drain。

deadline 是所有新外部工作的硬 CAS 条件，不只是一个异步 worker：任何 waiting/reconcile/safely-not-executed/lifecycle-adopt → open、Planner materialize、Generation 新 provider request、Gateway Tx A prepare 与 Tx B call-issued 都必须在各自同一 `UPDATE ... WHERE` 使用数据库时间验证 `database_now < obligation.deadline_at`。不满足时，本入口只拒绝新外部工作、幂等激活同一SettlementOperation并返回typed `deadline_elapsed_settlement_activated`；不得改obligation/FOP/handoff、释放或终结pre-Gateway owner，也不得直接写shortfall/reconcile-only。只有 deadline 前已提交 Tx B 的精确 `invoke_committed` 可由原 owner跨 deadline完成一次，既有 call-issued/started/unknown继续 reconcile；迟到 typed fact仍可投影。所有deadline业务状态终结、owner释放与immutable snapshot只由§4.1 settlement target chunk完成。

### 4.6 `AiGroupMessageMemory` 补齐

在现有表 additive 增加：

```text
task_day_ledger_id
coverage_ledger_id
obligation_id
content_variation_id
check_in_trigger_reason
gateway_started_at
dedupe_expires_at
duplicate_reason
fact_anchor_key
reservation_version
reservation_owner_action_id
contract_version
```

正常正文继续按 `tenant_id + account_id` 执行滚动 10 天 exact/semantic/template 去重，并显式要求：

```text
content_source <> 'check_in'
dedupe_expires_at > database_now
status 属于 reservation / in-flight / unknown / success
```

切换期对旧正常正文只读兼容 `dedupe_expires_at IS NULL AND planned_at >= database_now-10d`，不得回写旧行；已由 check-in scope claim/legacy alias 识别的 `mask_missing_check_in/check_in_fallback/due_catch_up_check_in` 明确排除普通正文查询。这样既不让旧签到跨 Task/群阻断，也不提前放开旧正常正文质量窗口。

check-in 只使用统一 `content_source=check_in`，正文精确为 `签到`，并建立 partial unique：

```text
UNIQUE(task_id, group_id, account_id, task_day_ledger_id)
WHERE content_source = 'check_in'
  AND status IN (
    'reserved','pending','claiming','executing',
    'gateway_started','unknown_after_send','success'
  )
```

语义状态分别对应 open/Gateway/unknown/confirmed。check-in reservation key 使用 scoped business key，不再拼接 Action ID，并继续受现有永久 `reservation_key` unique 约束，因此 current scope 只创建一条持久 memory。明确 pre-Gateway 且 transport 未开始的失败把该行 CAS 为 `released`、清 current owner但保留 reservation key/历史；以后同一义务被合法 wake 时，必须在同一事务把 claim `available -> reserved` 与原 memory `released -> reserved,reservation_version+1,reservation_owner_action_id=new_action`，不得 insert 第二行。旧 Action 只保留 Attempt 审计。Gateway started、unknown、success 的 claim/memory 永不进入 released，也不能再次 reserve。

### 4.7 `AiGroupCheckInScopeClaim`：新旧合同统一资格占位

新增 scoped qualification claim，数据库唯一 `(tenant_id,task_id,group_id,account_id,task_day_ledger_id)`，保存 `state=available|reserved|gateway_started|unknown|confirmed`、owner kind/id、`memory_id/reservation_version`、remote mutation identity、trigger reason 和 version。它不是第二个成功事实表：只有 typed remote fact 能确认群日数量/coverage；claim 只统一新 memory partial unique 与不能改写的 legacy owner。

新 check-in 首次 reservation 先 CAS claim `available -> reserved` 再创建唯一 current memory；后续只按上段复用并推进同一 memory reservation version。pre-transport failure 用同 claim/memory owner+version 原子释放为 available/released，Gateway/unknown/confirmed 不释放。迁移遇到旧 check-in 时只新增 claim、current memory reservation owner（需要继续执行时）和 legacy link，不改旧 memory 的 `content_source/status/result/reservation_key`；legacy owner 已在 Gateway/unknown/confirmed 时 claim 直接指 legacy alias，不新建可 reserve memory。scope claim、permanent reservation key 与 active partial unique 共同防止 legacy/current owner 竞争。

### 4.8 `AiGroupMessageContractFleetPolicy/Enrollment/Route`：fleet、任务与任务日 writer fence

新增 tenant-level `AiGroupMessageContractFleetPolicy`，数据库唯一 `tenant_id`，保存 `new_task_contract_version=ai_message_obligation_v1`、`inventory_status=building|sealed`、`inventory_cutoff_at`、`legacy_state=allowed|preparing|disabled`、`legacy_inventory_hash`、`policy_epoch/version/activated_at`。它只控制 AI message legacy→current rollout，不能复用或改写 `DispatchClaimScope.active_contract_version`；后者继续只表示现有 Dispatcher runtime/topology contract。

新增 immutable-membership `AiGroupMessageLegacyInventoryItem`，数据库唯一 `(policy_id,task_id)`，保存不可变逻辑 `task_id`、nullable `task_record_id`（仅作导航 FK，Task 物理删除时 `ON DELETE SET NULL`）、immutable `frozen_task_status/frozen_task_contract_version/frozen_task_lifecycle_epoch/frozen_config_epoch/frozen_task_route_epoch`，以及受控可变的 `allowed_task_status/allowed_task_lifecycle_epoch/allowed_config_epoch/allowed_task_route_epoch`、`state=open|enrolled|retired`、`enrollment_id/takeover_class/retired_reason`、`version/frozen_at/transitioned_at`。`legacy_inventory_hash` 由按逻辑 task_id 排序的 item immutable membership/frozen identity 列计算，allowed status/epoch/state、`task_record_id` 置空均不改 membership hash；每次合法legacy lifecycle transition在同一事务CAS allowed status/epochs，每次fleet transition另归档按 task_id 排序的 state/allowed-status/epoch hash与count。cutover只能匹配当前Task状态与allowed status，不得按inventory初始状态猜测。

兼容基线全部 role 就绪后，以 policy=`inventory_status=building` CAS 开始冻结：building 期间 legacy writer fail-closed，新 Task 仍按 current 合同创建 active enrollment；builder 按固定 `inventory_cutoff_at` 用 keyset 重复扫描 cutoff 前存量 Task，为无 enrollment 的合法 legacy Task幂等插 item，直到 Task 创建/生命周期在途收口且 readback 证明 cutoff 范围零漏项，再以 item membership hash CAS `building -> sealed, legacy_state=allowed`。policy 尚不存在只允许发生在第一条 enrollment/route 之前的 compatibility bootstrap 窗口；该窗口内旧任务按既有 route 运行，但 group AI Task create/start 显式返回 `503 ai_message_inventory_bootstrap_in_progress`，避免产生无归属新任务。一旦 policy building 或任一 enrollment 存在，永不把“缺 policy/item”解释为 legacy 许可。

新增 task-level `AiGroupMessageContractEnrollment`，以不可变逻辑 `task_id` 数据库唯一，另存 nullable `task_record_id ON DELETE SET NULL`，保存 `contract_version/enrollment_epoch/state=preparing|active|blocked|retired/takeover_class/source_task_status/first_task_day_ledger_id/activation_manifest_hash/open_contract_blocker_count/contract_blocker_revision/version`；其中first ledger ID是审计用逻辑标识，不持有会阻断ledger删除的级联/RESTRICT FK。首次 cutover 创建 enrollment=`preparing|active|retired` 的任一受保护分类结果即永久禁止该 Task 所有当前/未来 ledger 的 legacy AI writer；activation、回滚与 Task 物理删除均不得删除 enrollment，`retired` 也永久保持 legacy writer=false。

新增 append-only `AiGroupMessageContractTombstone`，数据库唯一 `(task_id,enrollment_epoch)`，不持有到 Task 的级联 FK，保存 policy/item/enrollment identity、最后 route/ledger/target-set/activation-manifest/source-fence hash、raw/bound/unbound/hold/projector 最终计数与identity-set hash、delete operation ID、snapshot hash 与 committed_at。既有 `RemoteMutationTombstone` additive 保存 `original_obligation_type/id/task_day_ledger_id/target_operation_target_id/quantity_ordinal/enrollment_epoch/route_epoch/latest_evidence_hash/quantity_binding_state/reconcile_state`；delete archive把每个call-issued/unknown/open reconcile/pending projection及其RemoteReconcileCase规范化到该逻辑request identity，并以count/hash readback，不要求unknown先变成已知结果。它们只保留fleet closure、远端防重和守恒/只读reconcile所需最小事实，不保存可恢复配置或正文。

FleetPolicy、LegacyInventoryItem、Enrollment、ContractTombstone、RemoteMutationTombstone以及既有全局activation manifest均不随Task删除。live Task内的canonical fact仍append-only；物理删除阶段只有在fact/binding/projection identity已逐项进入remote/contract tombstone hash后，才可按全局retention合同删除其Task runtime副本。Task删除后的late fact仍按request identity append canonical fact，由`projection_kind=deleted_task_remote_tombstone`只CAS RemoteMutationTombstone reconcile state并追加历史delta审计；不得重建Task/ledger/target/obligation、不得改删除时confirmed/coverage。详细route/source-event/Action/Attempt/RemoteReconcileCase等runtime行仅在双tombstone readback相等后删除。

每个 TaskDayLedger 另有唯一路由行，保存 `enrollment_id/contract_version/route_epoch/task_lifecycle_epoch/target_set_hash/target_set_hash_version/migration_status=preparing|active|blocked/writer_state=running|paused|stopped|closed|incompatible/open_blocker_count/blocker_revision/takeover_manifest_hash/activated_at/version`，数据库唯一 `task_day_ledger_id`。`closed`只表示任务日自然结束，旧request/fact仍可reconcile，不能用于Task stop。activation unit 是整个 Task 任务日：manifest 必须覆盖该 ledger 下完整 target-operation set，禁止同一 Task 日一部分目标走 legacy、一部分目标走 current。该行是内部执行合同，不进入运营 type_config，也不能由页面任意编辑；迁移失败或版本不兼容写 migration blocker，公共 `Task.status` 仍使用现有 `paused`，不新增假枚举。

contract error 的持久真相源统一为 `AiGroupMessageContractBlocker`，保存 `enrollment_id/scope=enrollment|route/route_id nullable/blocker_kind/blocker_identity/state=open|resolved/first_occurrence_identity/source_kind/source_identity/source_revision/snapshot_hash/opened_by_kind/opened_by_id/current_resolution_id nullable/opened_at/resolved_at/version`。数据库只对当前open blocker使用两个partial unique约束：`UNIQUE(enrollment_id,blocker_kind,blocker_identity) WHERE scope='enrollment' AND state='open'`；`UNIQUE(route_id,blocker_kind,blocker_identity) WHERE scope='route' AND state='open'`，并CHECK route scope必须有route、enrollment scope的route只作origin审计。另建append-only `AiGroupMessageContractBlockerOccurrence`，保存owner/scope/kind/stable blocker identity、`occurrence_identity=sha256(source_kind,source_identity,source_revision,snapshot_hash)`、source fields、snapshot、linked blocker ID与observed_at；enrollment/route scope分别以owner+kind+occurrence identity永久唯一。精确旧occurrence重放只回读原linked blocker，即使它已resolved也不得重新打开；新的source revision/snapshot形成新occurrence，当前无同stable identity open blocker时创建新blocker并递增owner count，已有open blocker时只把occurrence链接到它而不重复计数。resolved blocker与occurrence均保留不可变审计，不能把旧行原地改回open。

blocker registry固定scope与resolution：`quantity_fact_conflict|projection_poison|remote_identity_conflict|canonical_conservation_breach|blocker_snapshot_conflict`一律为enrollment scope，跨任务日持续；`takeover_manifest_mismatch|target_mapping_conflict|route_static_version_mismatch|contract_migration_blocked`只为route scope且只能在preparing/blocked迁移中解决，不能随route active遗留。resolution channel同样由registry固定：quantity只先走quantity adjudication再contract_reopen，projection poison只走poison resolution成功投影后自动resolve，remote identity/conservation/snapshot conflict只先走generic blocker resolution再contract_reopen，route migration kind只能重做同一takeover manifest/readback。未登记kind拒绝打开，不能由调用者任选scope或channel。

所有open/resolve事务必须先按§7.5以expected version no-op CAS/锁定enrollment owner，route scope再按规范顺序锁route owner；持有owner锁后才能insert-or-read occurrence、current blocker或resolution，最后在同一已持有owner锁的事务CAS count/revision，禁止blocker→owner反锁。只有新open blocker winner才执行`open_contract_blocker_count+1,contract_blocker_revision+1`，route scope同理；旧occurrence重放、或新occurrence并入已有open blocker均不重复加count。同一个source identity/revision出现不同snapshot的原操作必须失败，另以 `blocker_snapshot_conflict` 打开enrollment blocker；其stable blocker identity与occurrence identity都由原kind/identity/revision及排序后的两个snapshot规范hash导出，重放只命中同一occurrence，不反复加count也不递归打开自己。任何resolve必须绑定append-only adjudication/resolution identity，并同事务把对应owner count减1、revision+1；后续同stable identity但新source revision再次发生时必须创建新的open blocker行并重新加count。恒等式 `enrollment.open_contract_blocker_count=count(open enrollment blockers)` 与 `route.open_blocker_count=count(open route blockers)` 都要readback，不能靠缓存、日志或runtime reason猜测。`migration_status=blocked`只描述未激活迁移；active route运行期错误由enrollment blocker立即fence当前和后续ledger。

通用恢复对象为 append-only `AiGroupMessageContractBlockerResolution(blocker_id,resolution_revision,decision,evidence_hash,new_contract_version,deployed_sha,operator,approval_ref,expected_blocker_version,expected_owner_revision,expected_source_read_model_hash,state=approved|applied,result_hash,created_at)`，唯一 `(blocker_id,resolution_revision)`。`decision`只允许registry按kind白名单的 `verified_canonical_replay|fixed_contract_reprojection|acknowledged_irreversible_retirement`；使用`system.manage`、独立approval与受保护API/CLI，不得改写fact、binding、ordinal或history。resolution只在新合同重投影/守恒readback成功后转applied；contract_reopen只消费applied resolution。没有registry channel、完整证据或无法修复的blocker保持open，只能走审批退役/归档，不能强制清零。

新增 task-level `AiGroupMessageLifecycleAdoption`，数据库唯一 `(task_id,to_lifecycle_epoch)`，保存 `enrollment_id/task_day_ledger_id/route_id nullable/expected_route_missing/from_epoch/to_epoch/command=pause|stop|imported_baseline/state=pending|draining|blocked|ready|complete/discovery_cursor/discovery_complete/adoption_seq/adopted_count/deferred_count/blocked_count/manifest_hash/lease_*/version`。它是 lifecycle sweep 唯一 owner，不依赖 route 必然存在；route 存在时保存其 expected id/epoch/version，缺失时冻结 ledger+enrollment+manifest lineage与 expected-missing evidence。另建 `AiGroupMessageLifecycleAdoptionItem`，保存 `adoption_id/obligation_id/trigger_kind=initial_scan/trigger_identity/item_seq/state=pending|processing|applied|deferred|blocked/latest_safe_evidence_id nullable/lease_*/version`，数据库唯一 `(adoption_id,trigger_kind,trigger_identity)`；`item_seq`来自DB sequence，领取索引为`(state,item_seq,id) WHERE state IN ('pending','processing')`。安全证据不创建第二个计数item：append-only `AiGroupMessageLifecycleSafeEvidence(deferred_item_id,evidence_identity,request_identity,evidence_hash,state=observed|accepted,result_hash,created_at)`以`(deferred_item_id,evidence_identity)`永久唯一、每个deferred item至多一个accepted evidence。`ready`必须同时满足initial discovery完成、pending item=0、`blocked_count=0`，可仅余old-epoch call-issued/Generation deferred reconcile；`complete`表示连deferred也归零。任一item转blocked必须同事务把owner转blocked、打开`canonical_conservation_breach` enrollment blocker并告警，不进ready或resume；只有generic blocker resolution成功修复精确owner/item、以expected versions将item重排pending且blocker解除后，owner才能回draining。`imported_baseline`只可由takeover final CAS创建为complete，from=to=冻结源epoch且manifest hash非空，证明所有legacy pre-call owner已在manifest中转waiting/cancelled/closed、call-issued仅reconcile；它不推进Task epoch，也不能由普通API伪造。

现有 `tasks` 表没有通用row version，本次 additive DDL必须新增 `Task.version BIGINT NOT NULL DEFAULT 1`：历史行在migration内以1建列/回读，之后所有Task status、display、lifecycle epoch、config pointer与delete-fence写都必须使用expected `Task.version` CAS并在winner事务`version+1`；不得把现有 `config_revision` 当通用row version。展示字段只推进Task.version；status/lifecycle同时推进Task.version和其专属epoch/revision。任何遗留直接赋值入口未改为shared transition即Release Gate失败。

新增 task-level `AiGroupMessageTaskRevision`，数据库唯一 `task_id`，保存 `next_ledger_revision/content_plan_revision/generation_policy_revision/account_scope_revision/restart_revision`、各域 current snapshot hash/pointer 与 row `version`；对应 append-only `AiGroupMessageTaskRevisionHistory` 唯一 `(task_id,domain,domain_revision)` 保存规范化 snapshot hash、source request/AuditLog和effective scope。展示字段只推进新增的 `Task.version`，不推进这些业务域。混合PATCH在一个事务以expected Task/config/revision-row version CAS，只给实际变化的域各+1并插history；next-ledger revision只由后续ledger bootstrap冻结为`applied_next_ledger_revision`，不唤醒current义务；content-plan只影响下一allocation plan；generation-policy推进current policy wake并只允许未绑定unit创建新intent；account-scope通过scoped事实/fan-out推进资格；start-after-stop由TaskStartOperation推进restart revision。manifest A static vector、ledger/plan/intent/Action分别冻结实际消费的域revision，禁止用一个`Task.config_revision`让name/timezone变更错误重置全部3+3或内容Cycle。

首次任务日完成 readback 时，enrollment 与 route 在同一最终事务 CAS `preparing -> active`。后续 TaskDayLedger 创建必须在同一事务读取 active enrollment、冻结新 target-set hash，并直接创建 current route=`active`；ledger 已提交而 route 缺失、enrollment/route version 不匹配或 route insert 失败时，Planner/Generation/Dispatcher 全部 contract-blocked，recovery 只允许补同一 ledger 的 route，绝不回到 legacy。pause 跨 deadline 不创建新 ledger；resume 建新 ledger 时同样继承 enrollment。

Alembic additive migration 只创建 fleet policy/item schema、约束和索引，不创建任何 tenant policy/item 或回填生产 Task。生产数据 ownership 只属于 Release Gate 中受保护的 `ai_message_inventory_bootstrap` workflow，按上段 building/cutoff/barrier/readback/seal 协议执行。sealed 后新 Task 默认合同为 current：本合同代码发布后新建的 `group_ai_chat + fact_first_v3` Task 不经过 legacy，Task 创建事务同时创建 active enrollment=`ai_message_obligation_v1`，失败则 Task 创建整体回滚；首次 start 建 TaskDayLedger 时同事务建 active route。存量无 enrollment Task 只有在 policy sealed+allowed、存在匹配当前 allowed epochs 的 inventory item=`open` 且现有 TaskContractRoute 合法时才可继续显式 legacy route；复制/重建 Task 视为新 Task，不能借无 enrollment 取得 legacy 权限。受控 legacy lifecycle/config 命令必须在同一事务以 expected item version 同步 allowed epochs；受控 takeover 创建 enrollment=`preparing` 时必须在同一事务把对应 item `open -> enrolled`。普通 stop 可由显式 start 恢复，不能 retire item/enrollment；只有 delete operation 已 fence、归档与守恒 readback 完成，或运营以不可逆 retire workflow 明确放弃未接管 legacy Task 后，inventory item 才可审计 CAS `open|enrolled -> retired`，current enrollment 才可 CAS `preparing|active|blocked -> retired`，随后才物理删除 Task。current 新 Task不在冻结inventory中，仅保留retired enrollment/tombstone；inventory item无论最终enrolled或retired都继续计入原membership hash和fleet总数。current epoch 与 item allowed epoch 漂移时阻断，不能仅凭 membership hash 放行。

仓库现有三层控制必须组合成一个共享 `require_ai_group_message_route(operation, frozen_identity)`，不允许各模块自行解释：

```text
write_allowed =
  DispatchClaimScope 当前允许新 claim，且 version/topology/active contract 匹配
  AND TaskContractRoute/activation manifest 允许该 Task 与当前部署合同
  AND AiGroupMessageContractFleetPolicy 允许该 Task 的 current/legacy route
  AND AiGroupMessageContractEnrollment = active + ai_message_obligation_v1
      + open_contract_blocker_count=0
  AND AiGroupMessageContractRoute = active + ai_message_obligation_v1 + running
      + activated_at非空 + open_blocker_count=0
  AND Task.task_lifecycle_epoch、dispatch scope version、task activation route epoch、
      task-day route epoch、manifest hash
      与 obligation/Action/Attempt 冻结值全部匹配
```

- enrollment 与 task-day route 均缺失只在 fleet policy=`sealed+allowed`、对应 inventory item=`open` 且 allowed epochs匹配、并已获现有 task-level old route权限时才允许旧 writer；新 writer false。item缺失/非open、inventory building、fleet policy=`preparing|disabled`后，任何无 enrollment Task都是contract blocker。enrollment一旦存在，当前/未来ledger的legacy AI writer永久false；route缺行也不是legacy许可。
- `preparing|blocked` 将该 task-day 的新旧 Planner、Generation claim 和任何新 Gateway 调用全部 fenced；只允许读取和对已冻结 request 的 reconcile。
- 只有 enrollment `open_contract_blocker_count=0` 且 route `active+running+activated_at非空+open_blocker_count=0` 才允许新 writer；任一 enrollment/route blocker 或 `paused|stopped|incompatible` 都禁止新物化、Provider request、Tx A/Tx B和新网络调用。`operation=reconcile` 仍可追加真实fact并打开更多blocker，不能因已有blocker丢失外部事实。
- `operation=write` 使用上述完整 AND；`operation=invoke_committed` 只允许 Tx B 已提交的精确 call-issued row、原 `invocation_owner_token` 与冻结 request 执行一次 adapter call，即使随后 pause/stop 也可完成这个已跨业务边界的调用，其他 worker/lease recovery 永不重放；`operation=reconcile` 只允许冻结 identity/epoch 的结果 journal、remote fact、projection、deadline 与 lease recovery，即使 Task paused/stopped 或全局 claim scope 关闭也可收口，但不得新建 Action/GenerationJob/request 或调用 Telegram；`operation=read` 只做授权后的只读诊断。
- `operation=ledger_route_bootstrap` 是唯一可创建新current TaskDayLedger+targets+read-model+route的入口，调用来源为：`first_start`、持续running Task的`automatic_natural_rollover`、以及period结束后的`resume_rollover|start_after_stop_rollover`。新增持久 `AiGroupLedgerBootstrapOperation`，数据库唯一 `(task_id,next_period_start_at,enrollment_epoch)`，保存 `current_request_revision`、state=`pending|processing|completed|blocked`、lease/cursor、result ledger+route/version与row version；每个请求版本另写 append-only `AiGroupLedgerBootstrapRequestRevision`，数据库唯一 `(bootstrap_operation_id,request_revision)`，冻结 caller kind、source ledger/route identity、expected Task/lifecycle/domain revisions、target-set hash+version、request hash、supersedes revision/reason与created_at。first/start类仍由TaskStartOperation外层同事务创建/推进该operation；running Planner只insert-on-conflict一个pending请求版本并让出，不自行建ledger，`recovery`的ledger-bootstrap drain用lease/CAS执行。调用方提交source ledger identity/period end（首日为空）、expected Task/config/target revisions、lifecycle/Fleet/Enrollment/TaskContract/Dispatch versions和完整target-set hash+version。

BootstrapOperation字段必须显式包含`next_retry_at/lease_owner/lease_epoch/lease_expires_at/version`，领取只用partial index `(next_retry_at,next_period_start_at,id) WHERE state IN ('pending','processing')` 做有界keyset，然后以`state/version/lease`条件单行`UPDATE ... RETURNING`。processing lease过期时recovery必须先按唯一period/ledger/route/result hash做独立readback：结果已原子存在则收口completed，全部不存在才能`lease_epoch+1`重领，半存在或identity冲突转blocked并打开contract blocker。不使用扫表、OFFSET、`FOR UPDATE/SKIP LOCKED`或不带result readback的lease重置。

bootstrap事务对所有caller都必须先CAS enrollment `active + open_contract_blocker_count=0`；存在source ledger/route的rollover caller还必须分别要求source settlement operation=`completed`、source route `open_blocker_count=0`。zero-ledger `first_start`不适用source前置，只能以zero-history manifest与enrollment/Task/TaskStartOperation expected versions进入完整首日bootstrap，禁止伪造settlement或closed route；user-start外层已按§7.5取得enrollment/Task/StartOperation锁时，bootstrap复用同一事务已持有的enrollment锁与expected version，不得二次反序获取。任何enrollment-scope quantity conflict/poison/identity/conservation blocker或未完成结算都跨午夜继续fence，不能通过关闭旧route洗掉。`automatic_natural_rollover`的BootstrapRequest冻结且只接受两种互斥`source_mode`：`normal_running`要求Task仍running、旧ledger period已结束、旧route=`active+running`且settlement completed，同事务把旧ledger/day phase与route收口closed后建新ledger；`takeover_closed`要求Task仍running、旧route=`active+closed`、settlement completed、同route imported-baseline/final-manifest/readback hash一致且没有next ledger，只把closed route作source evidence，绝不重开或再次close。两种mode都匹配source route id/version/class，消费当时next-ledger revision，并创建新ledger、完整TaskGroupDailyTarget set、read-model row、完整SettlementOperation+每target item与route=`active+running,open_blocker_count=0`。新active route的settlement必须在该事务写`state=pending,activation_ready=true,next_retry_at=deadline_at`，并把source mode、operation/item identities、count/hash和activation值纳入BootstrapOperation/TaskStartOperation result hash；不得使用takeover专用的false/null默认。resume/start rollover使用相同构造但匹配paused/stopped及adoption ready/complete，最终Task/新route状态按命令变running。pause/stop/PATCH/start、contract_reopen、settlement与rollover由expected Task/enrollment/route/domain/blocker/settlement revisions决定唯一winner；旧route late fact只reconcile。它不能补已提交但缺route的ledger，不能激活takeover preparing/blocked，不能创建义务/Action/GenerationJob/request或调用Telegram；合法orphan只由审计recovery按原ledger identity修复。

同一稳定 bootstrap operation identity允许替换的只是**尚未产生任何ledger/route结果的请求版本**。若pause/stop/next-ledger PATCH先赢，drain以expected request/Task/domain version把旧revision记为`blocked(stale_precommit)`；随后resume/start/automatic rollover可在确认 `result_ledger_id/result_route_id IS NULL`、目标period不存在任何ledger/route、operation不在有效processing lease（过期lease先做结果readback并收口为blocked）后，以expected operation version/current revision CAS `request_revision+1`、append新revision、state回pending。旧revision不可改写。两个replacement只有一个CAS winner，失败方回读并只在request hash相同才幂等成功。只要operation已记录result，或数据库已存在该period任何ledger/route，即永久禁止替换：完全相同period/enrollment/target-set/result identity只回读completed，任何差异都是`contract_bootstrap_identity_conflict`。这样auto→pause→resume、auto→next-ledger PATCH与worker crash不会永久堵住新任务日，也不能借supersede制造第二个ledger。
- 所有 `first_start/start_after_stop/natural_rollover start` 继续由既有 `execute_task_start_contract -> start_task_once` 外层持有每Task至多一条current `TaskStartOperation`，不得直接从router调lifecycle helper。保留现有 `task_id` 主键、全局 `UNIQUE(start_operation_id)`、`operation_version`与状态 `processing|started|failed`，不新增持久`replaces_id`、历史多行或复合unique；replace命令仍以`expected current start_operation_id + operation_version`校验后覆盖同一current row，并由AuditLog留历史。现表只additive增加 nullable `result_task_lifecycle_epoch/result_route_id/result_route_epoch/result_target_set_hash/result_target_set_hash_version/result_bootstrap_request_revision/result_snapshot_hash`，复用已有`task_day_ledger_id`作为结果ledger。每次赢得`-> processing`的CAS必须原子清空旧ledger/result字段；只有`processing -> started`的同一事务可一次性写全结果字段与hash，`failed`全部为空。外层orchestrator严格按§7.5先锁/CAS TaskContract/Fleet/Inventory → Enrollment → Task → TaskStartOperation，再在**同一事务复用这些锁**调用`lifecycle_control/ledger_route_bootstrap`；不能先Task/StartOperation后反锁Enrollment。同start operation id重放只回读相同started结果，replace/stale expected version或任一内部CAS失败按现有failed/stale command语义整笔回滚，不留下半建ledger/route或部分result。`resume`不是start operation，继续以lifecycle command id/Task状态幂等。
- `operation=takeover_activate_running|takeover_activate_paused|takeover_activate_stopped|takeover_activate_closed` 只允许受保护 takeover workflow 在完整 conservation/readback 后按 §10.3 frozen class调用。running cutover pause已把Task lifecycle epoch从旧值推进到`E`，最终事务以Task=`paused`+version+epoch=`E`、inventory item=`enrolled`、enrollment/route=`preparing`、完整target/manifest/readback hash为CAS条件，同时写Task=`running`（含next_run_at/runtime projection）、enrollment=`active`、route=`active+running`，但epoch保持`E`不再递增。`same_period_running|paused|stopped`的最终CAS还必须使用数据库时间证明`database_now < ledger.deadline_at`；等于或晚于deadline即旧class失效，事务零激活并保持preparing，受保护workflow supersede旧manifest，按projection/settlement事实重新冻结`settling_closed`，已完成settlement才可为`rollover_eligible`，禁止过期route变active-running或先开新发送owner。paused/stopped/closed分别要求源Task仍为同名状态/版本/epoch并最终写route=`active+paused|stopped|closed`，Task状态与epoch原样；它们不得创建可发送owner。`enroll_never_started|enroll_rollover_eligible|retire_terminal`使用各自零集合/closed-ledger/tombstone manifest，不借空route调用activate。任一失败整笔回滚，绝不先active-running再普通pause/resume/stop；这些operation不能用于open blocker route、普通start/resume、跨日bootstrap或绕过readback。
- `operation=lifecycle_adopt` 是旧 epoch pre-call unit进入当前epoch的唯一入口。pause/stop事务先推进`Task.task_lifecycle_epoch`；存在route时只同步`route.task_lifecycle_epoch=Task新epoch`和writer state，`route.route_epoch`保持不变，确保旧call-issued仍能按原route identity reconcile。同事务插入LifecycleAdoption=`pending`并立即fence旧owner；`recovery`先以obligation `(task_day_ledger_id,task_lifecycle_epoch,state,id)` keyset完成initial discovery，只负责幂等插入initial AdoptionItem并推进discovery_cursor，随后严格按item `(state,item_seq,id)`领取。只有 `old_epoch < to_epoch=current Task epoch`、相同enrollment/manifest lineage、无call-issued/Gateway hold、无不可确认Generation request，且active owner已用expected version安全终结/释放时，item事务才能CAS obligation epoch到current：Task/route paused时进入`waiting_dependency(task_lifecycle_paused)`，running时重评估后open/其他waiting；stopped时必须在同一事务终结Action/FOP、写`cancelled_by_task_lifecycle`并把`due_rank_state active -> retired`及`rank_retired_reason=task_lifecycle_stop`，释放active-rank unique供未来更高ordinal补位。item applied、owner terminal、wake/read-model同事务收口。route缺失只允许pause/stop adoption做waiting/cancel/reconcile，不得open；call-issued/unknown/Generation reconcile/protected owner保留原Task lifecycle epoch与route epoch并把initial item置deferred，绝不得退休rank。

以后同request取得safely-not-executed/pre-call-safe证据时，Tx C必须在锁obligation/Action/journal前先按§7.5锁adoption与原deferred item，append或回读唯一SafeEvidence。accepted winner在同一事务要求原item仍为deferred，以expected item/adoption versions把原item重排为pending、分配大于当前cursor的新`item_seq`、写`latest_safe_evidence_id`，同时只执行一次`deferred_count-1`、`ready -> pending`（或保持pending/draining）与`adoption_seq+1`；不得新建第二个AdoptionItem。双证据、重复request或崩溃重放只能回读同一accepted evidence/result，不能再次递减deferred。adoption worker处理被重排的原item后重新判ready/complete。普通write不能隐式改epoch。

adoption sweep完成时，以adoption version/discovery_complete、`pending item=0`与`blocked_count=0`一起CAS `draining -> ready|complete`并记录adopted/deferred/blocked counts；deferred reconcile不阻断ready，blocked item必须阻断。resume必须等待当前epoch adoption=`ready|complete`且active route存在/匹配；恢复后复用最近pause产生的current epoch，不再递增。只有 Task 状态真正从非paused CAS为paused，或从非stopped CAS为stopped 的 winner 才能推进epoch并创建该epoch adoption；已经paused再次pause、已经stopped再次stop，即使使用不同request/command ID或并发入口，也只关联Audit并回读原状态，不推进epoch、不创建新adoption。双resume按command id/source state幂等，下一次完成 running→paused 的pause才推进新epoch。Generation persist、Gateway Tx A/Tx C与adoption worker竞争时只有一个expected obligation/owner/route/adoption version CAS成功，失败方整笔重读。

takeover preparing时另建 `AiGroupTakeoverSourceFence`，数据库唯一 `(task_day_ledger_id,route_epoch)`，保存 `a_event_version/reconcile_delta_version/reconcile_delta_hash/last_event_seq/version`。对应 `AiGroupTakeoverSourceEvent` 保存 `fence_id/event_seq/source_kind/source_identity/source_revision/transition_kind/payload_hash/created_at`，数据库唯一 `(fence_id,source_kind,source_identity,source_revision,transition_kind)`；`event_seq` 来自数据库 sequence。高频 scoped A 类 writer（legacy Action/Attempt/Generation、Gateway journal/call-issued/result、remote fact/projection、coverage/claim/alias）必须先按§7.5 route层 CAS/持有 source fence，再 `INSERT ... ON CONFLICT DO NOTHING RETURNING` source event，只有 insert winner 才把 fence `a_event_version+1,last_event_seq=event_seq`，随后写原事实并同事务提交；冲突重放只回读同 payload hash且不二次 bump，唯一键相同但payload不同立即contract-blocked。readback按 event_seq/C-normalized identity 重算规范 hash，sequence空洞允许但排序不得变。Task/ledger/target/config/pacing、Provider model/config/policy、TaskContract/Fleet/Inventory/Enrollment/Route等有限权威行不做全fleet fanout，manifest/readback保存其规范化 `a_static_revision_vector_hash`，activation事务按§7.5顺序锁同fence、重读并重算。

remote-fact Tx C、fact projector、coverage transition、check-in claim和legacy alias apply都必须调用同一`append_takeover_source_event_if_preparing()` helper，不得因为路径名为“reconcile/projector”就跳过。helper在route/settlement（如需）之后、binding/target/obligation/coverage之前执行；每个writer的真PostgreSQL测试都要强制`manifest readback -> A writer -> activate`失败，并证明source event/fact同成同败。

`takeover_activate` 最终事务不能只在开始时读取 static vector，也不能先锁target再回头锁SourceFence。它必须严格按 §7.5 的层级取得并持有写锁：`global Provider/Dispatch/TaskContract/Fleet/Inventory -> Enrollment/Task/TaskRevision -> ledger/route -> SourceFence -> target/pacing/config`；每层多行均按table name+primary-key的C-collation规范顺序，对manifest列出的A类有限权威行执行 `UPDATE ... SET version=version WHERE id=? AND version=? RETURNING version` 等价no-op CAS，所有锁持有到事务提交。取得SourceFence锁后才锁target/pacing/config，禁止target→fence反序；对应static writer和projector/reconcile更新这些行时也必须走同一层级。随后以全部RETURNING值重算`a_static_revision_vector_hash`，并在已持有Task/enrollment/route锁的同一事务执行最终 active CAS。writer先提交则expected version CAS=0并阻断activation；activation先取得相应线性化锁则writer只能在activation提交后生效。禁止“重读后释放snapshot再激活”。最终CAS还必须匹配readback的`a_event_version/reconcile_delta_version/hash + a_static_revision_vector_hash`。B类liveness/dedupe不进入activation fence，变化可激活但owner仍实时重评估；任何A writer漏source event、违反锁序或static row CAS失败时原事务整体重试，不能只落事实。
- `operation=lifecycle_control` 是唯一可写管理面模式，输入必须冻结 `command/source_task_status/expected_task_version/task_lifecycle_epoch/fleet policy epoch+version/enrollment state+version/route migration status+writer_state+version`；route 缺失时改为提交 `expected_route_missing + task_day_ledger_id + enrollment_epoch`，不能省略 identity。它只授权下表所列 Task/enrollment/route/config revision/AuditLog CAS，禁止物化义务、Action、GenerationJob、request 或调用 Telegram；CAS 后仍需重新走 `write` 才能恢复业务 writer。命令/源状态矩阵如下：

| command | 允许源状态 | 允许的合同变更 | 明确拒绝 |
| --- | --- | --- | --- |
| `PATCH` | enrollment/route 为 `preparing/blocked/active`，writer 为 `running/paused/stopped/incompatible`；或满足冻结 legacy inventory 的旧任务 | 仅展示字段或 §8.3 定义的 next-ledger/scope/config revision | 改当前 target-set hash、ordinal、事实、hold、history |
| `start` | new current Task尚无ledger；或Task=`stopped`且current epoch stop/imported-baseline adoption=`ready|complete`；跨period可为latest route=`active+closed`或manifest证明从未有route | 必须由TaskStartOperation owner调用；first start按bootstrap建首ledger；同period start-after-stop把Task/active route `stopped -> running`并复用stop/imported epoch，不复活cancelled unit/旧Action，Planner只按新restart/effective target revision分配更高ordinal补当前due；period已结束只把closed prior route当source evidence，原子建新ledger/route，绝不把closed route改running | adoption pending/draining、takeover preparing/blocked、completed/deleted/incompatible、StartOperation/epoch/hash漂移、enrollment blocker非0 |
| `resume` | current period enrollment=`active`、route=`active+paused`、Task=`paused`且current epoch pause/imported-baseline adoption=`ready|complete`；或period结束且latest route=`active+closed`/manifest证明无route并有imported baseline | 同periodTask与route CAS到running并复用最近pause/imported epoch；跨period通过resume_rollover bootstrap新ledger/route，closed prior route保持closed；随后writer重新鉴权 | adoption pending/draining、current-period missing route、takeover preparing/blocked、stopped/completed/deleted/incompatible、epoch/hash漂移、enrollment blocker非0 |
| `pause` | enrollment=`active|preparing|blocked`且Task实体存在、Task=`running|paused`；route可为对应`preparing|blocked|active+running|active+paused|active+incompatible`，或有精确current ledger/zero-ledger expected-missing evidence | 仅Task `running -> paused` CAS winner推进Task lifecycle epoch并创建adoption pending；存在route时只写writer paused与`route.task_lifecycle_epoch=Task新epoch`，`route.route_epoch`不变；缺route冻结expected-missing evidence；已paused无论command ID只幂等回读，preparing/blocked保持fence | enrollment=`retired`，Task=`draft|pending|target_reached|wrapping_up|failed|completed|stopped|deleted`，route=`closed`，借pause激活/补建route，或取消call-issued |
| `stop` | enrollment=`active|preparing|blocked`且Task实体存在、Task=`draft|pending|running|paused|stopped`；route可为对应`preparing|blocked|active+running|active+paused|active+stopped|active+incompatible`，或已冻结zero-ledger/current-ledger expected-missing evidence | 仅Task从非`stopped -> stopped` CAS winner推进Task lifecycle epoch并创建adoption pending；存在route时只写writer stopped与`route.task_lifecycle_epoch=Task新epoch`，`route.route_epoch`不变；缺route仍按adoption守恒收口；已stopped无论command ID只幂等回读；未来显式start可按上行恢复 | enrollment=`retired`，Task=`target_reached|wrapping_up|failed|completed|deleted`，route=`closed`，新业务写、删除hold/fact/projector状态，或隐式自动start |
| `retry/reset` | 任何 enrollment 已存在的 Task | 无 | 固定 `409 ai_message_contract_managed_recovery_required` |
| `delete` | 任意 enrollment/route 状态；先满足 stop CAS | soft tombstone；物理删除仅走 §8.3 保留与守恒前置 | 绕过 stop、hold/reconcile/projector 或审计 |

`failed|completed|deleted`与 enrollment=`retired`是管理面不可逆状态，不能被pause/stop/start/resume/retry/reset改写；只允许授权诊断、同request reconcile、归档/删除或明确的新Task。`target_reached|wrapping_up`只允许deadline settlement、closed-route reconcile、归档/删除，不再转paused/stopped。helper在解析已删Task的retired enrollment/tombstone时只回读审计状态，没有`task_record_id`就不存在任何lifecycle CAS目标。HTTP和批量/内部调用共用该矩阵，不同入口不得各自放宽。

missing enrollment 仅在 fleet policy=`sealed+allowed`、对应 inventory item=`open` 且 frozen epochs 匹配、原 TaskContractRoute 合法时返回 `legacy_control_allowed`，继续既有 legacy 管理命令；item 缺失/非 open、inventory building、policy=`preparing|disabled` 时 fail-closed。enrollment 已存在且当前 ledger 已提交但 route 缺失时，`start/resume/PATCH/ledger_route_bootstrap` 不能补建或激活 route；`pause/stop/delete` 仍可用 enrollment+Task 的 expected versions 收口，route 只能由受审计 recovery 补同一 ledger identity。
- Planner、Generation、Dispatcher/Gateway、wake、provider reconcile、remote-fact projector、deadline/recovery 与 generic Task lifecycle service 必须调用同一 helper；Action/Attempt 使用结构化列冻结 dispatch-scope version/fingerprint、task activation route epoch、task-day route epoch、lifecycle epoch 与 manifest hash，不只塞 payload JSON。缺行、epoch 漂移或 manifest 不一致 fail-closed 并按registry写 typed contract blocker。

## 5. 义务状态机

| 当前状态 | 含义 | 允许的下一状态 |
| --- | --- | --- |
| `open` | facts 当前允许物化 | `materializing`、waiting、cancelled、shortfall、contract error |
| `materializing` | Planner 已 CAS 领取并绑定 allocation/assignment、immutable intent 与 FOP；normal body不得创建Action，deterministic check-in仅在scoped claim+memory+intent同事务完整后可直接创建ready Action | `generation_pending`、`action_bound`（仅check-in分支）、`open`、waiting、`contract_error` |
| `generation_pending` | 唯一 GenerationJob 正在生成/持久化 | normal accepted variation到`action_bound`；六轮耗尽且符合direct签到资格时原子到`check_in_ready`；其余到waiting、`generation_reconcile`、`contract_error` |
| `check_in_ready` | Generation已持久终结六轮预算并交回同一义务，或Planner已确定direct check-in分支；尚无签到Action | Planner核对handoff/scoped claim+memory+intent后到`action_bound`；owner占用则confirmed projection/hold/`content_capacity_gap`；deadline后只允许settlement target chunk转`terminal_shortfall` |
| `action_bound` | 唯一 Action 内容已绑定或正在准入等待，尚未跨 Telegram mutation 边界 | `gateway_prepared`、`open`、waiting、`generation_reconcile`、`contract_error` |
| `quality_waiting_context` | 当前生成 epoch 已无可证明的新 variation | 新 wake version 后 `open`；deadline 后只允许settlement target chunk转`terminal_shortfall` |
| `content_capacity_gap` | 当前事实下无合法正文/check-in/账号内容容量 | 资产、资格、上下文或 policy 事件后 `open`；deadline 后只允许settlement target chunk转`terminal_shortfall` |
| `waiting_dependency` | Provider、准入、Session、transport 等依赖未就绪 | 对应依赖事件后 `open`；deadline 后只允许settlement target chunk转`terminal_shortfall` |
| `generation_reconcile` | Provider request/persist outcome 不确定，但未触发 Telegram mutation | 同 GenerationJob/request 对账后 `action_bound`、`open`、waiting 或 shortfall；不能新建业务义务 |
| `gateway_prepared` | Tx A 已冻结 request/mutation identity 与 Attempt；Tx B `call_issued` 尚未提交，因此 adapter 被禁止外调 | 无 call-issued 时 recovery 按当前 lifecycle 审计后转 `open`、task-lifecycle waiting/cancelled；提交 call-issued 时转 `gateway_unknown_hold` |
| `gateway_started_hold` | evidence journal 明确证明 mutation 已开始 | `confirmed` 或 `remote_reconcile_only`；禁止回 `open` |
| `gateway_unknown_hold` | Tx B 已持久化同 request `call_issued/ambiguous`；无论 socket 是否真正发出都按可能执行占位 | deadline前同 request typed safely-not-executed可回`open`，明确started转started hold，否则confirmed；deadline后只由settlement target chunk转remote-reconcile-only；占位期间禁止replacement |
| `remote_reconcile_only` | unknown deadline 已关闭，只读对账 | 迟到权威事实只写fact pointer/timeliness与历史修正，状态保持；禁止新发送 |
| `confirmed` | 已有合法 typed remote fact | 终态 |
| `cancelled_by_target_revision` | 目标 revision 下调且未物化/未进 Gateway | 终态；不计成功，ordinal 不复用 |
| `cancelled_by_scope_revision` | 上游 coverage 取得权威 `abandoned_for_day` 且未物化/未进 Gateway | 终态或按守恒规则先转 extra-volume；不计成功 |
| `cancelled_by_task_lifecycle` | Task 明确 stop/delete，且 unit 未提交 call-issued | 终态；该旧identity不计成功/shortfall，同事务`due_rank_state=retired`并保留rank/停止审计，ordinal不复用。stop不缩target：未重启留下的空rank在deadline另写known shortfall identity |
| `contract_error` | 未分类代码错误、约束破坏或 route/manifest 不一致 | 非热循环 blocker；只有新代码/合同版本及人工 CAS 可恢复 |
| `terminal_shortfall` | deadline 到达仍未满足 | 终态；迟到fact可绑定并标记resolved-late，但状态与deadline settlement保持，不重开发送 |

`abandoned_for_day` 是 coverage/account scope 的上游状态，不是含混的 message obligation 状态；它只通过带 scope revision 的 CAS 触发 `cancelled_by_scope_revision` 或合法 extra-volume 转换。GenerationJob 的 provider response/persist unknown 统一进入 `generation_reconcile`，与 Telegram `gateway_started_hold/gateway_unknown_hold/remote_reconcile_only` 分账。

typed→FOP 映射固定为：

| typed obligation | FOP state |
| --- | --- |
| `open` | `open` |
| `materializing` | `materializing` |
| `generation_pending`、`check_in_ready`、`action_bound`、`gateway_prepared` | `action_bound`；`check_in_ready`由typed state+handoff字段区分，FOP不得据此假设已有Action |
| `quality_waiting_context`、`content_capacity_gap`、`waiting_dependency` | `waiting` |
| `generation_reconcile` | `generation_reconcile` |
| `gateway_started_hold` | `remote_inflight` |
| `gateway_unknown_hold` | `remote_unknown` |
| `remote_reconcile_only` | `remote_reconcile_only` |
| `confirmed` | `confirmed` |
| 三类 cancelled | `cancelled` |
| `contract_error` | `contract_error` |
| `terminal_shortfall` | `shortfall` |

deadline业务收口只能发生在§4.1 settlement target chunk：它使用同一数据库snapshot与规范锁序，把open/materializing/generation/check-in-ready/action-bound/waiting/generation-reconcile的同一unit转`terminal_shortfall`；`check_in_ready`在该chunk内以expected handoff/obligation/FOP version把pending|claimed handoff转`superseded`、清claim lease并推进read-model，之后Planner claim CAS必败。`gateway_prepared`由该chunk完成not-issued审计再shortfall；`gateway_unknown_hold/gateway_started_hold`由该chunk转`remote_reconcile_only + closed_with_unknown_shortfall`。deadline subscription、wake、Planner、Generation、Dispatcher Tx A/Tx B、safely-not-executed与lifecycle入口只拒绝新工作并激活同一operation，不能竞争终结。迟到remote fact仍append/bind，只按§4.1权威`remote_effect_at+confirmation_time_basis`写timeliness；terminal状态不转open/normal confirmed，projector只修正历史，不创建mutation、不改变immutable missed/met。

关键转移必须在同一数据库事务中完成：终结当前 pre-Gateway Action/variation/memory reservation、释放 coverage reservation、更新 typed obligation、写 blocker/wake。任何一步 CAS 冲突都显式失败并交由 recovery 读取当前事实收敛，禁止只释放 coverage 却把义务静默放回热循环。

## 6. 失败分类与恢复合同

| 失败类型 | Action | 义务处理 | 唤醒 |
| --- | --- | --- | --- |
| `duplicate_message` / 模板 / 语义重复 | 当前 Action terminal failed，无 Gateway fact | 新禁用集合或未用角度成立则原义务下一轮 `open`；basis 不变则 `quality_waiting_context` | context/mask/policy/dedupe expiry |
| scoped check-in 已占用 | 不创建新的发送 Action；已有生成 Action可终结为明确 capacity decision | owner confirmed 则投影 coverage；owner open/unknown 则 hold；资格用尽且无正常内容则 `content_capacity_gap` | profile/context/policy 或 owner remote fact |
| 缺面具且 scoped check-in 可用 | 原 coverage 义务生成统一 check-in | 继续 `action_bound`，真实成功后 confirmed | Gateway result |
| 缺面具且 check-in 已用 | 不创建重复签到 Action | `content_capacity_gap(profile_unavailable_check_in_exhausted)` | active profile event/deadline |
| Provider timeout/unavailable/quota | 不用新 Action 重置生成轮次；GenerationJob/Attempt 保留类型化失败 | `waiting_dependency`，未分类程序错误升级 task-scoped contract blocker | provider config/health epoch 或明确调度的 dependency probe |
| Provider response/persist outcome unknown | 不新建 GenerationJob/Action | `generation_reconcile`，按同 request/job 对账；无 Telegram 副作用 | provider audit/persist recovery/deadline |
| normal主/备用各3轮耗尽，且extra-volume+coverage已完成+direct+active/usable面具+scoped check-in五项均成立 | Generation不创建Action；同tx终结job claim并写唯一handoff | 同一义务`generation_pending -> check_in_ready`，Planner复核五项后只消费handoff建ready签到Action；未完成coverage不得handoff | Planner `check_in_ready` partial index；不得调用Provider |
| 准入/在线/Session/transport 不可用 | Provider/Gateway 前保持 fail-closed | `waiting_dependency`；其他账号/义务继续 | AccountEligibilityEvent/准入/transport event |
| 同 request typed safely-not-executed | 当前 Action terminal | 仅 `gateway_prepared/gateway_unknown_hold -> open`，materialization version +1；明确 started hold 不可释放 | 下一次 Planner rotation |
| Gateway started、结果未知 | Action unknown | `gateway_started_hold/gateway_unknown_hold -> confirmed|remote_reconcile_only` | 只读远端对账；unknown 占位期间禁止 replacement |
| 未分类代码异常/合同不变量破坏 | 显式 error | 当前义务或 Task lane `contract_error`；不得 fallback | 新代码/contract version 人工恢复 |

废弃新数据错误码 `direct_check_in_10d_duplicate`。它只保留为历史展示映射；新 check-in 决策使用 `check_in_scope_occupied`、`content_capacity_gap` 和具体 trigger/blocker。

统一 check-in 的两个 trigger 不得混用：`mask_missing` 只允许当前未完成 coverage 的 direct obligation，且该账号不能领取独立 extra-volume；`normal_generation_exhausted` 可用于已经完成当前任务日 coverage、具有 active/usable 面具、正常正文主/备用各 3 轮已在同一 generation epoch 用尽、relation 仍为 direct 且 scoped check-in 未使用的 extra-volume obligation。reply/强引用永不降级 check-in。两种 trigger 共享同一 scoped unique、remote mutation 和 unknown 防重合同。

账号面具 Job/Item 已批准的专用恢复合同保持不变，包括当前显式尝试审计与 4 次后 `manual_required`；本事故不新增通用 `Action.max_retries`，也不把 manual-required 伪装成 active profile。人工创建 linked retry item 或 active profile 版本落库时，事务内递增对应 account 与 task-day aggregate wake clock。

## 7. Planner、Generation、Dispatcher 数据流

### 7.1 Planner 有界批次

保留当前单次最多20条的事务上限，但数量必须先在**同一current TaskDayLedger + target operation + database now**的stable rank集合上定义，不再用可重叠的标量计数相减。对target定义`RankDueNow={r | 1<=r<=cumulative_due(database_now,effective target,pacing)}`；每个rank通过当前active owner映射到至多一个quantity identity，owner的持久`due_at`还必须`<=database_now`。其他ledger/target、未生效next-ledger revision、rank大于当前cumulative due、`due_rank_state=retired|protected_overage`均不在当前`D_now`。每个rank按下列优先级只归入一个互斥集合：

```text
C = active rank owner已有bound quantity fact的confirmed unit
U = active rank owner无bound fact且已进unknown/remote-reconcile-only
H = active rank owner无bound fact/unknown，但Tx B call-issued/Gateway hold已提交
P = active rank owner无C/U/H，但有current epoch有效Generation/Action pre-call owner
W = active rank owner无C/U/H/P，但处于waiting/ineligible
T = active rank owner为terminal-shortfall或其他明确禁止replacement的终态unit
R = active rank owner无C/U/H/P/W/T且open/ready、无有效owner
M = RankDueNow中尚无active owner的rank

D_now = RankDueNow
due_by_now = |RankDueNow|
volume_need = |R| + |M|

coverage_need = 当前到期且没有现存 coverage subtype/unknown/confirmed 占位的未覆盖义务数

planning_need = max(volume_need, coverage_need)

batch = min(
  planning_need,
  distinct ready/online/admitted/progressable accounts,
  real Generation + interaction free slots,
  DAILY_COVERAGE_PLAN_BATCH_LIMIT,
  20
)
```

`C/U/H/P/W/T/R/M`必须以target active-rank、obligation、fact/binding和Gateway journal的同一快照做anti-join，转态用expected ledger/target/effective revision/obligation version CAS；禁止分别count后相减。`W/T`继续计入due/欠额和结算守恒，但不是当前Planner候选、不占worker槽；`P`已有owner也不重复物化。Planner按`rank,due_at,quantity_ordinal`从`R∪M`取不超过batch的candidate：`R`复用原义务；`M`先检查同rank protected owner并恢复，否则才用target单行CAS匹配`effective_planned_target_revision + target.version + next_quantity_ordinal`分配新identity、写`effective_due_rank`并insert-on-conflict。本轮最多物化20条stable unit/FOP、allocation assignment与immutable intent，不一次创建数千欠额。normal body转`generation_pending`且Action=0，由Generation对唯一job/accepted variation+memory CAS后原子创建ready Action；deterministic check-in只有scoped claim+memory+intent同事务完整时由Planner直接创建ready Action；takeover apply只能按§10.3 final manifest导入已验证legacy content例外。coverage若可转换必须复用同一active rank owner。只有允许的observed overage才创建`protected_overage` identity且不偷偷扩大当前DueSet。decision id只记录来源，不参与业务去重；每个Task仍只调用一次build_plan后轮转。

blocked 账号继续 keyset 扫描后续页；`messages_per_round/max_concurrent/participation_rate/hard-hourly` 都不成为 fact-first 批次上限。

Planner 先读取当前未结 `AiGroupContentAllocationPlan` 的未绑定 requirement assignments；只有逻辑 Cycle 的 `scope_total_units` 已全部分配或显式 content-shortfall，才以 content cursor CAS 创建下一 plan。一次最多 20 条的技术批次只消费 assignments，不创建新的 reply/material 配比起点；并发 Planner 通过 `(allocation_plan_id,plan_unit_ordinal)` 与 obligation active-assignment partial unique 收敛。

### 7.1.1 动态 coverage 加入与退出

所有current-ledger scope join/abandon 都必须按 §7.5 先取 route→SettlementOperation→target 的规范锁/CAS顺序，并在同一CAS要求 `database_now < ledger.deadline_at AND settlement.state='pending'`。settlement=`processing|blocked|completed`或数据库时间已到deadline时，只记append-only late scope observation/Audit并更新next-ledger account-scope revision；禁止改current effective target/revision、创建/取消current coverage obligation或改immutable settlement。scope writer与settlement并发由该顺序和expected versions决定唯一winner，不使用应用时钟判断。

任务日内新增必达账号时，按以下顺序守恒：

1. 若已有同 Task/群/账号/任务日的合法 remote message fact，直接投影 coverage confirmed，不再发消息；
2. 否则优先 CAS 将一个尚未物化、未绑定账号且 requirement assignment 与该 coverage 能力兼容的 extra-volume obligation 转为 coverage subtype，冻结 coverage/account 并创建新的 content intent revision；不得为转换静默修改 reply/material/act-type assignment；
3. 没有兼容可转换 unit 时，新增一个 coverage obligation、分配守恒的 requirement assignment，并只递增 `effective_planned_target_revision/effective_planned_target + target.version + read_model_version`，不改base revision/target或target-set hash，确保 coverage 目标不会被原配置数量吞掉；
4. action_bound、Gateway-started、unknown、confirmed 的 unit 永不改绑、删除或转换。

账号取得权威 `abandoned_for_day` 且 obligation 尚未物化时，coverage 不再进入动态分母；如果群日数量仍欠且账号候选合法，可按 target revision CAS 转 extra-volume，否则取消该 unit。普通面具缺失、内容重复或暂时离线不是 abandoned，不得借此缩小 coverage 或转走 unit。

### 7.2 Generation

1. claim 现有 open obligation，而不是 claim 无身份 Action；claim/materialize CAS 同时要求数据库时间早于 deadline；
2. 读取 immutable content intent、账号 binding、context/mask/policy version；
3. 创建或读取唯一 GenerationJob 与 variation；只有新 provider request CAS 要求数据库时间早于 deadline，已存在 request只能 reconcile；
4. 主/备用各 3 轮累计在同一 generation epoch，replacement Action 不能清零；
5. normal Phase C通过后memory与ready Action同事务创建；拒绝按§6原子转移；
6. 主/备用各3轮耗尽时，只有义务为extra-volume、coverage已由bound fact完成、relation仍direct、active/usable面具仍有效且scoped check-in可尝试五项同时成立，Generation才写唯一`AiGroupCheckInHandoff`并把同一义务转`check_in_ready`，不创建签到Action；Planner随后复核同五项并只消费handoff，不再调用Provider。未完成coverage的normal六轮失败不得handoff；
7. extra-volume与coverage正常正文必须同样有variation key；check-in使用scoped identity，不伪装普通variation。

GenerationJob 必须冻结 `provider_request_identity/idempotency_key/provider_config_revision/provider_round/request_started_at/response_hash/persist_state`。`ai-generation` role 专门 drain `generation_reconcile`：Provider 支持按 request 查询时只查询同一 request，不发新 generation；能确认响应则持久化同一 variation，能确认未执行则以同 job 继续该 provider round。Provider 不提供可验证查询时，unknown request 终结为 `generation_outcome_unknown_no_transport` 并消耗当前 provider round；由于没有 Telegram 副作用，可在同一 epoch 尚有预算时进入下一 variation，预算耗尽则等待外部 basis。任何分支都不新建 obligation，也不清零 3+3。heartbeat 发布 reconcile backlog、oldest age、last success 与 typed error；Provider 仅健康恢复可唤醒 dependency，但不改变 generation epoch，模型/配置 revision 变化才改变 basis。

### 7.3 Dispatcher/Gateway

Gateway 协议固定为三次数据库事务加一次事务外调用，禁止把网络调用包在数据库事务中：

1. **Tx A / prepare**：重读 obligation、intent、variation或check-in identity、账号、准入、reply target、memory 和统一 route guard；CAS `action_bound -> gateway_prepared` 必须同时要求数据库时间早于 deadline，冻结 Action、Attempt、task/task-day route epoch、manifest hash、request/mutation identity。提交后尚不允许 adapter 外调。
2. **Tx B / call-issued**：再次校验同一 frozen identity和数据库时间仍早于 deadline，才追加不可变 `gateway_call_issued` journal，冻结不可接管的 `invocation_owner_token`，并在同一提交将 obligation 置 `gateway_unknown_hold`、Attempt 写 `call_issued_at`。若 deadline 已到，本事务不追加call-issued、不改变prepared obligation/FOP/Attempt owner，只幂等激活同一SettlementOperation并返回typed deadline elapsed；Gateway adapter 的硬前置条件是能读到完全相同 request的 committed call-issued行，没有该行不得调用 Telegram。
3. **事务外 Telegram call**：只使用冻结 request/mutation identity 与原 invocation owner，通过 `operation=invoke_committed` 调用一次。Tx B 后发生 pause/stop 不能撤销已 issued 调用；原进程可完成一次 call/result，其他 worker/recovery 不能接管或再次调用。进程从 Tx B 提交开始崩溃，一律按 unknown hold，不得由 lease expiry、缺 result journal 或“可能尚未建立 socket”推断 safely-not-executed。
4. **Tx C / result**：同 identity 持久化 typed result。deadline前，`safely_not_executed`追加明确证据并释放为open；deadline已到时仍可append不可变证据，但不得改obligation/FOP/hold，只激活同一SettlementOperation，由settlement chunk判断并释放安全owner。`started_without_result`转started hold，`unknown`保持unknown hold；成功且 `remote_message_id` 非空时始终按 request/mutation identity 追加或回读 `FulfillmentRemoteFact(remote_message_observed)`，不能因 obligation 已有 success 而拒绝真实 fact。fact同时冻结`ai_group_fact_projection_v1`、required kinds/count/set hash，并在该Tx C中插入全部required ProjectionState；任一缺行/冲突使整笔事务失败。quantity binding由 projector按 §4.1 partial unique收敛；第二真实 success落 typed unbound conflict并阻断，不重发也不谎绑。

`gateway_prepared` 且无 call-issued 能证明 adapter 不可能调用，recovery 可审计 `prepared_not_issued` 后安全恢复；一旦 call-issued 存在，只有同 request identity 的可信 typed `safely_not_executed` 可释放。任何未知、超时、worker 重启或 route pause 都不生成 replacement。

### 7.4 Remote fact projector 与 recovery ownership

`FulfillmentRemoteFact` 是不可变完成事实；Tx C 只需将 fact 与各 `FulfillmentFactProjectionState(kind)` 以 pending 原子落库，业务投影允许后续事务完成。projection row additive补 `lease_owner/lease_epoch/lease_expires_at/version`；`recovery` role持续drain pending/到期failed projection：严格按`(next_retry_at,id)`命中partial index做有界keyset，再逐行用`state/version/lease`条件`UPDATE ... RETURNING`领取并递增lease epoch；不使用`FOR UPDATE/SKIP LOCKED`。每种projection key幂等，重复fact/worker只能成功一次。

同一 fact 的 AI 投影事务按 §7.5 顺序更新 enrollment/route/ledger guard、settlement late标记、`AiGroupMessageQuantityFactBinding`/contract blocker、TaskGroupDailyTarget计数、内容 allocation read model（如需）、message obligation/FOP、可选 coverage、check-in claim/message memory、wake clock与projection state。每个fact首次投影都幂等增加raw；binding=`bound`时才同时增加既有`confirmed_message_count`，按权威remote effect basis增加on-time、late或time-unproven，settlement后投影再增加post-settlement，并CAS obligation/coverage事实指针。非terminal unit可转confirmed；`remote_reconcile_only|terminal_shortfall`只保存fact/timeliness并减少当前unresolved shortfall，typed state与immutable settlement不变。非bound只增加`unbound_observed_remote_count`及typed blocker/审计，不确认其他义务。每次提交校验`confirmed=on_time+late+time_unproven`、`raw=confirmed+unbound`及settled字段hash不变；target/coverage/claim与对应wake clock必须同一事务，避免confirmed已生效但waiting永不醒。投影失败只更新typed error、`next_retry_at`和审计，保留fact canonical与obligation hold；没有固定终止重试次数，也绝不因此重新发送。无法解析的poison row保持可见failed、打开持久enrollment contract blocker并告警，不吞掉、不伪造confirmed。

projector 支持 paused/stopped/incompatible route 的 `operation=reconcile`，但不创建 Action/GenerationJob/Gateway request。迟到 fact 仍按唯一 request/fact identity投影late/unproven history；quantity binding partial unique冲突时写unbound typed overage并打开enrollment contract blocker，两个真实消息都保留canonical fact，禁止折叠。heartbeat和监控至少包含pending/failed、oldest lag、lease recovery、last success、bound/on-time/late/time-unproven/unbound-conflict count与各projection kind lag。

### 7.5 事务边界与规范 CAS 变更顺序

所有会同时触达多类业务行的事务按以下无环顺序做 version/state CAS；只读可跳过无关层，不能逆序。该顺序用于避免不同事务形成锁环，不恢复已退役的跨表显式锁链：

```text
ProviderKey / ProviderModel / ProviderConfig / ProviderPolicy
  -> DispatchClaimScope / TaskContractRoute / FleetPolicy / LegacyInventoryItem
  -> AiGroupMessageContractEnrollment / Task / AiGroupMessageTaskRevision
  -> TaskStartOperation（仅 user start）/ AiGroupLedgerBootstrapOperation
  -> TaskDayLedger / AiGroupMessageContractRoute
  -> AiGroupTakeoverOperation / AiGroupTakeoverManifest / AiGroupTakeoverChunkCheckpoint
  -> AiGroupMessageLifecycleAdoption / AiGroupMessageLifecycleAdoptionItem
  -> AiGroupTaskDaySettlementOperation / AiGroupTaskDaySettlementTargetItem
  -> AiGroupTakeoverSourceFence / AiGroupTakeoverSourceEvent / AiGroupMessageQuantityFactBinding / AiGroupMessageContractBlocker
  -> TaskGroupDailyTarget
  -> ContentAllocationPlan / RequirementAssignment
  -> AiGroupMessageObligation
  -> FulfillmentObligationProjection
  -> AiGroupCheckInHandoff
  -> Coverage / CheckInScopeClaim
  -> GenerationJob / ContentIntent / Variation / MessageMemory
  -> Action / ExecutionAttempt / GatewayJournal
  -> FulfillmentRemoteFact / ProjectionState / WakeClock / Subscription / ReadModelRevision
```

queue claim/lease 只在独立短事务按对应 partial index keyset 读候选并逐行 CAS owner token。业务 transition 重读 route/ledger 后，在该 queue row 所属的规范层以 owner/version CAS 消费 token；不能统一拖到事务最后，也不能为了“先验 token”逆转上表顺序。后续层失败由同一事务整体回滚。各类写事务的实际 touched-row 顺序冻结为：

| transition | 规范 CAS 顺序 |
| --- | --- |
| Planner materialize | route/ledger → target effective-revision/rank-space校验与monotonic quantity identity allocation（只锁target，不在此层锁obligation）→ plan/assignment → obligation active-rank owner/state CAS → FOP claim token；normal body再写immutable intent并转`generation_pending`且Action=0。mask-missing check-in按Coverage/CheckInScopeClaim→intent→memory reservation→ready Action；已有R owner到obligation层才CAS |
| Generation normal persist/reject | route/ledger → plan/assignment（如重分类）→ obligation → FOP → GenerationJob claim token → intent/variation/memory → ready Action；非accepted分支Action=0 |
| Generation exhaustion handoff | route/ledger → plan/assignment → obligation → FOP → insert-or-read/lock `AiGroupCheckInHandoff` → GenerationJob claim token → intent/variation evidence → ReadModelRevision；只适用于coverage已完成的direct extra-volume，提交后`check_in_ready`且Action=0 |
| Planner handoff consume | route/ledger → plan/assignment → obligation → FOP → CheckInHandoff claim token → Coverage/CheckInScopeClaim → intent → memory reservation → ready Action → ReadModelRevision；不得创建GenerationJob或调用Provider |
| subscribe/wake/deadline trigger | subscribe先route/ledger → obligation expected-version行锁 → FOP → WakeClock subscription-fence → Subscription → ReadModelRevision；event/time drain先独立短事务领取/清clock lease，提交后只唤醒同一settlement owner或按同一顺序重评估未到deadline的obligation，禁止持clock锁反向锁obligation，也禁止在此入口终结handoff |
| ledger settlement/deadline chunk | enrollment/route/ledger → LifecycleAdoption（只读当前epoch）→ SettlementOperation/TargetItem claim token → quantity-binding/contract-blocker → target → obligation → FOP → CheckInHandoff（如有，expected version supersede并清lease）→ coverage → projection barrier/read-model；这是deadline业务终结的唯一owner。缺失rank只在obligation/FOP层按§4.1 CHECK插入`settlement_shortfall`终态，不触达plan/assignment/intent，也不进入任何物化索引 |
| fact projector | enrollment/route/ledger → settlement（只读/late标记）→ SourceFence/SourceEvent（preparing时必须）→ quantity binding/contract-blocker（如冲突）→ target → plan projection（如需）→ obligation → FOP → coverage/claim/alias → memory → remote fact projection/wake → ProjectionState claim token → ReadModelRevision |
| Dispatcher Tx A/Tx B | route/ledger → obligation → FOP → coverage/claim → memory → Action claim token/Attempt/GatewayJournal → wake/read-model |
| Dispatcher Tx C | enrollment/route/ledger → LifecycleAdoption/Item（safe evidence时）→ SettlementOperation/Item（只读/late线性化）→ SourceFence/SourceEvent（preparing时）→ obligation → FOP → coverage/claim/alias/memory → Action/Attempt/GatewayJournal → remote fact+完整required ProjectionState/wake → ReadModelRevision |
| lifecycle/takeover | provider/dispatch/task-contract/fleet/inventory → enrollment/Task/TaskRevision → StartOperation（如适用）→ ledger/route → TakeoverOperation/immutable manifest/checkpoint → LifecycleAdoption/Item → SettlementOperation/Item → source-fence/source-event/quantity-binding/contract-blocker → target/pacing/config → plan/assignment → obligation → FOP → coverage/claim → generation/intent/memory → Action/Attempt/journal → fact/projection/wake/read-model |

`ledger_route_bootstrap` 按 TaskContract/Fleet/Enrollment/Task → TaskStartOperation（仅user start）/LedgerBootstrapOperation → source ledger/route → new ledger/route/targets/read-model执行；新ledger/route FK同事务insert，running Planner只能创建pending bootstrap request，不允许已有ledger缺route时借此补洞。禁止 `FOR UPDATE`、`SKIP LOCKED`、OFFSET 或一次锁住跨表行集。Planner target-range、wake/fan-out、Generation、Dispatcher、projector、deadline、pause/stop、automatic rollover与takeover分别写事务矩阵测试，覆盖无deadlock/lost update/双success。任何外部Provider/Telegram调用都在事务外，调用前durable identity/hold先提交。

## 8. API、页面与权限

### 8.1 任务详情

AI 设置/履约详情新增四层摘要：

1. **业务结果**：due/target、remote confirmed、coverage confirmed、terminal shortfall；
2. **当前运行**：open、Generation active、`check_in_ready`（handoff pending/claimed）、action_bound、Gateway hold、unknown；
3. **当前等待**：quality waiting、content capacity gap、dependency waiting，含唯一义务数、账号数、首次/最近时间和wake condition；check-in handoff不是普通dependency waiting，必须单列age/lease/trigger/evidence状态；
4. **尝试历史**：success/failed/skipped Action 明细和 attempt count。

原“已执行”改为“尝试历史”；默认 active backlog 只统计 pending/claiming/executing/Generation active/Gateway hold，不把 terminal pre-Gateway Action 算作拥堵。

失败聚合接口按 `reason_code + blocker_basis_hash` 返回：

```text
unique_obligation_count
unique_account_count
attempt_count
first_observed_at
last_observed_at
wake_type / wake_at
latest_dependency_state
```

面具卡片显示 active/usable、queued/generating/retry_wait/manual_required、最后类型化错误和已有人工重试入口。`manual_required` 不自动伪造恢复；active 面具产生后由事件唤醒相关义务。

### 8.2 Read API 合同

新增 ledger-level `AiGroupMessageReadModelRevision`，数据库唯一 `task_day_ledger_id`，保存 `target_set_hash/target_set_hash_version/current_version/version/updated_at`。所有页面可见变更——target due/confirmed/coverage/projection lag、obligation state/owner/blocker、assignment/content shortfall、Action/Attempt reason/status、Gateway hold、route lifecycle/migration blocker——必须在同一事务用 helper CAS `current_version+1`；一个业务事务无论改几行只递增一次，CAS 冲突重试整个事务。它是分页/摘要快照 owner，不能用 `Task.updated_at`、各 target max(version) 或缓存时间代替。

保留 `GET /api/tasks/{task_id}/daily-fulfillment`，新增权威 `task_day_ledger_id` query；兼容 `date=YYYY-MM-DD` 只在本地日期唯一映射一个 ledger 时解析，多个 ledger 返回 `409 ambiguous_task_day_ledger` 与候选 id/timezone/period。对 current AI task additive 返回 typed `ai_message` 摘要：`task_day_ledger_id/target_set_hash/target_set_hash_version/targets[]/enrollment_epoch/route_epoch/read_model_version/as_of`；`targets[]` 每项含 `target_operation_target_id/base_target_revision/effective_planned_target_revision`，以及 total/on-time/late/time-unproven/post-settlement quantity、coverage、runtime、`generation_pending/check_in_handoff_pending/check_in_handoff_claimed`、waiting、unresolved与settled shortfall、immutable settlement status、lifecycle-cancelled、projection-lag 计数，顶层另给全 target-set aggregate和enrollment/route open blocker counts/revisions。当前只有一个目标也必须返回数组，不能用 singular target 字段暗示 route 只覆盖一部分。仅 enrollment/route 都缺失且通过 sealed legacy inventory guard 的 legacy task 返回明确 `contract_version=legacy`；enrollment 存在而 route 缺失返回 typed contract blocker，不拼装伪 obligation。

新增两个独立分页资源，避免把全量行塞入 `TaskDetailOut`：

```text
GET /api/tasks/{task_id}/ai-message-obligations
  ?task_day_ledger_id=...
  &date=YYYY-MM-DD          # 仅兼容，和 ledger id 二选一
  &target_operation_target_id=...
  &state_class=active|waiting|terminal
  &blocker_code=...
  &account_id=...
  &cursor=...
  &limit=20

GET /api/tasks/{task_id}/ai-message-attempts
  ?task_day_ledger_id=...
  &date=YYYY-MM-DD          # 仅兼容，和 ledger id 二选一
  &obligation_id=...
  &reason_code=...
  &account_id=...
  &cursor=...
  &limit=20
```

`check_in_ready`归入`state_class=active`但必须返回`materialization_mode/check_in_trigger_reason/check_in_handoff_id/handoff_state/handoff_age/lease_expires_at`；它不等于已有Action。attempts资源不为handoff伪造Attempt，handoff从义务详情typed runtime字段下钻。API/UI若只显示`action_bound`或Generation active而隐藏handoff，Release Gate失败。

`task_day_ledger_id` 与 date 必须提供其一且不可同时提供；date 多义同样返回 `409 ambiguous_task_day_ledger`。`limit` 为 1～100。obligations 固定按 `(due_at ASC,id ASC)`，attempts 固定按 `(created_at DESC,id DESC)`。**每一页**都必须在同一repeatable-read snapshot中执行：先读ledger/target-set/enrollment/route/read-model versions，对首页冻结这组值、对后续页与cursor逐项比对，再做keyset行查询，最后在同snapshot复读owner versions；不一致时首页整笔重试、后续页返回`409 ai_message_read_model_snapshot_changed`。禁止在READ COMMITTED中先比version、后查数据；返回 `as_of/target_set_hash/target_set_hash_version/targets[]/enrollment_epoch/route_epoch/read_model_version/next_cursor`。

cursor 使用服务端 HMAC，payload 必须签入 endpoint、tenant/task、ledger、`target_set_hash+version`、enrollment/route/read-model version、固定 order、最后排序键，以及完整 normalized query：obligations 的 `target_operation_target_id/state_class/blocker_code/account_id/limit`，attempts 的 `obligation_id/reason_code/account_id/limit`；未提供的 filter 也以显式 null 签名。后续页在上述同一snapshot内逐项比对 target-set/enrollment/route/read-model version和请求规范化结果，任一变化返回 `409 ai_message_read_model_snapshot_changed`，前端从第一页刷新；cursor 换 filter、limit、order、endpoint 或 Task 使用返回 typed 422，禁止静默混页。不存在任务/ledger 返回 404，非法 date/filter/cursor 返回 typed 422。DTO 在 `schemas/task_center.py` 显式定义，不使用 `Record<string, any>`；正文/prompt 只在既有审计权限下单独取。

### 8.3 Lifecycle 与既有管理命令

共享Task CAS的HTTP合同不可隐含：`TaskOut`/任务详情必须返回必填`task_version=Task.version`；generic与group-AI专用PATCH、start/pause/resume/stop/delete body统一要求`expected_task_version`。create-and-start内部只可使用create事务返回的version；成功返回新version，stale统一`409 task_version_conflict + current_task_version`且零业务写。前端保存/生命周期按钮提交当前详情version，409后刷新并要求重新确认；service不得先读最新Task再替调用方填expected version。retry/reset虽固定409也必须经过同一Task/enrollment解析，但不推进version。

UI 隐藏按钮不是安全边界。现有 `PATCH settings/start/pause/resume/stop/retry/reset/delete` 对 group AI Task 一律先走 §4.8 helper 的 `operation=lifecycle_control`；即使 enrollment/route 正在 preparing、blocked、paused、incompatible 或 route 缺失也不能绕过。helper 只授权该命令的管理 CAS，业务恢复后仍须独立通过 `operation=write`，并按下表执行：

generic `TaskUpdate/TaskSettingsUpdate` 与专用 `GroupAIChatConfig` PATCH 必须先归一为同一个 typed field-family decision；不能由两个 router各自解释：

| 字段族 | current route合同 |
| --- | --- |
| 展示 | `name/priority/target_title` 可用expected Task/config version直接CAS，不改变任何业务revision |
| target/ledger identity | `target_group_id/target_operation_target_id/target_input/target_type/timezone/daily_message_target` 只写next-ledger pending revision；current ledger/target-set/ordinal/due/deadline不改。server-owned `target_reference_revision`不可由客户端写 |
| Task排期/pacing | `scheduled_start`仅first ledger前可改，current route固定409；`scheduled_end/max_duration_hours`仅first ledger前直接改，运行后只能写next-ledger lifecycle schedule且不得缩短当前period/deadline；generic `pacing_config`、hourly weights、`hard_hourly_* / hourly_min_messages` 对current fact-first固定422 `ai_message_legacy_pacing_not_supported` |
| task-day quiet/ramp | `silent_mode/start/end/max_accounts/messages_per_round`、`ramp_up_minutes/ramp_start_ratio` 只写next-ledger schedule revision，不重算current due_at或移动既有Action |
| aggregate content | `messages_per_round_mode/messages_per_round/reply_min_per_round/participation_* / allow_account_repeat/repeat_cooldown/rule_set/material/act-type` 递增content-policy revision，只从下一 `AiGroupContentAllocationPlan`生效；当前plan vector/count不改，且不能改变stable obligation、20条硬上限或Task轮转 |
| unit generation/content | `topic_directions/teacher_targets/chat_history_depth/ai_model/system_prompt/slang/tone/language/max_message_length/account_personas/account_memory_depth/context_expire/fact_anchor/semantic_repeat/low_confidence` 只允许对无Action、无active Generation、无call-issued的未绑定unit按assignment守恒创建新intent revision；已绑定intent immutable |
| account/admission | `account_config/history_fetch_account_id/auto_join/group_bot_admission/verification/captcha/membership_max_concurrent` 写current scope/admission revision，按§7.1.1动态加入、等待或abandon；不清ordinal、hold、fact或其他账号义务 |
| 技术/退役控制 | `due_catch_up_pipeline_depth` 只影响未来materialization调度且受代码20上限/真实槽约束；`idle_continuation_*`、旧retry/reset/rewrite-on-reject语义在durable wake合同下拒绝，不能恢复固定轮询 |
| `failure_policy` | current route固定409 `ai_message_typed_failure_policy_required`；不得用`max_retries/retry_delay/backoff/on_content_rejected`覆盖typed obligation、3+3 epoch、deadline或retry/reset合同，告警配置走既有独立告警入口 |

上表所有业务字段必须映射到 §4.8 的持久domain revision owner：target/ledger identity、Task排期、quiet/ramp写`next_ledger_revision`；aggregate content写`content_plan_revision`；unit generation/content写`generation_policy_revision`；account/admission写`account_scope_revision`；展示字段只写Task.version。一个字段不得同时无理由推进多个域；确实跨域的字段在同一PATCH decision中显式列出全部changed domains。ledger/plan/intent/scope owner冻结各自revision+snapshot hash，API的`effective_revision`返回对应domain revision而不是笼统config revision。

混合字段请求必须在一个事务全部接受并返回每字段 `effective_scope=current_display|current_scope|new_intent|next_plan|next_ledger/effective_revision/effective_at`，或全部回滚；未知/retired/server-owned字段422，状态/epoch不允许的合法字段409。helper前schema与helper后业务拒绝都返回稳定reason code并写manage AuditLog（不含敏感prompt正文）。direct API、批量更新和UI保存使用同一决策器。

| 命令 | current route 行为 | pre-Gateway unit | call-issued/Gateway/remote fact | HTTP/审计 |
| --- | --- | --- | --- | --- |
| `PATCH task/settings/group-ai` | 名称/优先级等展示字段正常 CAS；timezone、手工 daily target 与 target-operation set 只建下一 ledger pending revision，当前 route 的 target-set hash 不变；账号范围按 §7.1.1 新 scope revision；prompt/content/admission 只影响未绑定 unit，安全撤销可类型化终结 pre-Gateway | 不清 stats、plan、fingerprint、ordinal 或历史；intent/variation 不原地改写 | immutable；只 reconcile | manage 权限、expected config/route version、AuditLog；冲突 409 |
| `start/resume` | 不得激活takeover的preparing/blocked route。first start/start-after-stop/natural-rollover start必须由既有TaskStartOperation外层owner原子调用helper；同period start-after-stop等待stop adoption=`ready|complete`，复用stop epoch把Task/route转running，不复活cancelled unit；period结束走natural-rollover。resume只接受paused+adoption ready/complete，复用pause epoch；first_start不要求不存在的adoption行 | paused unit按新clock重评估；start-after-stop仅为当前due创建更高ordinal的新unit，旧cancelled/旧Action保持终态；accepted variation仅在完整basis未漂移且同义务未cancelled时可复用；旧epoch行不得直接领取 | 继续同request reconcile；安全收口后触发adoption，旧call-issued不因start/resume换epoch | StartOperation/bootstrap/adoption/epoch/hash不匹配409；same start id只回读 |
| `pause` | 仅Task从非paused→paused的CAS winner在同事务写route=`paused`、lifecycle epoch+1、Adoption=`pending`；已paused的任意新command ID只回读，不再+1；不在控制事务无界扫描义务 | recovery 逐行 `lifecycle_adopt`：open/Action/已知-safe generation 收口到 current epoch waiting；`generation_reconcile` 保持旧 epoch同 job对账；prepared 必须审计无 call-issued 后才能 adopt；不创建新 ordinal | 已 call-issued 的原 owner可完成一次 committed invocation并永久保留旧 epoch只 reconcile；无 replacement | AuditLog + adopted/deferred/blocked typed counts |
| `stop` | 仅Task从非stopped→stopped的CAS winner在同事务写route=`stopped`、lifecycle epoch+1、Adoption=`pending`；已stopped的任意新command ID只回读；停止期间禁止新writer且不无界扫描义务 | recovery逐行lifecycle adopt，把未call-issued（含audited prepared）的current/future unit转`cancelled_by_task_lifecycle`并原子`active rank -> retired(task_lifecycle_stop)`；Generation unknown保留旧epoch同job reconcile，安全后再取消；未来显式start只为M中的空rank建新高ordinal active identity，不复活这些行。若直到deadline仍未start，空rank按target SLA归known shortfall | 已call-issued原owner可完成一次并保留旧epoch；hold/事实只reconcile且不得retire rank | AuditLog + adoption/rank-owner/conservation readback；再次start走TaskStartOperation |
| `retry/reset` | 禁止 generic Action resurrection、清 result、清 plan 或 force bootstrap | 不改变义务 | unknown/failed Action 不回 pending | `409 ai_message_contract_managed_recovery_required` |
| `delete` | 先执行 stop 语义并创建既有delete operation；Task已stopped时不得再次推进epoch | 同 stop；归档前保留allocation/义务/历史 | hold、pending/failed projection与open reconcile不是永久禁删条件；必须先完整归档其request/fact/binding/case identity到RemoteMutationTombstone+ContractTombstone，归档count/hash不完整才typed 409 | 接受后返回 `202 + operation_id`；只有operation=`committed`后Task才物理不存在，late result继续按logical request/tombstone只读reconcile |

合法恢复只有 durable wake、同 request safely-not-executed、fact projector、provider reconcile，或内部受审计的 `contract_error` CAS；不提供“全部重试/清空 unknown”。物理删除复用全局 `fencing→snapshot_committed→archiving→archive_verified→deleting→committed`：stop/adoption先fence新业务写；snapshot冻结hold/fact/binding/projection/reconcile集合；archiving把每项写入RemoteMutationTombstone与AiGroupMessageContractTombstone；只有count/hash逐项相等才archive_verified并按显式逆FK顺序删除可重建task-day执行表和Task。open unknown/reconcile可在Task删除后按logical request/tombstone继续只读收口，不阻断删除；只有无法形成完整归档或归档集合漂移才409。delete operation提交时把inventory item（如有）与enrollment转`retired`。FleetPolicy、LegacyInventoryItem、Enrollment、ContractTombstone、RemoteMutationTombstone和全局activation manifest不在删除清单且不得CASCADE；canonical fact只在其identity已归档且全局retention允许时清理，其逻辑task_id继续参与membership/state hash和E4。详细source-event可按retention删除，但最终digest/count必须留在ContractTombstone。

### 8.4 权限与隐私

- 任务义务、blocker 和聚合使用既有 `tasks.view`；人工恢复/面具重试继续要求现有 manage 权限；
- quantity conflict adjudication、projection poison resolution、generic blocker resolution和owner-aware contract reopen统一使用已在权限目录中的`system.manage`；permission middleware与handler/service双重校验，各操作使用独立approval ref、expected hashes/version与AuditLog；跨tenant对象404，同tenant无权限403，普通`tasks.manage|tasks.dispatch_control`不能代替；
- 默认诊断只返回 reason code、hash、reference ID 和计数，不返回完整生成 prompt、Provider 原文或敏感群消息；
- 详细正文沿用现有消息审计权限和脱敏策略；所有人工恢复写 AuditLog。

## 9. 监控与告警

新增/校正指标：

```text
ai_group_open_obligations
ai_group_action_bound_obligations
ai_group_quality_waiting_context
ai_group_content_capacity_gap
ai_group_waiting_dependency
ai_group_terminal_shortfall
ai_group_pre_gateway_attempts_total{reason}
ai_group_unique_obligations_affected{reason}
ai_group_variation_key_missing_total
ai_group_same_basis_rematerialization_total
ai_group_check_in_scope_conflict_total
ai_group_gateway_unknown_hold
ai_group_wake_pending / ai_group_wake_oldest_due_seconds
ai_group_fact_projection_pending / failed / oldest_lag_seconds
ai_group_route_blocked{reason}
ai_group_legacy_writer_increment_after_route_total
```

以下是零容忍不变量，不用经验阈值掩盖：

- normal coverage/extra-volume `content_variation_key` 为空；
- 同一 obligation + materialization version 存在两条非终态 Action；
- blocker basis 未变化却新增 Action；
- 同 scoped check-in 出现多个 open/Gateway/unknown/confirmed owner；
- Gateway unknown 产生 replacement send；
- gap/shortfall 增加 confirmed；
- remote fact 已提交但 projection 长期无 owner/heartbeat；
- route 存在后 legacy writer 新增 Action 或 Gateway journal。

成功率同时按 Action attempt 和 unique obligation 展示，避免 100 次相同失败把一个真实 blocker伪装成 100 个业务欠额。

## 10. 存量接管与数据迁移

### 10.1 原则

- additive schema、preview/apply/readback；不删除历史、不重置 Task、不重算成功为失败；
- 只从 TaskDayLedger、coverage ledger、Action/Attempt、Gateway evidence 与 typed remote fact 重建当前状态；`Task.stats` 仅作对照；
- 一条历史失败 Action 不对应一条新义务；按当前 due 欠额和 coverage 未完成事实建立唯一现存义务；
- 任何 Gateway-started/unknown 均先建 hold/reconcile 投影，绝不重发；
- takeover manifest 保存输入 SHA、Task/ledger、状态计数、冲突计数和结果 hash，不保存秘密或完整正文。

新增 `AiGroupObligationLegacyLink`（或等价 immutable alias）逐行记录旧 `(legacy_kind,legacy_id)` 到新 message obligation。基数是“多个 legacy failed Action/memory/旧 obligation identity 可折叠到一个新义务；每个 legacy identity、尤其每个 remote fact最多映射一个新义务”，因此对 legacy identity/fact建唯一约束，不对 new obligation id建通用唯一。quantity success唯一性由 §4.1 `AiGroupMessageQuantityFactBinding`承担，不在 canonical fact或 alias上拒绝第二真实事实。takeover 对彼此独立的历史 remote fact按 canonical order各分配 stable unit/ordinal；current合同中同 requested obligation 的第二真实 fact则保留为 unbound conflict，不能事后谎绑。旧 `FulfillmentRemoteFact`、message memory、Gateway journal、Action result和Attempt均不改写；projector通过 alias/binding完成新 typed read model。旧空 variation只导入为 `legacy_import/quality_rejected`审计，不能伪装成合法新 key。

### 10.2 check-in 历史 additive claim

preview 枚举当前任务日 legacy `mask_missing_check_in/check_in_fallback/due_catch_up_check_in` 的 open、Gateway、unknown、success：

1. 每个 scoped key 选择权威 owner：remote success 优先，其次 unknown/Gateway，再次唯一 open；
2. 新增 `AiGroupCheckInScopeClaim` 指向该 legacy owner，并以 legacy link 补 task-day/coverage/trigger 解释；旧 terminal/Gateway memory、Action、Attempt、fact 一字不改；
3. 同 scope 多个历史 identity 全部保留并链接同一 claim/新义务，非 owner 的 `legacy_scope_conflict` 只写在 alias classification，不回写旧记录；
4. 发现无法区分的多 unknown/多远端事实时停止该 scope activation：claim 保持 `unknown`，route/scope activation 标记 `blocked`，只读对账；
5. 其他 Task/群的 check-in 产生不同 scoped claim，不占当前 scope。

message memory partial unique 只约束切换后的新 `content_source=check_in` 行；迁移不把 legacy source 批量改名。apply 后同时核验 scope claim 唯一、新 memory partial unique 和每个 legacy fact 唯一 alias。

### 10.3 当前任务接管

inventory builder与每次cutover都必须冻结/重读 `Task.status + Task.version + lifecycle/config/route epochs + current ledger period + legacy Action/Attempt/fact counts`，并只接受下列互斥 takeover class；status/ledger组合不命中任何一行时写typed blocker，禁止猜测：

| takeover class / operation | 合法源事实 | 最终Task/enrollment/route | 是否可能开始发送 |
| --- | --- | --- | --- |
| `never_started / enroll_never_started` | `draft`、API/schema仍允许的pre-start `pending`调度别名，或**无历史start的`stopped`**；且从未有ledger、Action、Attempt、fact或legacy route | 单事务item `open->enrolled`、enrollment=`active`，精确保留源Task状态，current_route/ledger=0；零集合manifest/hash | 否；draft/pending以后由scheduled/manual first-start，stopped以后由显式TaskStartOperation start-after-stop/first-ledger bootstrap |
| `same_period_running / takeover_activate_running` | `running`且current ledger满足`database_now < deadline_at` | cutover暂时pause并产生epoch `E`；最终Task=`running`、enrollment/route=`active+running`，复用`E` | 是；仅此类可作立即发送canary |
| `same_period_paused / takeover_activate_paused` | `paused`且current ledger满足`database_now < deadline_at` | 不伪造第二次pause、不推进epoch；最终Task仍paused、route=`active+paused`，同tx建current epoch `imported_baseline=complete` | 否；pre-call unit只导入waiting，无current Action |
| `same_period_stopped / takeover_activate_stopped` | `stopped`且current ledger满足`database_now < deadline_at` | Task仍stopped、route=`active+stopped`；pre-call unit按stop合同cancelled，hold仅reconcile；同tx建current epoch imported baseline | 否；以后显式TaskStartOperation start-after-stop |
| `settling_closed / takeover_activate_closed` | `target_reached|wrapping_up`有可守恒ledger；或`running|paused|stopped`的latest ledger已到deadline但settlement/projection barrier尚未completed | Task状态原样、enrollment active、latest route=`active+closed`、settlement activation-ready；仅hold/fact/projector/settlement reconcile，running也不得在结算前发送 | 否；settlement完成后再按状态选择automatic/resume/start bootstrap |
| `rollover_eligible / enroll_rollover_eligible` | `running|paused|stopped`的prior ledger已到deadline且immutable settlement completed、manifest证明没有next-period ledger/route | enrollment active；prior route只建/保持`active+closed`并守恒，Task状态原样；同tx建current epoch imported baseline，不提前建新period ledger | 否；running由automatic bootstrap，paused由resume，stopped由TaskStartOperation start-after-stop |
| `terminal_retired / retire_terminal` | `failed|completed|deleted`，或经审批明确放弃的其他旧Task | 完成remote/contract tombstone与守恒readback后item/enrollment=`retired`，Task状态原样；不建active route | 否；generic retry/reset/start均拒绝 |

`enroll_never_started` 必须在一个事务匹配zero-ledger/zero-legacy-owner hash、item/Task/allowed-status+epoch versions后直接active，不能先造空ledger。`retire_terminal`要求独立approval与archive hash，不能把failed静默当completed。paused/stopped/settling/rollover激活必须在同一最终CAS写唯一`imported_baseline=complete`；baseline hash包含pre-call收口、deferred call-issued集合和source epoch，缺失即不允许resume/start/bootstrap。expired ledger以settlement是否completed把`settling_closed`与`rollover_eligible`互斥；running expired不会因worker尚未rollover而落入非法组合。paused/stopped/closed激活都必须有各自manifest/readback和expected source status CAS，禁止复用running final CAS后再另调pause/stop。状态在preview后变化即manifest stale；新class必须重做preview。pending但已有ledger、running无任何latest ledger/可证bootstrap lineage、draft却有Action、failed/completed仍有未归档hold等不一致组合一律blocked。

对“西安天上人间”当前running任务日先做只读粗 preview；随后在同一事务把 inventory item `open -> enrolled`、创建 enrollment/route=`preparing`，并将 Task `running -> paused`、lifecycle epoch推进一次为 `E`，同时 fence 该 Task 当前及未来任务日的新旧 Planner、Generation claim 与新 Gateway call。后续 manifest 与所有导入 owner 都冻结 `E`，最终只由 `takeover_activate_running` 复用。其他有ledger class走同一quiescence/manifest/apply框架，但从头冻结各自Task状态，paused/stopped/closed不得创建current pre-Gateway Action。quiescence 条件是：没有可继续领取的 legacy/new claim、没有未落库的 pre-call lease、所有 pre-Gateway worker 已退出或按 frozen owner/version 收口；已提交 call-issued 的 Attempt 和 remote-fact projector 允许继续 `operation=reconcile`，不阻塞为“无在途”，但必须进入最终 manifest 的 hold/fact 集。

接管不能依赖无状态脚本或route上的单一hash。新增持久 `AiGroupTakeoverOperation`，以`inventory_item_id`数据库唯一，保存逻辑task、takeover class、`current_request_revision`、state=`previewed|fenced|quiescing|manifested|applying|readback|blocked|activated|retired`、preview hash、current/final manifest ID、source-fence ID、expected policy/item/Task/enrollment/ledger/route versions、chunk cursor、expected/applied/readback counts+hashes、next_retry_at、lease owner/epoch/expires_at与version；同一item的新preview只可在无已激活结果时以request revision审计supersede，已activated/retired结果永久只读。每个请求版本另有append-only request row，冻结调用者、审批、候选SHA/migration、rollback capability、expected versions与payload hash。

最终事实集由 immutable `AiGroupTakeoverManifest(operation_id,manifest_revision,as_of,takeover_class,target_set_hash,a_static_revision_vector_hash,source_fence_version,source_event_set_hash,reconcile_delta_hash,eligibility_delta_hash,legacy/canonical/current identity-set counts+hashes,manifest_hash)` 与 `AiGroupTakeoverManifestItem(manifest_id,item_seq,source_kind,source_identity,target_operation_target_id nullable,classification,canonical_order_key,payload_hash)`持有；分别唯一`(operation_id,manifest_revision)`、`(manifest_id,item_seq)`及`(manifest_id,source_kind,source_identity)`。分块进度由 `AiGroupTakeoverChunkCheckpoint(operation_id,manifest_id,chunk_seq,first_item_seq,last_item_seq,input_hash,output_hash,state=pending|processing|completed|blocked,lease/version)`唯一持有。claim只按`AiGroupTakeoverOperation(next_retry_at,id) WHERE state IN ('quiescing','manifested','applying','readback')`和checkpoint item range keyset领取；preview/apply/readback/activate API或CLI都必须提交expected operation/version/manifest hash，不能拿本地文件或route hash推断续点。

quiescence 后在一个可重复读取的数据库 `as_of` 生成最终 immutable manifest，覆盖该 TaskDayLedger 的完整 target-operation set，并冻结 target-set hash、deployed SHA、migration revision、Task/lifecycle/route、ledger/各 target/config/pacing revision、Provider model/config/policy revision、`a_static_revision_vector_hash`、takeover source fence event/delta version+hash、分域 source observation、行数与输入 hash。source observation 必须是 tenant+Task+ledger+target/account/message scope 的版本或 keyset 上界，禁止使用会被无关流量推进的全表 watermark。迁移源分两类：

凡takeover class带现存TaskDayLedger，manifest必须同时冻结唯一SettlementOperation identity、deadline、target-set hash/version和完整TargetItem key集。apply幂等创建`activation_ready=false,state=pending,next_retry_at=NULL`的operation与每target item，并纳入chunk hash/readback/conservation；无ledger的never-started/retired class不创建。deadline已过的存量ledger在preparing期仍只做守恒导入，不由settlement worker领取；最终分类activation CAS同事务把operation设`activation_ready=true,next_retry_at=max(deadline_at,database_now)`，激活后立即可drain。operation/item缺行、多行、target-set/hash不等或已有冲突结果都阻断activation；不得在激活后靠recovery猜测补建。

- A 类 `identity/conservation`：Task/ledger/target/pacing/config-policy、legacy Action/Attempt/Generation request、Gateway journal/call-issued、quantity fact/binding、coverage owner/claim及其scoped alias。高频scoped事实由source fence覆盖，有限权威revision由static vector覆盖；除manifest已列identity的同request hold→result/fact、pending fact→projection等守恒单调delta外，行新增、identity/owner/数量/版本漂移均阻断并重做preview；Provider model/config/policy revision也属于A类。
- B 类 `liveness/dedupe`：群 context revision、profile usable revision、account online/session/admission revision、Provider health revision、跨 Task message-memory/dedupe scope revision。它们允许在 preparing/apply 期间单调推进，不使 manifest 整体失效；每条 imported pre-Gateway owner 在最终 readback 按最新 B 类版本重评估。仍合法才保持 binding；已淘汰则只新增 alias/assignment reclassification，把原 stable obligation/ordinal 转对应 waiting/ineligible，绝不修改旧 Action、重排 alias/ordinal、另建 replacement 或把 terminal history 复活。Provider 恢复、账号重新 online 等变好事实只决定义务为 open/waiting，后续仍由 current writer 重新物化，不直接复活 legacy row。

B 类观察值与逐行判定写入 `eligibility_delta_hash`。activation CAS 不把 B 类当静态全局 fence；active 后 Planner/Generation/Gateway Tx A/Tx B 仍须通过同一实时 liveness/dedupe 校验，Action 冻结 observed revision，任何后续漂移在外调前转同一义务 waiting/ineligible，不能用 manifest 时点资格绕过当前事实。权威分类和稳定排序固定为：

在分类前先冻结legacy→target-operation映射：legacy Action/fact/hold/coverage有结构化`target_operation_target_id`且该target属于manifest target-set、其group与legacy group一致时优先采用；否则只允许在manifest内按legacy group筛选后候选恰好为1时映射该唯一target。候选为0、候选多于1、结构化target不在set或target/group冲突时，route=`blocked`，manifest逐identity记录`mapping_reason/candidate_target_ids/source_fields_hash`，禁止按标题、数组顺序、最早ID或canonical排序猜归属。current create/PATCH仍按完整target-set合同校验；即使正常group-AI配置只有单target，迁移脏数据也必须走此规则。

| 优先级 | legacy 事实 | 新投影 | canonical sort key |
| ---: | --- | --- | --- |
| 1 | typed `remote_message_observed` | confirmed obligation；每个 fact 一个 unit | `(coalesce(fact.observed_at,fact.created_at),fact_id)` |
| 2 | committed call-issued / Gateway started / unknown，无 success fact | `gateway_started_hold` 或 `gateway_unknown_hold` | `(coalesce(attempt.call_issued_at,attempt.gateway_call_started_at,attempt.before_call_at,journal.observed_at,attempt.created_at,action.created_at),journal_id,attempt_id,action_id)` |
| 3 | Provider request/persist unknown、无 Telegram call-issued | 新建 current generation-reconcile wrapper，冻结同 provider request identity且只查询；旧 Job/Action仅 alias | `(coalesce(job.request_started_at,job.created_at,action.created_at),generation_job_id,action_id)` |
| 4 | 合法 pre-Gateway Action | 复用经验证内容新建唯一 current materialization/Action；旧 Action/memory仅 alias且永不执行 | `(action.scheduled_at NULLS LAST,action.created_at,action_id)` |
| 5 | coverage ledger 无以上 owner | confirmed/open/waiting 由 scoped fact/claim/profile/admission 决定 | `(coverage_ledger_id,account_id)` |
| 6 | terminal failed/skipped 或无 current 身份的 generated-ready Action | 仅 immutable legacy alias/audit，不占新 unit | `(coalesce(action.executed_at,attempt.after_call_at,attempt.created_at,action.created_at),action_id)` |

以上所有 timestamp 在 SQL/manifest serializer 中统一转 UTC，保留微秒；ID tie-break 按规范化小写文本的 bytewise/C collation，禁止依赖 locale。`call_issued_at`、`request_started_at` 是 current 行 additive 字段；takeover 对 legacy 行只计算并把 `canonical_order_at/canonical_order_source` 写入 manifest/additive alias，不回填 Attempt、GenerationJob、Action 或 journal。任一候选缺表关联时仍按明确 coalesce 链和最终现有 ID 排序；重跑必须得到相同 canonical key、ordinal 与 manifest hash。

`canonical_order_at`只是导入排序证据，永远不能被复用为`remote_effect_at`。legacy fact/binding/coverage的timeliness映射只有三类：旧远端事件本身含可验证的Telegram/adapter effect time时写`remote_event_time`；只有旧Attempt成功回执、Gateway成功journal与fact能以同一request及同一原子提交证据证明时，才可写`same_attempt_atomic_gateway_success`与该提交时间；其他全部`remote_effect_at=NULL,confirmation_time_basis=unproven,timeliness=unproven`。旧fact `created_at/observed_at/projected_at/reconcile_at`、Action executed time或导入时间都不能推断on-time。manifest分别记录三类count、identity-set hash与evidence reference，readback与QA逐fact复算；无证据只能unproven，不得为通过settlement而降低标准。

manifest 在计算缺口前必须把每个已按 canonical source order 分配 ordinal、计算 frozen due_at 的 stable unit 归入且只归入一个互斥集合，按上表高优先级吞并同 identity 的低优先级 legacy alias：`confirmed`、`gateway_hold`、`pre_call_active`（含 generation reconcile/合法 Action）、`open`、`waiting`、`terminal_shortfall`、`future`、`cancelled`。coverage 只是 unit 的 subtype/coverage alias，不是第九个计数集合；同一 coverage owner 已在 confirmed/hold/pre-call/open/waiting 中出现时绝不能再加一。历史 terminal failed Action 本身只做 alias，只有已存在稳定 due unit 且 deadline合同成立时才进入 terminal_shortfall。`future` 定义为冻结 due_at > manifest as_of，`cancelled` 为已按 target/scope/lifecycle合同终结且无 remote boundary证据；二者都不占当前 due。

“合法 pre-Gateway”必须同时满足：没有 call-issued；legacy Action/Generation/memory owner 唯一；账号、reply/material 引用仍存在；normal accepted content有非空稳定 variation key、可重建的 current intent snapshot与通过的质量证据；确定性 check-in则必须有可接管的 scoped claim/memory reservation。它们也绝不原地变成 current owner：apply创建新的 current assignment/intent、imported accepted variation+memory（check-in改为CAS claim owner并建新 current memory，不造variation）和唯一 current Action，结构化冻结 enrollment/route/lifecycle/manifest、全新 current Action/request identity及 legacy source link；对 dedupe重评估时只排除其被 alias的同一未发送 legacy reservation，不排除其他外部 memory。旧 pending Action/status/payload/result/memory逐列 hash不变，只由 legacy route fence永久禁止执行。

Provider request/persist unknown 同样不复用旧 GenerationJob行：apply 新建 current `generation_reconcile` wrapper，保存 `legacy_generation_job_id`、完全相同 provider request identity与 current fence，只允许 query/reconcile，不发新 request；旧 Job/Action仅 alias。Gateway hold/unknown使用 immutable legacy Attempt/journal/fact alias作为 reconcile evidence，不计 active Action，也不新建可调用 owner。generated-ready但 variation为空、request/intent不完整或 policy不兼容的旧 Action只标 `legacy_ineligible_pre_gateway`；同一 stable obligation依新合同 open/waiting。327条 check-in与258条 duplicate terminal历史因此不逐条变成业务欠额。

conservation 中 `pre_call_active` 只统计上述新 current Action或current generation-reconcile wrapper；legacy pending/generating 行无论原 status为何都不进 active backlog。每个 obligation仍受 current Action/GenerationJob partial unique；apply chunk必须同时证明旧 alias恰有一个、新 current owner至多一个、旧 route dispatch增量为0。route activation后 Dispatcher只接受带当前结构化 fence的 current Action，任何 legacy Action id直接 typed拒绝。

manifest 按上述优先级/排序为全部已识别 stable unit 分配不重复 ordinal；互斥 due 集合记为 `D={confirmed,gateway_hold,pre_call_active,open,waiting,terminal_shortfall}`，只计 `due_at <= as_of` 且非 cancelled 的 unit。takeover 中每个历史 remote fact各分配独立 unit/bound binding；若发现同 current obligation 的 unbound conflict则直接阻断而不进入正常构造。令 `boundary_count=|confirmed|+|gateway_hold|`、`planned_boundary_overage=max(boundary_count-due_by_now,0)`，则：

```text
existing_due_non_cancelled = sum(|state ∩ due| for state in D)
required_due_cardinality = due_by_now + planned_boundary_overage
missing_due_units = max(required_due_cardinality - existing_due_non_cancelled, 0)
```

只为规范active-rank空间中的`missing_due_units`创建缺少的extra-volume unit，并为每行同时分配规范rank与单调quantity identity；coverage owner已是任一集合成员时不再创建数量unit。future/cancelled不进入公式，但保留stable identity/ordinal/rank历史审计；若existing due集合超过required且超出部分无remote boundary，按target revision合同把最高安全rank owner转future/`due_rank_state=retired`，有boundary则转`protected_overage`，不能用负missing隐藏。每个unit的due_at使用§4.1 rank函数和manifest pacing snapshot计算一次；`next_quantity_ordinal=max(all assigned ordinal)+1`，即使observed success超过目标也不复用或折叠。近期duplicate references只初始化对应义务forbidden set，不产生额外unit。所有旧fact/action/journal/memory字段逐列hash保持不变。

apply 只能由上述operation按 manifest item key分块提交，route 在全部 checkpoint 完成前始终 `preparing`。每一 chunk 先以expected operation/version、manifest hash、item range、Task lifecycle/route/target version CAS领取checkpoint，再幂等写 additive rows并把output count/hash回写同一checkpoint；崩溃后只续同一operation+manifest+未完成range，不重新 preview 或分配新 ordinal。preparing 后已禁止新 call-issued；manifest A 类只允许已列 identity 的守恒单调变化，以 `reconcile_delta_hash` 将同一 ordinal 的 hold→result/fact 或 pending fact→projection，不改变排序、identity 或数量，其他 A 类漂移一律 blocked。B 类版本推进写 `eligibility_delta_hash` 并按上段逐行重评估，不阻断无关 chunk，也不得改变 due-unit identity、ordinal 或成功事实归属。

最后 readback 重算operation声明的全部checkpoint count/output hash、A类static vector/event fence/reconcile delta、B类eligibility delta、legacy alias唯一、remote fact request identity唯一、quantity binding bound-obligation partial unique/unbound conflict、ordinal/due、current active owner、coverage和Gateway mutation identity；同时证明每个仍绑定的pre-Gateway owner已用最新scoped B类revision重评估，并把最终A fence/vector写入operation readback hash。冲突或缺行把operation与route标`blocked`；running cutover的Task保持临时paused，其他class保持其冻结源状态，全部禁止active或legacy fallback。若非允许delta的A类源事实确实变化，新manifest revision必须引用已写stable due-unit/alias IDs并仅追加/重分类additive rows，旧manifest标superseded留档；不得删除旧行、重分已占ordinal或把旧Action改写成新历史。activation最终事务同时CAS operation=`activated|retired`，没有completed checkpoint/readback恒等式不得只改route。

manifest 必须证明以下守恒后才能激活：

```text
due_by_now + planned_boundary_overage =
  bound confirmed obligations
  + Gateway started / unknown holds
  + open / action-bound obligations
  + waiting obligations
  + terminal shortfall

raw observed remote facts =
  bound quantity fact bindings
  + unbound obligation conflicts
```

其中 `planned_boundary_overage=max(bound_confirmed+holds-due_by_now,0)` 与 `unbound_obligation_conflict` 分栏展示，都不能伪装无冲突目标完成；future units 与 `cancelled_by_task_lifecycle` 分别守恒，不进入当前 due等式。并分别证明 coverage confirmed、未完成coverage、active current Action、Gateway mutation identity与quantity binding对应。守恒或 remote-fact binding/alias冲突时，不激活当前任务日；route/runtime写 `contract_migration_blocked`，Task保持§10.3该class的冻结/临时fence状态，不能自动回到legacy Action-only writer。冲突排除后只允许使用同一manifest/hash重试；源事实已变化则废弃旧manifest并重新从preparing状态生成新preview，留完整审计。

## 11. 发布顺序与 Release Gate

1. PRD、专项设计和 dataflow 索引评审通过，独立复核无 P0/P1；
2. dev 从合并时最新 `master` 新建干净工作树 resync；本设计分支已落后共享 worker/Gateway/部署代码，不得直接编码；
3. 实现 additive migration、typed model/worker/control/API/UI、takeover/E4 工具和测试；QA 通过 pure unit、真 PostgreSQL 并发/迁移/deadlock、前端与回归分区；
4. 先部署 additive schema 与 fence-compatible 候选 SHA 到 API、Planner、Generation、Dispatcher、recovery/projector，以及§4.5全部dependency producer实例（`worker-listener`、`worker-voice-profile`、`worker-account-online`、`worker-account-security`、`worker-ai-memory`、独立`worker-material-cache`、全部planner/generation/dispatcher/recovery与material/API写路径）；此时不得由 Alembic 建 policy/item，且 enrollment/route 必须为0。所有业务writer明示 capability=`ai_message_enrollment_fence_v1`；所有依赖producer另明示`ai_message_dependency_producer_v1`及writer-family bitset。实例集合必须从合并时实际compose/runtime manifest生成，不使用过时手写列表；逐实例核对SHA、migration、role、heartbeat/capability，停止旧producer并证明旧SHA进程/lease为0，完成受控source clock/outbox→consumer readback；
5. 在创建任何 fleet policy/enrollment 前，将上一步已验证 SHA/role/producer 集冻结为 rollback baseline。受保护 deploy/rollback workflow 必须先查询 policy/enrollment/route count 与两套 capability matrix：一旦 fleet policy 已存在或任一 enrollment/route 存在，任何 API/lifecycle/Planner/Generation/Dispatcher/Gateway/recovery role 缺 `ai_message_enrollment_fence_v1`，或§4.5任一producer实例缺`ai_message_dependency_producer_v1`，都拒绝部署；不得把不读新fence/不写wake事实的旧SHA当回滚目标；
6. 显式运行受保护 `ai_message_inventory_bootstrap`：CAS 创建 policy=`inventory_status=building` 后确认全部 legacy writer 已 fence；冻结 cutoff，以 keyset 重复扫描并写 inventory item；等待 cutoff 前 Task create/lifecycle operation barrier；核对 cutoff 范围零漏项、零多项、allowed epoch 与 Task/TaskContractRoute 一致；归档 membership hash/item count/state+epoch hash 后 CAS `building -> sealed, legacy_state=allowed`。任一步失败保持 building/fenced，不进入 preview；Alembic 不承担本步骤；
7. sealed inventory readback 通过并解除 legacy fence 后，生产只读粗 preview，确认 ledger/target、check-in scope conflict、Gateway unknown、remote fact 与 projection lag；
8. 受保护 cutover workflow 以目标 Task、§10.3 takeover class/source status、可选ledger/target-set hash、候选SHA、migration revision、preview hash、inventory item/version、rollback baseline capability hash和审批reference为输入：running类原子创建enrollment+route=`preparing`并Task paused；paused/stopped/closed类保持Task状态并创建preparing route；never-started直接走零集合enroll、rollover类不提前建新ledger、terminal类归档retire。任一分类一旦写item/enrollment即永久fence该Task legacy writer；
9. 等待 §10.3 quiescence，由同一`AiGroupTakeoverOperation`生成最终 as-of immutable manifest，并按manifest item range写ChunkCheckpoint后分块 apply additive takeover；
10. operation声明的全部checkpoint与conservation/readback artifact通过后，只能通过§10.3对应activation operation执行最终事务与A类static/source-fence CAS。running类才CAS Task `paused -> running`、enrollment/route `preparing -> active+running`并复用cutover epoch；paused/stopped/closed分别保持Task状态并active对应writer_state；never-started/rollover/terminal按各自无current route/closed/retired合同收口。全部匹配Task/version、可选TaskDayLedger、dispatch/task/task-day route epoch、inventory item、target-set/target version、operation/version、manifest/checkpoint/readback hash，并同事务把operation写`activated|retired`；A类变化或任一CAS失败全部不变。只有running提交后Planner可见，其他class禁止另调generic lifecycle命令；
11. “西安天上人间”以`same_period_running`完成canary产品验收与生产E4后，按冻结legacy inventory逐Task先分类再执行对应preparing/quiescence/manifest/readback/activation；paused/stopped/draft接管本身必须验证发送增量为0。单Task blocked隔离并显式处置，不得跳过后宣称fleet完成；
12. 所有 inventory item 均已 `enrolled|retired` 后，按Task生命周期分类完成fleet readback：never-started为`active enrollment + first_task_day_ledger_id=NULL + route/legacy owner=0 + first_start capability pass`；same-period running/paused/stopped为保持原业务状态的active route；rollover-eligible为active enrollment+closed prior route/无新ledger；target_reached/wrapping_up为active+closed；failed/completed/deleted只能有审批retire+tombstone，不得generic retry/reset。满足后先以membership/state+allowed-status/epoch hash CAS tenant fleet policy `allowed -> preparing`阻断漏网legacy start/resume，再核对open item=0及上述分类hash并CAS `preparing -> disabled`；不得改写 Dispatcher `DispatchClaimScope.active_contract_version`；此后 missing enrollment fail-closed；
13. 全局切换后观察 E4 窗口通过才写整体 `production_fixed`。

禁止先 active 再导入。已 call-issued/unknown 继续只读对账，合法 executing 按冻结合同收口；takeover 失败保持 preparing/blocked+paused。禁止新旧 writer 双写，也禁止因 takeover 失败自动回退到 Action-only 路径。

切换 CAS 使用 `TaskDayLedger + task_lifecycle_epoch + enrollment_epoch + AiGroupMessageContractRoute.route_epoch + target_set_hash + manifest_hash`。没有 enrollment 的未切换 Task 仅在 tenant fleet policy=`sealed+allowed`、inventory item=`open` 且 allowed epochs/现有 TaskContractRoute 均匹配时可继续其原 legacy route；inventory building、item 缺失/非 open、policy=`preparing|disabled` 后 missing enrollment fail-closed。enrollment 已创建的 Task 无论应用回滚、当前 route 缺失或跨日都不能重新取得 legacy Gateway 权限。

Release Gate 必须归档：候选/实际 deployed SHA、migration before/after revision、所有生产 role image/current symlink、逐 role `ai_message_enrollment_fence_v1` 与逐producer实例 `ai_message_dependency_producer_v1`/writer-family capability matrix、旧producer零进程/lease及受控wake readback、rollback baseline SHA/capability hash、policy/enrollment/route preflight、inventory building fence/cutoff/inflight barrier/seal CAS、inventory status/membership hash/item count/state+allowed-status/epoch hash、逐Task takeover class/source/final status、TakeoverOperation/request revision/粗preview/final manifest identities+hash、审批 reference、fence/quiescence 证据、每个ChunkCheckpoint input/output hash、conservation/readback、operation+activation CAS result、rollback decision 和 E4 artifact。任一 SHA/epoch/hash/capability 不一致即停止，不允许人工跳步或用 `/api/health` 代替业务证据。

fleet artifact 另保存 tenant policy epoch/inventory_status、cutoff、membership/state+allowed-status/epoch hash、item open/enrolled/retired counts、逐Task `never_started|same_period_running|same_period_paused|same_period_stopped|rollover_eligible|settling_closed|terminal_retired`分类及其 enrollment/route/bootstrap/tombstone/readback 状态、blocked/retired处置、fleet policy preparing/disabled CAS和切换后legacy Action/Gateway journal零增量。canary pass只能写`canary_production_fixed`，不能替代fleet。

## 12. 回滚

- fleet policy 创建前：additive schema 保留，可回滚到旧 SHA；但必须先证明 policy/enrollment/route count 全为 0；
- fleet policy=`building` 起（即使尚无 enrollment）：进入 fence forward-only 边界；应用只能回滚到 Release Gate 冻结且所有相关 role 都具备 `ai_message_enrollment_fence_v1` 的 baseline SHA，受保护 workflow 对不兼容 SHA hard fail；Task paused 或 inventory building 不能替代此能力门；
- preparing 阶段：保持 Task paused 与 enrollment/route preparing/blocked，可回滚到 fence-compatible baseline，但不能删除 enrollment或恢复 legacy writer；同一 manifest 可由兼容版本续 apply/readback；
- activation 后：若 fence-compatible 版本认识 route 但不支持其 contract version，Task 保持现有 `paused`，route `writer_state=incompatible`、runtime blocker=`paused_contract_incompatible`；不得部署完全不理解 Enrollment/Route 的旧版本，也不得让 legacy writer 忽略新义务继续发；
- 已进入 Gateway 的 Action 继续由兼容 reconciler 收口，unknown 只对账；
- 回滚不删除 obligation/intent/variation/wake，不回写 legacy ContentMix，不清空 message memory；
- 修复版本重新部署后按原 contract epoch/readback 恢复，不创建第二套当日义务。

## 13. QA 验收矩阵

### 13.1 单元与数据库并发

1. 账号当天在其他 Task/群 check-in success，不阻断当前 scoped check-in。
2. 同 scoped key 并发 reservation 只有一个成功；open/Gateway/unknown/success 均防重。
3. check-in 不进入 normal 10 天 exact/semantic/template 查询；normal 正文仍跨 Task/群按同账号阻断。
4. 两个不同 Planner decision 并发读取同一缺口时，target CAS 分配不同 quantity ordinal；相同 due-unit key 最多一条 obligation。
5. normal body在accepted variation+memory ready前Action数必须为0，只有Generation以唯一job/variation/memory CAS创建至多一条ready Action；deterministic check-in仅由Planner在scoped claim+memory+intent同事务完整时创建ready Action；takeover apply只允许final manifest导入例外。coverage与extra-volume normal Action均有稳定obligation和非空variation key。
6. event 在 subscribe 前、并发、提交后发生，以及乱序/重复送达时都不丢唤醒且只 CAS open 一次；真 PostgreSQL 强制三事务交错 `Tsub已锁obligation且clock fence未提交 -> Tprod event -> Tdrain clear`，证明 producer/drain 等订阅提交或令订阅CAS失败，不能清掉不可见旧版本订阅；drain领取/clear clock的短事务不得在持clock锁时反锁obligation。
7. 未绑定 extra-volume 订阅 task-day aggregate capacity clock；任一其他合格账号 profile/coverage/online/admission 变化可唤醒。
8. 相同 blocker basis 连续 Planner rotation 不新增 Action/GenerationJob；context/mask/policy/dedupe event 只唤醒一次。
9. duplicate reject 原子终结当前 variation；相同 external basis 不重置 3+3，只有明确 basis version 变化才创建新 epoch。
10. concurrent Planner/Generation 受 obligation、Action、GenerationJob、variation唯一约束收敛；normal body只有Generation是Action writer，Planner check-in与takeover import分支分别受scoped claim/manifest唯一owner约束，任何分支都不得产生pending/空正文Action或双写。
11. Gateway crash injection 覆盖 Tx A 前/后、Tx B call-issued 前/后、Telegram call 前/后、Tx C 前/后：无 call-issued 才能 prepared-not-issued 安全恢复；call-issued 后一律 hold，只有同 request typed safely-not-executed 可释放，started 永不重开。
12. Provider response/persist unknown 进入 generation reconcile，不混入 Telegram unknown，也不新建义务。
13. capacity gap/shortfall 不增加 confirmed、不退出 due 欠额，且 active Action/GenerationJob 为 0。
14. aggregate allocation plan 跨多个 20 条技术批次、replacement 和 extra→coverage 后仍守住 reply/material/act-type minimum/assignment unique；非法降级 fail-closed。
15. 动态 coverage 加入按 remote fact、extra-volume 转换、新增 target revision 三段收敛；已 Gateway/unknown/confirmed unit 不改绑。
16. current target必须绑定TaskDayLedger+target operation；真PG迁移后legacy null-target row、同local date两个current ledger row可共存，各partial unique只约束自身域；takeover新row link旧row且旧hash不变。跨时区/夏令时/partial-start只按ledger与冻结pacing计算ordinal due_at，target revision不改旧due_at。
17. 同 obligation 两个不同 request 的 remote success均先 append canonical fact；quantity binding并发时仅一条 `bound`，另一条为 `unbound_obligation_conflict`、observed overage并阻断 route。两事实都可查，第二条不确认coverage/其他义务，也不被约束回滚或折叠。
18. remote fact 提交后每个 projector crash point均可由 recovery lease 重放；target/coverage/wake 同事务，projection failed 不生成 replacement。
19. global claim scope、TaskContractRoute、task-day route 和 lifecycle/manifest epoch 任一不匹配时，Planner/Generation/新 Gateway 均 fail-closed；reconcile 仍可收口同 frozen request。
20. generic retry/reset 对 current route 返回 typed 409，unknown/failed Action 不回 pending；pause/resume/stop/delete/PATCH 按 §8.3 守恒且有 AuditLog。
21. wake producer inventory 的每个 writer 都做 event-before-subscribe、事务回滚与 worker crash replay；漏掉任一 producer 的契约测试失败。
22. Provider 无 request-query 时 unknown 消耗原 round并按同 epoch收口，不创建新 job/义务；健康恢复不重置 generation epoch。
23. Planner vs wake、Generation vs policy update、Dispatcher vs pause/stop、projector vs deadline、takeover vs reconcile 按 §7.5 在真 PostgreSQL 无 deadlock/lost update。
24. active enrollment 跨 TaskDayLedger rollover 时与新 ledger 同事务创建 current route；注入 route insert crash 后 ledger 不出现半提交，故意缺 route 时全 writer blocked 且 legacy 增量为 0。
25. 新建 current group-AI Task 与 active enrollment 同事务；失败整体回滚，首次 ledger/route 同事务，复制 Task 不能进入 legacy。
26. tenant fleet policy=`sealed+allowed`且inventory item=`open`/allowed epochs匹配时legacy Task才可继续；`allowed -> preparing -> disabled` CAS要求所有item enrolled/retired，并按never-started（enrollment active但零ledger/route/legacy Action）、同periodrunning/paused/stopped、rollover-eligible、retired逐类readback；合法draft不提前建ledger，stopped仍按start-after-stop能力验收。building/preparing/disabled或missing/non-open item下legacy start/resume/Planner/Gateway全部fail-closed；Dispatcher runtime contract字段保持不变。
27. dirty wake clock 在 subscription-fence/producer/consumer/clear-dirty 并发、worker 崩溃续 lease、同 key 连续多版本和冷 key 排队时均扫到 `observed_version < claimed_version` 的全部已提交订阅；row-version CAS 冲突可重试，新事件使 clock 保持 dirty，不做全表 subscription join。
28. assignment reclassification 与两个 Generation worker 并发创建 intent 时，plan/obligation CAS 只留下一个 current pointer；intent revision/snapshot unique 收敛，Action/Job/Gateway 冻结并核对同一 intent。
29. `ledger_route_bootstrap(first_start|automatic_natural_rollover|resume_rollover|start_after_stop_rollover)`在重复、双worker与各crash point下只产生同identity ledger/完整target-set/active route/read-model+pending settlement；持续running跨任务日由Planner只建一个pending BootstrapOperation，recovery仅在旧ledger settlement completed且blocker两层count=0后建新route。`normal_running`只允许同事务closed旧active-running route；`takeover_closed`只允许以final-manifest/imported-baseline证明的既有active-closed route为source且永不重开。settling完成后auto request、rollover-ready直接auto、late fact、双worker及pause/stop交错只有一个winner；orphan ledger、preparing takeover或hash漂移blocked。
30. running takeover cutover pause只推进一次lifecycle epoch `E`；最终`takeover_activate_running`同事务切Task/enrollment/route且保持`E`，注入任一CAS失败三者均不半激活，generic resume不被调用。paused/stopped/closed分类激活保持原Task状态且发送增量=0。
31. lifecycle_control 从 preparing/blocked/paused/incompatible/missing-route 执行 pause/stop/delete 按矩阵收口；start/resume/PATCH/retry/reset 的非法来源返回稳定 typed 409，不能绕 helper 发起业务写。
32. rollback capability matrix 在首条 enrollment 前允许旧 baseline、之后拒绝任一缺 `ai_message_enrollment_fence_v1` 的 API/worker/Gateway SHA，或§4.5任一producer实例缺`ai_message_dependency_producer_v1`/writer bit；混合role、旧producer、旧generic resume与旧Gateway均无法通过Gate。
33. legacy inventory building/seal 重复扫描捕获 cutoff 前晚提交 Task；writer 只 join sealed+allowed+open item 与 allowed epochs，takeover/retire CAS 后该 Task 不能再走 legacy，缺 item 的复制/新 Task 被拒。
34. ledger-level read-model revision 对并发可见 transition 无 lost bump；summary 返回 whole target-set，cursor 更换任一 filter/limit/endpoint 或版本后返回 typed 422/409，不混页。
35. pause 将 epoch `E -> E+1` 后，旧 pre-call unit 只能由 lifecycle_adopt CAS到新 epoch；resume等待 adoption=`ready|complete`并复用 `E+1`。initial cursor已越过的deferred item在ready后取得safe evidence时必须append唯一SafeEvidence、把原item分配新item_seq并reopen adoption，不能新建第二计数item；双证据与崩溃重放只递减一次deferred。Tx C与adoption worker按route→adoption/item→obligation/journal无死锁。两个不同command ID并发pause/stop、已paused再pause、已stopped再stop都只有一次状态CAS/epoch推进/adoption行；双resume、Generation persist/Dispatcher并发时无旧owner复活；call-issued/unknown保留旧epoch只reconcile。
36. multi-clock helper 对调用方输入 A→B 与 B→A 都归一为 wake_key C-collation升序；subscriber/producer/clear-dirty真 PostgreSQL并发无 deadlock且版本/dirty不丢。
37. 每个obligation创建与唯一deadline guard同事务；从未waiting的open/materializing/generation/action-bound、普通wake已supersede其他订阅、call-issued hold均可被time-due drain发现。deadline与wake、safely-not-executed、lifecycle_adopt、Generation、Tx A/Tx B/crash replay强制交错；到期后新provider request/call-issued增量为0，只有此前committed invoke可完成。
38. takeover readback后强制插入A类scoped event或修改Provider/config/target static revision，再activate必须CAS失败且保持原class/preparing；activation严格按global→enrollment/Task→ledger/route→SourceFence→target锁序，与projector反向输入无deadlock。只推进B类context/online/dedupe revision可activate，但imported owner在Tx A/Tx B前被最新事实重评估。
39. generic与group-AI专用PATCH逐字段族/混合请求/direct API共用决策器：current deadline/pacing不变，next-ledger/new-plan/new-intent/current-scope revision各自生效；scheduled_start、legacy pacing/hard-hourly、failure_policy/旧retry固定typed 422/409且有AuditLog。
40. 物理删除inventory内open/enrolled Task和current新Task时，先完成stop/adoption与remote archive snapshot；包含open unknown、call-issued hold、pending/failed projection和RemoteReconcileCase时仍可在双tombstone count/hash相等后删除Task/runtime，随后late fact只投影deleted-task tombstone。FleetPolicy、retired item/enrollment、Contract/RemoteMutation tombstone继续存在，logical task id、membership/fleet count及删除时bound/raw/unbound/hold计数不漂移；archive前/中/删除中崩溃按同operation幂等恢复。
41. 同tenant+group有大量Task时，listener source事务只推进一个`group_context:{tenant}:{group}` clock且不查询Task；所有订阅者按同version独立重评估。profile/online/material共享事件只写一个source clock+唯一fan-out event，recovery按cursor/item投影全部受影响active target，`discovered=applied+retired`，无无界源事务fan-out。
42. fan-out event在aggregate订阅前/中/后、Task target并发加入/结束、event/item worker各crash点都不丢wake或二次bump；source event replay、同event两个worker、A→B/B→A输入命中partial index且无deadlock，heartbeat/readback计数可对账。
43. first_start/start-after-stop/user-triggered rollover由TaskStartOperation外层持有，automatic running rollover只由LedgerBootstrapOperation owner持有；所有AI user-start严格按`TaskContract/Fleet/Inventory -> Enrollment -> Task -> TaskStartOperation -> ledger/route/bootstrap`取得锁并复用同一事务。same key重放、replaces冲突、双Planner/bootstrap worker和内部任一CAS崩溃不半建ledger/route；与pause/stop/takeover/blocker/PATCH/deadline反向输入无deadlock且只有一个winner，禁止Task/StartOperation→Enrollment反锁；跨时区边界按ledger数据库时间，旧call-issued/late fact只reconcile。
44. 并发/混合PATCH只推进实际受影响的next-ledger/content-plan/generation-policy/account-scope domain revision；name不重置3+3，timezone不改变current deadline，new ledger/plan/intent/scope分别冻结正确revision/hash，API effective_revision与manifest vector一致。
45. current scoped check-in首次safe pre-transport失败后，claim+同一memory原子转available/released；wake后第二Action只能CAS复用该memory并`reservation_version+1`，永久reservation-key unique不冲突。Gateway/unknown/confirmed不可reserve，legacy owner通过claim/alias不改旧memory。
46. required-material等待由素材创建/启停/asset/cache ready事件精确唤醒，reply等待由另一unit的bound remote fact推进`reply_source`唤醒；两者只重评估原assignment，不降relation/material、不另建义务，event-before与deadline交错真PG通过。
47. takeover legacy identity带合法target-op时按结构化字段映射；缺字段仅在group候选唯一时映射。0/多候选、target不在set或group冲突均route blocked并列出candidate/reason，重跑不得按顺序猜。
48. 第二真实success在无裁决时route不可reopen；受保护append adjudication后fact/binding/raw/confirmed/coverage均不改，只有全部conflict已裁决且source/read-model hash稳定时`contract_reopen`可移除typed blocker，再按resume/start恢复；普通retry始终无效。
49. stop adoption与Planner/start-after-stop/settlement强制交错：safe active rank只在Action/FOP终结同事务转retired，call-issued/protected绝不retire；重启后同rank仅一个更高ordinal active owner，旧identity保持cancelled。未重启到deadline时该空rank只生成一个`deadline_unmaterialized_after_stop` known shortfall，stop不隐式缩小settled target。
50. takeover source event同业务identity重放只insert/bump一次，payload冲突blocked；两个A writer与activate交错时SourceFence先于target/Action/fact锁序，无lost event/deadlock，readback按event_seq重算同hash。
51. quantity conflict/projection poison等enrollment-scope blocker在午夜与automatic rollover强制交错时，新ledger/Gateway增量均为0；owner-aware adjudication/reopen同时守住enrollment/route count恒等式，latest closed route也可合法裁决，但preparing migration blocker不能借此active。
52. dynamic coverage join/abandon只推进effective target revision/target row/read-model version，`ai_group_target_set_v1` hash不变且旧cursor因read-model version失效；next-ledger base target变化生成新hash。serializer在不同locale/timezone/JSON实现上命中同golden bytes/hash。
53. same-period legacy paused/stopped takeover最终原子建立current epoch imported-baseline complete；随后resume/start成功但不复活旧Action。rollover-eligible以latest closed/no-route+baseline触发新ledger bootstrap，绝不把closed route改running；与late fact并发按route锁唯一收敛。
54. `Task.version`历史回填、`TaskOut.task_version`与PATCH/start/pause/resume/stop/delete的`expected_task_version`端到端传递后，所有status/display/lifecycle入口与两个并发PATCH/stop只有一方成功；stale为409且零写。现有one-current-row TaskStartOperation每次processing清旧result，started一次写全ledger/route/hash，failed/replace/crash无部分result且same id只回读同结果。
55. settlement覆盖零obligation、部分物化、paused/stopped/closed、数千未物化due、coverage缺口与crash重放；未物化volume产生stable terminal-shortfall ordinal/consumed guard，immutable settled counts与逐unit/read-model守恒。
56. 真PG强制交错 `Tfact deadline前commit -> Tsettlement锁route -> Tprojector`、`Tprojector -> Tsettlement`、`Tsettlement锁route -> deadline后TxC`：前两者必须等required projection成功后结算，后者只进late/post-settlement；retryable failed只延后，poison才block，任何分支不把missed改met。
57. authoritative remote event time、同Attempt原子Gateway成功时间、无可靠时间三类分别投影on-time/late/unproven；总confirmed恒等式含三类，只有settled on-time quantity/coverage可写natural_day_target_met。deadline close→late success、rollover与delete并发不重开terminal unit。
58. inventory中draft、pre-start pending、stopped-zero-history、same-period running/paused/stopped、expired-ledger settling、rollover-ready、target_reached/wrapping_up、failed/completed/deleted逐类执行§10.3 operation；stopped-zero-history只命中never_started且与same-period stopped/rollover严格互斥。正常`running + latest ledger已过deadline`按settlement未完成/已完成分别唯一进入settling_closed/rollover_eligible，不被误判非法。接管前后Task业务状态相同（same-period running仅临时pause后恢复），非立即running类发送增量=0，非法status/ledger组合blocked。
59. producer writer inventory逐入口覆盖listener/voice-profile/account-online/account-security/ai-memory/material/API与消费roles；漏capability、旧SHA实例/lease未归零或受控clock/outbox readback失败时inventory policy不能创建/seal。
60. quantity adjudication、projection poison resolution、generic blocker resolution与owner-aware contract reopen均要求现有`system.manage`、各自独立approval与expected hashes；permission middleware与handler/service任一缺失均失败，同tenant无权限403、跨tenant/不可见对象404，普通Task retry/reset与前端无旁路。两个裁决/reopen并发只有expected revision winner。
61. 同blocker occurrence重放不加count；resolved后精确旧occurrence不得重开，新source revision再次poison必须新建一个open blocker并重新加count；并发新occurrence对同stable identity至多一个open blocker。两个open/resolve writer按owner→occurrence/blocker强制交错无deadlock或count漂移，任何blocker→owner反锁测试失败。相同source revision的不同snapshot只按规范hash打开一个`blocker_snapshot_conflict`；未登记kind/scope/channel全部拒绝。generic resolution与contract_reopen使用两个独立approval，不改canonical rows。
62. 当前ledger/target同时有future legacy obligation和当前due deficit时，`D_now`只含`due_at<=database_now`；C/U/H/P/W/T/R/M互斥anti-join每key只计一次，future不抵扣当前欠额，open-ready可物化且waiting/terminal不热循环。
63. enrollment retired、Task failed/completed/deleted以及target_reached/wrapping_up closed组合的直接HTTP/批量pause/stop/start/resume均被稳定reason拒绝；route-missing只在矩阵列明的live/zero-ledger身份收口。pause/stop不改route_epoch，旧lifecycle-epoch call-issued结果仍能按原route reconcile。
64. Tx C故意漏建一个required ProjectionState、篡改required count/set hash或插入未登记kind时整笔失败；settlement anti-join能发现legacy/current fact缺行，不把空集判projected。
65. 每一页API都用repeatable-read snapshot：强制`Tpage比version -> Twriter bump+改行 -> Tpage查询`时后续页只能409，不混入新行；更换filter/limit/endpoint仍422。
66. settlement golden fixtures覆盖目标数已on-time达标但另有unknown、quantity达标但coverage unknown/missed、late、unproven、cancelled、terminal shortfall与allowed overage；target/ledger status优先级、counts和snapshot hash与公式完全一致。
67. `worker-material-cache`作为独立实例必须出现在compose-derived producer inventory，并与backend/listener/实际material writers同SHA发布capability与受控clock/outbox读回；遗留旧role或漏实例时policy不能seal。
68. remote fact Tx C/projector、coverage、claim、alias每个A-writer都覆盖`readback -> A change -> activate`强制交错，SourceEvent/fact同成同败，且route→settlement→fence→target顺序无deadlock。
69. LedgerBootstrapOperation的pending/processing claim命中partial index；双worker、lease expiry、result已存在/全不存在/半存在三类readback分别收口completed、安全重领、blocked，不造第二ledger。
70. projection poison从settlement blocked经approve→fixed projector success后，只有无其他poison才对精确item/operation requeue pending；crash/并发重放不重复减blocker或丢next_retry，随后settlement→rollover成功。
71. 每个带存量ledger的takeover都在apply创建唯一SettlementOperation+完整TargetItems，preparing期不领取，activation CAS后到期项立即due；缺行/hash冲突不active，后续settle和rollover不永久卡住。
72. dynamic coverage join/abandon与settlement强制交错只有一个winner；deadline或settlement processing/completed后只写late scope/next-ledger revision，current effective target、unit与settlement hash不变。
73. takeover legacy fact分别覆盖authoritative remote event time、可证same-attempt atomic success和无权威时间三类；canonical order timestamp从不计on-time，第三类只能basis/timeliness=unproven。
74. `AiGroupTakeoverOperation`从rough preview、fence/quiesce、final manifest、多个chunk到readback/activate每个崩溃点均可恢复；同inventory item只有一个current operation，manifest item双unique和checkpoint range/input-output hash守恒。旧manifest supersede不改已分配identity，任一缺chunk/错hash/旧operation version都不能激活。
75. same-period running/paused/stopped takeover在final CAS前、恰等于deadline、deadline后一微秒强制交错：仅数据库时间仍在半开period内可激活；跨界winner保持preparing并supersede为settling-closed/rollover-eligible新manifest，旧class发送增量为0。
76. `normal_generation_exhausted`以extra-volume、coverage已完成、direct、active/usable面具、scoped check-in五项同时成立的fixture由Generation/Planner双worker强制交错；任一项缺失（尤其未完成coverage）时handoff/Action增量均为0并进入typed gap。合法fixture中Generation只能提交唯一handoff且Action=0；Planner只消费同一handoff创建一条ready签到Action、不新增job/Provider调用。handoff提交、Planner claim与deadline worker并发只有一个winner，deadline后新Action=0；双重放与lease接管不重复scoped claim/memory/count。
77. time-due subscription、两个settlement worker与已claimed check-in handoff强制交错：subscription只唤醒同一SettlementOperation；settlement chunk按obligation→FOP→handoff唯一顺序supersede并清lease。任一崩溃重放都只有一个handoff/shortfall winner，第二worker回读同result，不重复终结或产生deadline后Action。
78. settlement面对从未物化的missing rank只创建一条`materialization_mode=settlement_shortfall`终态义务，assignment/current intent/job/action均为空且精确绑定SettlementTargetItem；数据库CHECK接受该唯一例外。Planner、Generation、wake、handoff与Gateway对该行claim增量均为0；任一其他非终态或终态reason尝试空assignment/intent都在提交时失败。

### 13.2 迁移与性能

1. 含 legacy 多 check-in、重复失败、open、unknown、success 的脏数据 preview/apply/readback 幂等，旧 memory/action/fact 字段逐列 hash 不变。
2. takeover 不按历史 failed Action 数创建义务，只按当前事实缺口创建。
3. apply 前后 confirmed/remote fact 数一致，unknown mutation identity 一致。
4. `AiGroupMessageMemory` 生产规模窗口查询继续命中账号+expiry 索引；check-in partial unique 在真 PostgreSQL 并发成立。
5. legacy alias 允许多个历史失败 identity折叠到一个新义务，同时保证每个 legacy identity/remote fact最多一个新 owner；历史不同 remote fact各自分配 unit/binding，current重复 success保留 unbound conflict；旧事实原文和 mutation identity不改写。
6. preparing fence/quiescence、canonical 分类/排序、分块 apply、进程崩溃续跑和最终 activation 都使用同一 manifest；active 前任何 chunk 缺失均 blocked。
7. generated-ready 但空 variation/intent/request identity 的旧 Action 只进入 legacy audit，不再执行；合法 pre-Gateway owner 一一绑定。
8. current target 缺 ledger/target operation、confirmed 超 due、多个 unknown、A 类 identity/conservation source漂移分别显式 blocked；B 类 liveness/dedupe 推进只按最新 scoped version逐行重评估 imported owner，同一 ordinal 转 waiting/ineligible且不让 manifest 饥饿。
9. canonical sort 的每个 legacy timestamp fallback、UTC/微秒、NULLS LAST 与 ID C-collation 在缺新字段/缺关联组合下均 golden-test 固定；重跑 ordinal、order source 与 manifest hash 完全相同，旧行 hash 不变。
10. ready obligation、check-in handoff、lifecycle adoption owner/unit、ledger bootstrap、takeover operation/checkpoint、settlement、dependency fan-out、dirty wake clock、time-due subscription、projection due、obligation/attempt API 与 message-memory dedupe 查询建立 partial/composite indexes：`(task_day_ledger_id,state,due_at,id)`、handoff的`(state,created_at,id) WHERE state IN ('pending','claimed')`与`(lease_expires_at,id) WHERE state='claimed'`、Adoption owner的`(state,adoption_seq,id) WHERE state IN ('pending','draining')`、AdoptionItem的`(state,item_seq,id) WHERE state IN ('pending','processing')`、obligation表的`(task_day_ledger_id,task_lifecycle_epoch,state,id)`、LedgerBootstrapOperation的`(next_retry_at,next_period_start_at,id) WHERE state IN ('pending','processing')`与`(lease_expires_at,id) WHERE state='processing'`、TakeoverOperation的`(next_retry_at,id) WHERE state IN ('quiescing','manifested','applying','readback')`、ChunkCheckpoint的`(operation_id,manifest_id,state,first_item_seq,id) WHERE state IN ('pending','processing')`、SettlementOperation的`(next_retry_at,deadline_at,id) WHERE activation_ready=true AND state IN ('pending','processing')`、SettlementTargetItem的`(operation_id,state,target_operation_target_id,id)`与`(lease_expires_at,id) WHERE lease_owner IS NOT NULL AND state='processing'`、remote fact的`(task_day_ledger_id,fact_id) WHERE task_day_ledger_id IS NOT NULL`、fan-out event的`(state,dirty_seq,id) WHERE state IN ('pending','draining')`、fan-out item的`(event_id,state,task_day_ledger_id,target_operation_target_id)`、`(dirty,dirty_seq,wake_key) WHERE dirty=true`、`(wake_key,state,observed_version,obligation_id)`、`(state,wake_at,id) WHERE state=pending`、projection表的`ix_fact_projection_pending(next_retry_at,id) WHERE state IN ('pending','failed')`、takeover source event unique及`(fence_id,event_seq)`、`(task_id,task_day_ledger_id,due_at,id)`、`(obligation_id,created_at,id)` 及 §4.1 fact identity/quantity binding unique。projection迁移必须替换现有同名`(next_retry_at,fact_id,projection_kind)`索引，claim keyset/order固定`(next_retry_at,id)`，row补lease owner/epoch/expiry/version；不能另建第二套或误用不存在的`status`列。真PostgreSQL EXPLAIN必须证明check-in handoff、bootstrap、takeover/checkpoint、pending/failed、settlement/fact barrier、fan-out与source-event keyset命中对应partial/composite index。
11. 用生产规模匿名 cardinality 执行 `EXPLAIN (ANALYZE,BUFFERS)`；上述热查询不得对 obligation/clock/subscription/projection/fact/memory 主表做全表 Seq Scan，事件 drain 不得全量 join pending subscriptions；batch=100 的 p95 不得比发布前同类查询基线恶化超过 20%，计划与实测作为 Gate artifact。
12. `-m no_postgres` 纯回归与真 PostgreSQL 专项分别通过；缺数据库 fixture 不能写 pass。
13. confirmed+hold+pre-call+open+waiting+shortfall+future+cancelled 与 coverage subtype重叠的 golden fixture按互斥集合只计一次；`missing_due_units`、planned boundary overage、ordinal与readback守恒逐项固定。
14. 合法 legacy pre-Gateway内容只产生一条带current fence的新 Action或generation-reconcile wrapper；旧Action/Job/memory逐列hash不变且dispatch增量为0，active owner/conservation只计新行。check-in接管不造dummy variation。
15. 生产规模真PG按§15.1演练nullable/Task.version keyset backfill、NOT VALID/VALIDATE、CONCURRENTLY index、partial unique替换与projection index swap；强制lock timeout/invalid index/重复preflight失败时旧constraint仍有效、current writer/enrollment=0，重跑只清理本次invalid对象。catalog hash、predicate、indisready/valid与EXPLAIN全部readback。

### 13.3 前端

1. active backlog 不含 terminal failed/skipped；历史总数仍可下钻。
2. 同一 blocker 显示 unique obligation、账号和 attempt 三种计数。
3. capacity gap、quality waiting、dependency waiting、unknown 和 shortfall 文案不同。
4. manual_required 面具状态、最后错误和权限受控操作可见。
5. 历史 `direct_check_in_10d_duplicate` 可解释为旧实现错误，但新记录不再产生该码。
6. daily summary 与 obligations/attempts cursor API 使用 typed DTO；route/read-model version 改变时 409 刷新，不跨快照拼页。
7. retry/reset 在 current route disabled 且直接 API 同样 409；pause/stop/blocked/incompatible 使用现有 Task 状态加 typed route reason，不展示不存在的 Task status。

## 14. 生产 E4 与完成判定

发布锚点之后必须沿真实链路验证：

```text
Task
  -> AiGroupMessageContractEnrollment
  -> TaskDayLedger / whole-target-set AiGroupMessageContractRoute
  -> TaskGroupDailyTarget
  -> AiGroupContentAllocationPlan / RequirementAssignment
  -> AiGroupMessageObligation
  -> AiGroupContentIntent / AiGroupContentVariation
  -> Action
  -> ExecutionAttempt
  -> FulfillmentRemoteFact(remote_message_observed)
  -> AiGroupMessageQuantityFactBinding(bound + timeliness)
  -> TaskGroupDailyTarget / optional TaskAccountDailyCoverage projection
  -> AiGroupMessageReadModelRevision
  -> AiGroupTaskDaySettlementOperation / immutable on-time settlement
```

### E4 必过

1. 受影响两个 coverage scope 在事实版本不变期间新增 `direct_check_in_10d_duplicate` 为 0，新增失败 Action 为 0。
2. 其他 Task/群的同账号 check-in success 不阻断当前 scope；当前 scope 最多一个 owner。
3. 至少一个真实 scoped check-in 或 normal coverage 样本取得成功 Attempt、非空 remote message ID、typed remote fact 与 coverage 投影；没有自然安全样本则该项保持 `unproven`。
4. 至少一个 extra-volume normal 样本具有稳定 obligation、非空 variation key，并最终取得真实远端成功事实；duplicate replacement 必须显示递增 variation 和禁用参照。
5. 受控 `content_capacity_gap` 样本始终占同一 due-unit key：等待期间 Action/GenerationJob 增量均为 0，Task due/欠额保持 1；真实 profile/context/aggregate-capacity clock 递增后同一义务只唤醒一次。
6. Gateway prepared/call-issued/started/unknown 按同 request evidence 分类；unknown 没有 replacement，confirmed 只来自远端事实；生产不人为制造 unknown，崩溃点由真 PostgreSQL/受控 QA 证明，生产自然出现 hold 时再核对零 replacement。Dispatcher/Gateway 健康只作中间证据。
7. activation readback 证明 TakeoverOperation为activated、全部manifest/checkpoint count/hash闭合，并证明 enrollment/route epoch、target-set/manifest hash、quantity ordinal+active rank、legacy alias、confirmed/unknown/shortfall 守恒；切换后该 Task 当前及后续 ledger 的 legacy route 新 Action/Gateway journal 增量为 0，所有新 Action 带当前 enrollment/route epoch。
8. 至少一个跨技术批次内容 allocation 样本证明 reply/material/act-type assignment 与 aggregate count 未因 replacement 或 extra→coverage 改变。
9. UI active backlog、waiting 和 attempt history 与数据库三层事实一致。
10. 当前部署 SHA 的 dependency fan-out、wake/deadline、fact projector、lifecycle adoption、ledger bootstrap、task-day settlement 及 `ai-generation` reconcile 全部owner heartbeat存在；各自pending/processing/blocked/failed/poison backlog、oldest lag、lease recovery、last success可对账，目标task-day的required projection anti-join为0。
11. daily target readback 使用 TaskDayLedger period/timezone 与 frozen due_at；不存在 naive target_date/Action-success 增量，route/read-model version 与 API 页面一致。
12. fleet inventory 中所有Task已按never-started/running/paused/stopped/rollover/closed/terminal分类enrolled或审计retired；`AiGroupMessageContractFleetPolicy allowed -> preparing -> disabled` 两次CAS、membership/state hash、open item=0、missing enrollment=0均回读，切换后连续窗口 legacy Action/Gateway journal 增量为0。不得以“全局contract CAS”模糊代替FleetPolicy实际字段。

### 结论分级

- `qa_pass`：代码与测试通过，不能代表线上；
- `product_accepted`：产品合同和页面验收通过，不能代表远端发送；
- `production_fixed`：上述 E4 全部取得当前部署 SHA 的真实生产证据；
- `canary_production_fixed`：仅首个 Task E4 通过；fleet inventory/global contract 未闭合时不能升级为整体 `production_fixed`；
- `natural_day_target_met`：任务日结束后只有immutable settlement=`met`，且settled on-time quantity、on-time coverage全部达标才成立；总confirmed、late/unproven或部署后修正不能代替，与 failure-churn 修复单独报告。

## 15. 开发交接与预计改动边界

### 15.1 模型与迁移

| 责任 | 预计路径 | 要求 |
| --- | --- | --- |
| typed obligation + fleet inventory/enrollment/route/read model | 新建 `backend/app/models/ai_group_message_obligation.py`、`ai_group_message_route.py`、`ai_group_message_read_model.py` | obligation/current intent、fleet+item、enrollment/route+双scope ContractBlocker+Occurrence、LifecycleAdoption+Item/SafeEvidence/imported baseline、TaskDaySettlementOperation+TargetItem、task domain revision/history、LedgerBootstrapOperation+request revisions、TakeoverOperation/Manifest/Item/ChunkCheckpoint、takeover source fence/event、contract tombstone、target-set serializer version、ledger read-model revision/索引/FK；逻辑task id不级联；每文件≤500行 |
| allocation/assignment + intent/variation/check-in handoff | 新建 `backend/app/models/ai_group_content_allocation.py`、`ai_group_content_attempt.py` | aggregate守恒、active assignment partial unique、immutable intent/variation、`AiGroupCheckInHandoff`业务unique/lease/六轮evidence与Planner消费状态 |
| wake + dependency fan-out + claim/legacy alias | 新建 `backend/app/models/ai_group_obligation_wake.py`、`ai_group_dependency_fanout.py`、`ai_group_obligation_legacy.py` | dirty clock sequence/lease、event/time subscription、dependency event/item cursor/lease/partial indexes、素材/reply/shared account clocks、scoped claim、many-to-one immutable alias |
| quantity identity / active-rank allocator | `backend/app/models/task_group_daily_target.py`、typed obligation model | additive target operation FK、ledger current partial unique、`next_quantity_ordinal/effective_planned_target_revision/version/pacing_snapshot_hash/target_effective_at`；obligation保存`effective_due_rank/due_rank_state/rank_retired_reason/rank_retired_at`并以active/protected partial unique约束rank owner。target down→up、stop-safe→start、protected overage与nullable-owner SettledRankSet均由同事务CAS验证 |
| message memory | 抽出 `backend/app/models/ai_group_message_memory.py` | 保持原表名与 import 兼容，补 §4.6 字段/索引 |
| Provider/memory revision owners | Provider/Tenant AI setting 模型、AI memory scope revision | additive config revision/health epoch 与 account memory scope revision；所有 §4.5 writer 同事务递增 |
| fact projection/quantity binding/adjudication/Gateway evidence | `fulfillment_v2.py`、新 AI quantity fact binding/conflict adjudication/poison resolution、Gateway journal model | canonical fact只按request identity unique，补remote_effect_at/time basis与ledger index；binding对bound obligation partial unique并分on-time/late/unproven；append-only adjudication+owner-aware contract_reopen；projection lease/version/failure_class与poison resolution；call-issued immutable evidence |
| Task/Start row version | `backend/app/models/task_center.py`、`fulfillment_contract.py`及所有Task mutation入口 | additive `tasks.version`在线回填；现有每Task一条TaskStartOperation只补result_*，processing清旧result、started原子写全；不改成历史多行 |
| model exports/cleanup | `backend/app/models/__init__.py`、`physical_task_deletion.py`、retention/export 清单 | stop/fence后把hold/fact/binding/projection/reconcile case归档到RemoteMutation+Contract tombstone并readback，再retire item/enrollment、逆FK删runtime；policy/item/enrollment/tombstones/global manifest不级联；late fact走deleted-task tombstone projection |
| additive DDL | 从合并时真实 migration head 新建 revision | migration 只建 schema/index；生产规模 backfill 不在 Alembic 事务内执行 |
| inventory bootstrap 工具 | 新建 `backend/scripts/bootstrap_ai_group_legacy_inventory.py` | 受保护 `building -> cutoff keyset scan -> inflight barrier -> readback -> sealed+allowed`；preview/apply/readback hash 与审批输入，Alembic 禁止代做 |
| takeover owner/source fence/工具 | 新建TakeoverOperation/immutable manifest/item/checkpoint、source-fence/event模型与 `backend/scripts/reconcile_ai_group_message_obligations.py` | A类scoped writer同tx bump；static vector在activation重读；强制 `preview -> fence/quiesce -> immutable manifest -> checkpointed apply -> readback -> fenced activate`，输入operation/version/SHA/Task/ledger/hash不匹配即停止 |

生产DDL必须分相位在线执行，且整个schema阶段 enrollment/route=0、current writer inactive：

1. 先建新table/sequence/nullable列。`tasks.version`先以nullable+server default 1添加，新写入自然为1；历史NULL按Task PK keyset小事务回填并readback null=0，再建`CHECK(version IS NOT NULL) NOT VALID -> VALIDATE`，最后在低`lock_timeout`短事务`SET NOT NULL`。不得一次长事务更新全表。
2. FK/CHECK先以`NOT VALID`或等价安全形式添加；完成orphan/非法半行/重复业务键preflight后再`VALIDATE CONSTRAINT`。preflight输出count、样本hash和审批artifact，发现重复不自动删除/改写。
3. 所有大表普通/unique index用PostgreSQL `CREATE [UNIQUE] INDEX CONCURRENTLY`，Alembic必须用`autocommit_block`逐条执行。先在旧`uq_task_group_daily_target`仍有效时并发建并验证legacy/current两条命名partial unique index，再以低lock timeout短事务drop旧full unique constraint；partial unique本身即约束，禁止尝试把partial index attach成普通UniqueConstraint。任一并发index失败只清理`pg_index.indisvalid=false`且名称/definition完全匹配的本次invalid index并留审计，旧constraint与writer fence保持。
4. projection索引先并发建`ix_fact_projection_pending_v2(next_retry_at,id)`及其他wake/fan-out/settlement/API索引，catalog/readback与EXPLAIN通过后短事务把旧同名index改old、v2改正式名，再`DROP INDEX CONCURRENTLY`旧index；交换窗口两条物理index至少一条可用。unique索引、predicate、opclass/collation、`indisready/indisvalid`均逐项核对。
5. 最后验证全部FK/CHECK、partial predicate、Task.version null/default、表/sequence owner、索引定义及生产规模EXPLAIN，归档migration before/after revision与catalog hash，才允许§11 inventory bootstrap。lock timeout、statement timeout或readback任一失败都停止在current writer inactive；不得人工忽略、半建后启用或在已有enrollment后回滚到不识别新schema的SHA。

设计基线的 migration head 是 `0144_avatar_material_sources.py`，但 dev 必须以合并时真实 head 为准，禁止预占或写死下一个 revision。

### 15.2 后端责任拆分

| 模块 | 责任 |
| --- | --- |
| 新 `ai_group_message_obligations.py` | due unit 创建、动态 coverage 转换、状态 CAS、FOP 映射 |
| 新 `ai_group_content_intents.py` | aggregate allocation/assignment、immutable intent、generation epoch、variation 与禁用语义集合 |
| 新 `ai_group_obligation_wake.py` + `ai_group_dependency_fanout.py` | durable clock/subscription、event-before、共享source event→target aggregate有界fan-out、material/reply source；deadline入口只激活SettlementOperation，shortfall由settlement owner写入 |
| 新 `ai_group_obligation_takeover.py` | shadow manifest、target-operation唯一映射、legacy alias、source event/fence、守恒/readback、route activation |
| 新 `ai_group_message_route.py` | fleet/inventory/enrollment/task-day route、双scope ContractBlocker、现有TaskStartOperation user start、LedgerBootstrapOperation request revisions、start-after-stop、分类takeover activate、lifecycle adoption item/imported baseline、task domain revisions、owner-aware contract reopen及统一fence |
| 新 `ai_group_task_day_settlement.py` | 任意Task状态的deadline target/ledger结算、未物化due terminalization、projection barrier、immutable SLA snapshot与rollover gate |
| 新 `ai_group_fact_projector.py` | projection claim/lease/replay、remote effect timeliness、target/coverage/claim/wake 原子投影、retryable/poison分账与受保护poison resolution |
| `direct_check_in.py` | 删除账号全局10天查询；统一check-in scoped claim；safe失败复用同一memory并推进reservation_version |
| `ai_message_memory*.py` | normal查询排除check-in并用dedupe expiry；保存obligation/variation；current scoped reservation key永久唯一 |
| `daily_coverage.py` | failure release 改为 typed transition；不再把确定性 blocker 直接置 ready |
| `executors/group_ai_chat.py` | 只保留薄 Planner 编排；按 stable obligation/intent 物化，不产生 Action-only extra-volume |
| `ai_generation_parallel.py`、`ai_generation_quality.py`、`ai_generation_worker.py` | obligation/GenerationJob/variation claim 与 typed failure transition；`ai-generation` role drain provider reconcile |
| `dispatcher.py`、`gateway_evidence_journal.py` | Tx A prepare、Tx B committed call-issued guard、事务外 call、Tx C typed result |
| `fulfillment_remote_facts.py` + 新 AI projector | remote fact alias、message obligation、target 和 coverage 幂等投影；`recovery` role durable drain |
| `daily_fulfillment.py`、`api/routers/task_center.py`、`schemas/task_center.py` | ledger-level read-model revision、§8.2 target-set summary/obligations/attempts typed API 与 normalized-query snapshot cursor |
| `service.py` + 新 lifecycle/revision transition module | `Task.version`共享CAS、§8.3 domain PATCH、现有one-current-row TaskStartOperation first/start-after-stop/rollover、pause/resume/stop/retry/reset/delete/contract_reopen guard；不原地resurrect Action |
| `daily_group_target.py` | ledger-bound active-rank owner/effective revision CAS、monotonic quantity identity、frozen due_at、protected overage、SettledRankSet与remote-fact数量守恒 |
| `worker.py`、compose/runtime/check-worker-role | `recovery` 注册 wake+deadline+fact projector+settlement+Gateway reconcile，`ai-generation` 注册 provider reconcile；全部dependency producer role发布writer-family capability，heartbeat metadata 与 role 回归 |
| `production_e4_diagnostics.py` | 新 typed chain、route/manifest、projection lag、legacy zero-increment 与 TaskDayLedger readback |

`daily_coverage.py`、`group_ai_chat.py` 已远超 500 行硬限制；dev 必须把新逻辑放入上述专责模块，并逐步把本次触达的旧责任抽出，不能继续向巨型文件追加状态分支。函数保持 ≤50 行、复杂度 ≤10，所有状态改变使用命名 transition helper 与单行 CAS。

### 15.3 前端

- 从 `TaskCenterDetailModal.tsx` 拆出独立 `AiGroupFulfillmentPanel`、`AiGroupObligationWaitPanel`、`AiGroupAttemptHistoryPanel`；
- `frontend/src/app/types/taskCenter.ts` 增加 quantity/coverage/wait/failure aggregation 类型，不把 JSON 透传为 `any`；
- active/等待/历史分别请求或分页，终态历史不阻塞首屏；
- 保留既有权限组件，新增文案与标签测试；不增加“全部重试/清空 unknown/重置任务”入口。

### 15.4 必须新增或改写的测试入口

```text
backend/tests/test_ai_group_message_obligation.py
backend/tests/test_ai_group_message_obligation_postgres.py
backend/tests/test_ai_group_obligation_wake.py
backend/tests/test_ai_group_dependency_fanout_postgres.py
backend/tests/test_ai_group_content_allocation.py
backend/tests/test_ai_group_content_variation.py
backend/tests/test_ai_group_check_in_scope.py
backend/tests/test_ai_group_gateway_boundary.py
backend/tests/test_ai_group_obligation_takeover.py
backend/tests/test_fulfillment_fact_first_v3.py
backend/tests/test_ai_generation_parallel_postgres.py
backend/tests/test_ai_group_daily_coverage_planner.py
backend/tests/test_ai_group_message_memory_postgres.py
frontend/src/app/views/__tests__/TaskCenterDetailModal.test.tsx
backend/tests/test_ai_group_contract_route.py
backend/tests/test_ai_group_contract_blocker.py
backend/tests/test_ai_group_lifecycle_commands.py
backend/tests/test_ai_group_lifecycle_adoption_postgres.py
backend/tests/test_ai_group_task_day_settlement_postgres.py
backend/tests/test_ai_group_task_revisions.py
backend/tests/test_ai_group_task_start_contract.py
backend/tests/test_ai_group_target_set_hash.py
backend/tests/test_ai_group_fact_projector.py
backend/tests/test_ai_group_quantity_conflict_adjudication.py
backend/tests/test_ai_group_physical_delete_archive.py
backend/tests/test_ai_group_daily_target_ledger.py
backend/tests/test_ai_group_transaction_order_postgres.py
backend/tests/test_ai_group_api_contract.py
backend/tests/test_worker_roles.py
```

旧的 `test_ai_generation_phase_boundaries.py` 中把 `direct_check_in_10d_duplicate`、`mask_missing_check_in` 和 extra-volume 空 variation 固化为正确行为的断言必须改为新合同回归；不能仅新增测试而保留冲突旧断言。

开发完成后再同步 `project-structure-index.md` 的真实文件、方法、行数和测试入口；本设计不把计划路径伪报为已实现结构。

## 16. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 用户原始问题 | 覆盖签到失败风暴、duplicate 风暴、面具资产和“拥堵”误读 |
| 产品/前端 | 义务、运行、等待、历史四层状态及权限已定义 |
| 后端/API/worker | typed obligation、intent、variation、wake、Planner/Generation/Dispatcher 已闭合 |
| 数据流与真相源 | remote fact 仍唯一完成源；Action 不再充当欠额身份 |
| 并发/幂等 | 五类数据库唯一/CAS/unknown 防重已定义 |
| 失败与边界 | pre-Gateway、Gateway unknown、依赖、质量、容量、deadline 已分类 |
| 安全/隐私 | 不降质、不泄露 prompt/正文、权限和 AuditLog 已定义 |
| 存量与迁移 | preview/apply/readback、历史冲突、当前 Task 原位接管已定义 |
| 发布/回滚 | 单 SHA、contract epoch fence、activation 后 incompatible pause 已定义 |
| QA/E4 | 单测、PostgreSQL、前端、迁移、真实远端链路与结论分级已定义 |

`design_status=product_design_complete`；2026-08-10最终独立fresh复核确认当前冻结快照无阻断实现、迁移、发布或生产E4的P0/P1。开发、迁移、QA、发布和生产E4仍尚未开始，设计通过不得解释为修复完成。任何把waiting留在open热索引、用固定次数停止重试、恢复legacy ContentMix真相源、或只给extra-volume填随机variation字符串的实现都不满足交接。
