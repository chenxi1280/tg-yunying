# 频道浏览 Planner 饥饿与来源状态修复专项 PRD

> 日期：2026-08-10
> 分级：L3（生产履约阻断）
> `design_status=product_design_complete`：2026-08-10最终独立fresh复核确认本文件与主PRD、闭合PRD、数据流索引无实现/迁移/发布/E4阻断P0/P1；允许进入dev，但实现、QA、发布和生产E4仍未开始。
> 本文只修复 `task_type=channel_view`；其合同优先于历史“最晚 future Action 作为新批锚点”和“按 Task 串行拟人间隔”的冲突描述。

## 1. 原始问题与生产事实

用户要求修复线上“浏览任务仍堵、任务没有启动/没有完成”的问题，并由本任务完成设计、代码、发布及真实线上验证。

2026-08-10 只读生产证据显示：

- 两个 running 浏览任务当前分别已有 31、25 条未来 Action，但 `ExecutionAttempt=0`、`ViewRemoteFact=0`；当前累计到期量分别远高于已物化量；
- 两个任务的最晚 open Action 都已排到任务日 23:57。当前 `reserve_task_schedule_times()` 把新批整体平移到该时间之后，越过 deadline 后返回空列表；
- 另一个 running 任务的最新来源消息已超出 `message_active_days`，active message 为 0；页面没有 typed 来源状态，表现为“卡住”；
- 当前可用浏览账号池少于单消息 1,000 目标，而 `ViewRemoteFact` 的远端事实身份是 `target_peer + message + account` lifetime unique。目标不可达时系统只有 warning，没有可验收的结构性 shortfall；
- 现 E4 把“已物化 obligation 数”误当 required，可能在只完成少量 Action 时假绿；active source 为 0 又一律误报 obligation missing。

根因不是 Dispatcher backlog：首个断点发生在 Planner 的 due 物化和来源解释边界。健康检查、容器启动、已有未来 Action 或普通 `last_error` 都不能证明浏览履约。

### 1.1 2026-08-24 午夜源节奏回归

新自然日首个浏览 due unit 已在 00:00 后合法产生，账号预约允许 00:05 执行，但最终 source Gateway gate 把当时仅累计的 `sum(due_count)=1` 当成全天计划总量，冻结出 `86400s` 间隔；该间隔与上一自然日 23:59:55 的真实 Gateway marker 组合后，把新日首个 Action 推到当日 23:59:55，其余 due unit 全部停在 Action 前。

共享来源最小间隔必须以同一 ledger 全部 active message target 的 `sum(effective_target_snapshot)` 计算。当前允许执行的数量仍只由各 target 的 DueSet 和 Action `release_not_before_at` 控制；因此完整目标总量只决定相邻 Gateway 的最小安全间隔，不提前释放尚未到期义务。禁止用逐步增长的 `due_count` 作为全天 plan total，否则午夜第一条会被错误解释为“一天只允许一条”。已冻结错误间隔的存量 Action 只有在精确 Task/date/action、无 Gateway、无 typed remote fact、owner/current binding 未漂移及 preview hash 全部通过时，才可审计重算 admission gap 和 source cursor；不得重放 unknown 或已触达 Gateway 的 Action。

### 1.2 2026-08-28 全账号每日覆盖与时区边界补正

- `channel_view.account_coverage_mode` 的当前唯一配置值为 `all_accounts_daily`。Planner 必须扫描任务范围内全部候选账号，并以当日 `view_message` coverage 作为优先级；不得用普通分页上限截断账号池。
- 每条消息仍受 lifetime `(target_peer_id, channel_message_id, account_id)` 排除、账号日浏览上限、当前 DueSet、任务日容量和逐账号 membership 限制。已有义务在重绑 Action 前必须复核 task/ledger/target/message/account/materialization identity 完全一致。
- 同一轮多消息分配必须在消息缺口与合法账号边之间执行确定性最大基数匹配，优先让当日未覆盖账号获得至少一个合法 slot，再按稳定 message/account 顺序填充剩余缺口。禁止逐消息贪心消耗同一批账号，导致后续消息有合法解却被饥饿。
- `TaskDayLedger.period_start_at/planning_anchor_at/deadline_at` 与由 deadline 派生的 `ChannelViewDailyMessageTarget.active_until` 是 UTC storage；进入北京墙钟排期时使用 UTC-storage 转换。消息 `published_at` 和 target `accrual_anchor_at` 遵循项目的北京墙钟合同：naive 值按北京墙钟解释，aware 值先转换到北京时区。两类时间不得共用“直接去掉 tzinfo”的兼容路径。
- 本补正只改变 pre-Gateway 账号选择、义务身份校验和排期时间解释；成功仍只认 `Action -> ExecutionAttempt/Gateway -> ViewRemoteFact -> TaskAccountDailyCoverage`，Action 创建或本地测试不能充当浏览履约。

## 2. 产品合同优先级

频道浏览按以下顺序解释，低层规则不得覆盖高层规则：

1. 每消息每日目标、累计目标、`TaskDayLedger` 边界和 distinct `ViewRemoteFact` 是数量真相；
2. Telegram lifetime source identity、防重复、账号授权/Session/代理、membership、FloodWait 和 Gateway 容量是硬安全边界；
3. `due_by_now` 决定当前允许物化的最大业务量；future Action 只是一条 Action 的软排期，不是 Task 的容量预留；
4. 浏览拟人节奏按独立账号的真实操作链解释。不同账号对同一消息的被动浏览不是必须按 Task 全局串行的副作用；
5. 配置的 task 级 template/curve 只分布候选时间，不能把 1,000/消息隐式改成最多 480/日，不能减少 due、目标或事实成功数；
6. 外部身份容量不足保持原目标并形成 typed structural shortfall，禁止降低目标、伪造 success 或 silent running。

## 3. 唯一身份与精确数量集合

### 3.1 业务身份

- 业务目标先形成稳定 due unit，账号是该 due unit 的当前执行绑定，不再让“已选到账号的 obligation 行数”兼任目标分母；
- `ViewFulfillmentObligation` additive 增加 `daily_message_target_id/target_peer_id/source_revision/target_revision/due_ordinal/materialization_version/lifecycle_epoch/deadline_at`，`account_id/current_action_id` 在未物化时允许为空；
- due unit状态闭集为`unmaterialized|action_bound|gateway_inflight|unknown|remote_identity_conflict|confirmed|terminal_shortfall`；明确普通pre-Gateway失败只能`action_bound -> unmaterialized`，Tx/Gateway证据只能单向推进，unknown/confirmed不可重开；`remote_identity_conflict`只可由下文受保护source-identity resolution释放，不属于普通retry；
- current 合同唯一键为 `(daily_message_target_id, due_ordinal)`。同一 due unit 的安全 pre-Gateway 重建只递增 `materialization_version`并可绑定另一个账号；旧 Action/Attempt 保留旧账号和版本历史；
- 新Action payload冻结 `view_fulfillment_obligation_id + materialization_version + target_peer_id + account_id + source_revision + target_revision + ledger_id + route_epoch`，claim/Gateway前全量复核；Gateway-started/unknown/confirmed后禁止换账号；
- additive `ChannelViewActionBinding(action_id unique,view_fulfillment_obligation_id,materialization_version,account_id,route_id,route_epoch,state=active|released|terminal,version)` 是current Dispatcher唯一结构化owner。新Action与binding同事务创建；takeover中的合法legacy pre-Gateway Action保持历史字段/hash不变，但manifest apply为它建立binding。active route下Dispatcher必须同时命中active binding、immutable route_epoch与义务version，并实时要求route active/blocker=0；旧SHA/无binding Action不得执行。route行的普通writer version/read-model变化不使已合法Action伪stale；
- legacy 与 current writer 共同使用数据库唯一的 `ChannelViewLifetimeIdentityOwner(tenant_id,target_peer_id,channel_message_id,account_id,state=available|pre_gateway|call_issued|unknown|confirmed|conflict,owner_kind=legacy_action|current_obligation|remote_fact,logical_owner_task_id,owner_task_record_id nullable,owner_obligation_id nullable,owner_action_id nullable,materialization_version,request_identity nullable,remote_fact_id nullable,owner_contract_tombstone_id nullable,next_transition_seq,version)`；物理唯一沿用fact身份域`(target_peer_id,channel_message_id,account_id)`，tenant一致性由account FK/guard证明。`logical_owner_task_id`永久保留，`owner_task_record_id`只是nullable导航FK，完成tombstone回填前RESTRICT、之后`ON DELETE SET NULL`。append-only `ChannelViewLifetimeIdentityTransition(owner_id,transition_seq,transition_identity,from_state,to_state,logical_owner/request/evidence hashes,owner_contract_tombstone_id nullable,created_at)`唯一`(owner_id,transition_seq)`及`transition_identity`；每次owner expected-version CAS同时分配`next_transition_seq`并插transition，任一失败整笔回滚，exact request重放只回读同transition。fence-compatible legacy/current Planner都必须在规范事务最前层insert-or-CAS `available -> pre_gateway`后才可创建Action；Tx A在外调前CAS `pre_gateway -> call_issued`，unknown/fact只单向到unknown/confirmed。只有同request可证明未transport时才`pre_gateway -> available`并留transition；call-issued/unknown/confirmed/conflict永不释放。它是mixed-fleet唯一pre-Gateway/lifetime占位，Action/obligation partial unique只作本Task二级校验；禁止先查legacy Action再插current owner；
- 远端事实唯一键保持 `(target_peer_id, channel_message_id, account_id)`，一个 lifetime source fact 只能绑定一个 due unit；
- 旧 `(task_day_ledger_id,channel_message_id,account_id)` unique只服务legacy rows。迁移完成后以 current/legacy partial predicates替代全表约束，禁止半空current身份。

新增 additive `ChannelViewDailyMessageTarget` 作为每个 ledger+peer+message 的 target/due owner，唯一键 `(task_day_ledger_id,target_peer_id,channel_message_id)`，至少保存：

```text
tenant_id / task_id / task_day_ledger_id / target_operation_target_id / target_peer_id / channel_message_id
source_revision / target_revision / pacing_anchor_at / active_until
daily_target_count / lifetime_remaining_at_attach / effective_target_count
source_state = active | expired | source_unresolved
baseline_due_count / baseline_active_elapsed_us / accrual_stopped_active_elapsed_us
accrual_stopped_at / accrued_due_count / next_due_ordinal / version / created_at / updated_at
```

`channel_view`保持现有单`target_channel_id/target_input`合同；创建/PATCH必须恰好解析一个OperationTarget，禁止在同一Task内隐式混入多个peer。ledger首次规划时冻结initial selected source set；`dynamic_new/listen_new_messages`后续只append新target row，不删除或重排旧row。target创建时`lifetime_remaining_at_attach=max(per_message_total_view_target-existing_lifetime_distinct_fact_count,0)`，`effective_target_count=min(per_message_daily_view_target,lifetime_remaining_at_attach)`。一个`ViewRemoteFact`只能由造成该远端效果的单一Task/due unit通过唯一binding确认；其他Task在attach前看到的fact只减少`lifetime_remaining_at_attach`，attach后出现的fact只把对应`(target_peer_id,channel_message_id,account_id)`从候选身份集移除并可能形成structural shortfall，绝不进入本target的MaterializedSet、confirmed或settlement，也不改写已冻结target。消息达到`message_active_days`时，只停止该row后续due增长并在同一CAS冻结`accrual_stopped_at/accrual_stopped_active_elapsed_us/accrued_due_count`；此前已经累计到期的分母不得消失。配置变更不改写current row，只影响满足PATCH生效期的新消息或下一ledger。

`next_due_ordinal` 只在同一 target row 上以 `expected target/version/next ordinal` CAS 分配。`DueSet_m={1..due_m}`；Planner可以有界补建缺少的 obligation rows，但 E4 required始终来自 DueSet而不是现有行数。存量 current-ledger obligation按 `remote fact/Gateway evidence优先 -> scheduled_at -> created_at -> id` 规范排序分配ordinal；冲突、超出 target或证据错绑进入 migration blocker，不猜测覆盖。

### 3.1.1 版本化合同哈希

所有参与CAS、幂等重放、takeover、settlement、cursor或Release Gate的hash统一使用`channel_view_contract_hash_v1`，禁止API、worker、CLI各自拼接。canonical serializer固定为：hash输入先写ASCII前缀`channel_view_contract_hash_v1\n`；object key、set member与row identity按UTF-8 bytewise/C序；字符串NFC后UTF-8；整数用无前导零十进制；boolean/null用规范JSON字面量；timestamp统一UTC RFC3339六位微秒`Z`；禁止float、locale、本地时区、数据库默认JSON顺序和进程对象repr。结果为canonical bytes的SHA-256小写hex。所有gate-critical owner旁必须持久`hash_contract_version=channel_view_contract_hash_v1`；版本缺失/未知/不相等时fail-closed，不能“尽量兼容”或重算覆盖旧hash。

registry冻结各类最小payload，任何新增业务字段若影响对应守恒必须先升级hash版本：

- target-set：logical Task/ledger/route identity、period/deadline/timezone revision及排序后的`target_operation_target_id,target_peer_id,channel_message_id,source_revision,target_revision,effective_target_count,active_until`；
- due-set/read-model：target-set hash、clock/segment-set version+hash、规范as-of、每target due count/ordinal边界、source projection与settlement版本；
- materialized/matching：每个gap due key；候选lifetime identity及owner/fact version；账号slot identity/time/capacity revision；输出`due key -> peer-message-account-slot`映射及evidence class/version。heartbeat、lease、display name、进程ID和可重建stats明确排除；
- source/expiry：logical source、collector epoch、observation/policy revision、event/delta/subscription/fanout identity+count+payload hash；target identity、active_until、schedule/activation operation version与expected/applied count；
- settlement：target input version、clock deadline snapshot、SettledDue/OnTime/Late/Unproven/Unknown/KnownShortfall各identity set count+hash、projection barrier和最终status；post-settlement history排除；
- inventory/fleet：tenant policy/cutoff、每个logical Task membership identity、frozen/allowed Task lifecycle/config/domain versions和item state；runtime heartbeat排除；
- takeover/manifest/checkpoint：inventory item、frozen class、Task/ledger/route/source-fence/static revision vector、规范manifest item identity+payload hash、chunk range/input/output count+hash；
- fact/binding/observation/blocker/tombstone：canonical remote identity、logical owner/requested due identity、binding/occurrence/resolution revisions及归档identity sets/count；导航FK是否已SET NULL不改变logical identity。

route activation hashes、ReadModelRevision、SourceProjection/Event/Fanout、ExpiryActivationOperation/Schedule、SettlementOperation/TargetItem、FleetPolicy/InventoryItem、TakeoverOperation/Manifest/Checkpoint、ContractTombstone及E4 artifact都同时保存并回读该version。实现只允许一个共享serializer/registry helper，并以跨Python进程、不同locale/timezone、API/worker/CLI相同golden vectors验证；版本漂移、count相同但identity set不同、字段遗漏或额外未登记字段都必须阻断activation/settlement/replay。

### 3.2 当前到期量

每个current route同事务创建唯一`ChannelViewAccrualClock(route_id unique,task_day_ledger_id,state=running|paused|stopped|closed,imported_baseline_active_us,accumulated_active_us,current_segment_seq,segment_set_hash,version)`，并以append-only`ChannelViewAccrualClockSegment(clock_id,segment_seq,started_at,ended_at nullable,active_us nullable,stop_reason=paused|stopped|deadline|route_closed,version)`记录每段running区间；唯一`(clock_id,segment_seq)`且每clock至多一条`ended_at IS NULL`。只有Task/route从非running真实进入running时append新open segment；pause/stop/close的CAS winner以精确业务边界关闭当前segment并推进clock summary/hash。重复命令、Planner tick和进程时间都不能推进clock。`clock_elapsed_at(t)`的权威值是imported baseline加所有segment与`[-∞,t]`交集时长，summary只是可校验缓存；这样可在event/recovery延迟后仍精确回算deadline或message active_until，暂停/停止期间业务时间冻结，resume/start-after-stop继续原进度而不是按wall-clock追债。

对当前ledger的每个`ChannelViewDailyMessageTarget m`，唯一`ViewDueSnapshotAssembler`在同一数据库快照读取clock与target并计算：

```text
target_m = frozen effective_target_count
effective_as_of_m = min(database_now, ledger.deadline_at, active_until when non-null)
clock_elapsed_m = clock_elapsed_at(effective_as_of_m) from immutable segments
stop_elapsed_m = accrual_stopped_active_elapsed_us when non-null else clock_elapsed_m
active_elapsed_m = max(stop_elapsed_m - baseline_active_elapsed_us, 0)
remaining_m = max(target_m - baseline_due_count, 0)
virtual_as_of_m = min(pacing_anchor_at + active_elapsed_m, ledger.deadline_at)

if active_elapsed_m = 0 or remaining_m = 0:
    due_m = baseline_due_count
else:
    due_m = min(target_m,
                baseline_due_count +
                max(1, floor(remaining_m * curve_weight(pacing_anchor_at, virtual_as_of_m)
                                   / full_ledger_curve_weight)))
```

浏览的period是ledger冻结的任务本地自然日，`full_ledger_curve_weight`仍取完整业务区间；晚启动或恢复不把整日目标压缩进剩余wall-clock窗口。正常新target的`baseline_due_count=0`，`baseline_active_elapsed_us`取创建事务clock as-of值；takeover target以manifest时权威DueSet作为baseline并冻结同一clock基线，因此激活不重算历史。baseline精确时刻严格不增长，只有Task处于running且数据库业务时间真实推进后才允许`max(1,...)`。pause/stop关闭segment，故停顿不累计due；resume/start-after-stop只开新segment，不能瞬间补齐停顿债务。expire fanout无论何时处理都按`active_until`回算并冻结elapsed/due；settlement无论何时恢复都按`ledger.deadline_at`回算、在final CAS保存clock version/segment-set hash/elapsed，并以deadline而非recovery now关闭旧route segment。过期row的`due_m`固定等于CAS冻结的`accrued_due_count`，不能因它不再出现在latest/active查询而归零。所有时间用数据库时间、ledger冻结时区和同一clock segment hash，不用E4进程本地日期。Assembler由Planner、详情读模型、settlement和E4共用，禁止四处复制公式。

对每个 message，在同一快照按 due ordinal、义务/Action/Attempt/fact anti-join 得到互斥集合：

- `F_m`：有精确、且唯一binding明确归属于本target due unit的`ViewRemoteFact`；其他Task的fact不在本集合；
- `U_m`：Gateway 已开始且结果 unknown、必须防重复的 due units；
- `G_m`：Gateway 已开始、仍在途且无终态事实的 due units；
- `R_m`：同remote identity已存在其他bound owner，当前request已终结为`remote_identity_conflict`且等待受保护resolution的 due units；
- `A_m`：有当前 source/target/lifecycle/materialization version、`scheduled_at < deadline_at`，且同一`ChannelViewLifetimeIdentityOwner`正处于`pre_gateway`并精确指向该Action/request的有效pre-Gateway due units；无全局owner、精确deadline或已被其他Task事实/hold抢占的Action不可调用也不抵扣gap；
- `X_m`：failed/skipped/cancelled 且已安全释放、无 remote fact 的历史绑定，不占当前量；
- `I_m`：当前可推进、canonical fact不存在且全局`ChannelViewLifetimeIdentityOwner`不存在或state=`available`的 `(tenant,target_peer_id,channel_message_id,account_id)` distinct身份；不同频道相同message ID互不冲突，legacy/current Task都必须竞争同一owner CAS，其他Task事实只在同peer身份上排除candidate而不完成本target；
- `S`：当前ledger所有候选账号在deadline前的离散合法执行时隙，每个slot只能绑定一个due unit。

```text
DueSet_m = {ordinal | 1 <= ordinal <= due_m}
MaterializedSet_m = F_m ⊎ U_m ⊎ G_m ⊎ R_m ⊎ A_m
MaterializationGap_m = DueSet_m - MaterializedSet_m
LedgerGap = disjoint_union(MaterializationGap_m for every target m)
reachable_capacity = maximum_cardinality_matching(LedgerGap, {(I_m, S) with same tenant/peer/message/account identity})
structural_capacity_shortfall = |LedgerGap| - reachable_capacity
```

`R_m`是已落`remote_identity_conflict` observation但尚未完成受保护resolution的distinct due unit。证据优先级固定 `F > U > G > R > A`，每个ordinal只能进入一个集合。集合必须来自同一 snapshot/anti-join，禁止分别count后相减导致fact、unknown、identity conflict和Action重叠。future target、其他ledger、`X_m`不抵扣当前due；expired target保留已冻结accrued due。最大匹配必须在整个ledger上执行，不能逐message分别复用同一个账号时隙；最大cardinality相同时按`target created_at/message id/due ordinal/account id/slot time`的C序最小成本结果唯一化并保存input/output hash。Planner提交后仍有可匹配但未进入MaterializedSet的unit是`channel_view_due_unmaterialized`；无合法匹配的unit进入typed structural shortfall，两者分列。

### 3.3 远端事实、绑定与时间口径

`ViewRemoteFact`继续按remote identity append-only，additive保存`remote_effect_at/confirmation_time_basis/projection_contract_version/required_projection_kinds/required_projection_count/required_projection_set_hash`。当前registry `channel_view_fact_projection_v1`至少含`view_obligation,view_target,view_read_model`，若owner已归档则改用`channel_view_contract_tombstone`；kinds按UTF-8/C序规范化。Tx C必须与fact同事务insert声明集合中的全部ProjectionState，settlement按fact声明逐kind anti-join并校验count/set hash，缺行、多行、额外未登记kind或空集都不能判projected。只有同Attempt在Telegram Gateway确认view成功时提交的数据库时间，或远端协议明确返回的效果时间，才是authoritative；reconcile/row `created_at/observed_at/projected_at`不能伪装远端效果时间。

`ChannelViewRemoteFactBinding(fact_id unique,view_fulfillment_obligation_id nullable,binding_state=bound|unbound_conflict,timeliness=on_time|late|unproven,version)`与新canonical fact同事务insert-or-read。`bound`对obligation partial unique；fact和obligation都只能有一个bound owner，不存在“一fact投影多个Task target”的兼容路径。物理lifetime unique意味着同remote identity不会append第二条fact：每次Gateway/remote结果另append `ChannelViewRemoteFactObservation(fact_id,request_identity,evidence_identity,requested_obligation_id nullable,logical_requested_task_id,logical_task_day_key,logical_daily_message_target_key,due_ordinal,requested_contract_tombstone_id nullable,classification=new_canonical|idempotent_same_owner|remote_identity_conflict,evidence_hash,observed_at)`，以request/evidence identity永久唯一；exact重放只回读。`requested_obligation_id`只是nullable navigation FK，删除前RESTRICT/backfill完成后改`ON DELETE SET NULL`，逻辑requested due key永久保留。若canonical fact已存在且请求同一bound owner，observation为idempotent；若请求另一个due unit，observation winner在同一规范事务把本request的Action/Attempt/ActionBinding收口为typed `remote_identity_conflict`、obligation转同名state并打开enrollment blocker，既不新建fact/binding也不确认或立即释放请求unit。该state进入`R_m`占位，避免blocked期间再次物化；`observation.logical requested due key + fact_id`同时就是该due永久排除该peer-message-account identity的事实，不另造可漂移的forbidden集合。

source-identity resolution只有在权威fact/remote identity/request时间证据证明canonical owner先于本request、当前观测没有产生另一可计量remote identity时，才可在owner→blocker→obligation/binding锁序中把原ActionBinding保持terminal、把obligation `remote_identity_conflict -> unmaterialized`、`materialization_version+1`；随后matcher必须按上述永久observation事实anti-join该due与fact的peer-message-account identity，同due只能匹配另一未使用账号。database time已到deadline则改`terminal_shortfall(remote_identity_conflict)`，进入known shortfall，不重开。证据不足保持R_m+open blocker；contract_reopen只消费已applied resolution解除owner blocker，不再改due。全程不改canonical fact/bound owner、不复用同identity，generic retry/reset无权执行。

只有**不同remote identity**却请求确认已被另一fact bound的同一due unit时，第二个canonical fact仍正常append，其binding为`unbound_conflict`并打开enrollment-scope blocker；不能丢事实、谎绑另一个ordinal或自动重发。timeliness按`remote_effect_at < ledger.deadline_at`且basis authoritative判on_time；无可证明时间为unproven，deadline后为late。late/unproven更新其owner Task历史和全局lifetime identity占用，但不确认其他Task，也不把immutable missed settlement改成met。

canonical浏览fact与LifetimeIdentityOwner都是跨Task lifetime排除真相，绝不随owner Task/obligation物理删除。迁移把现有`ViewRemoteFact.obligation_id`级联外键改为nullable navigation FK `ON DELETE SET NULL`（在logical identity backfill与约束readback完成前先RESTRICT），并在fact/binding冻结`tenant_id,target_peer_id,channel_message_id,account_id,logical_owner_task_id,logical_task_day_key,logical_daily_message_target_key,due_ordinal,owner_contract_tombstone_id nullable`。逻辑identity/hash为含tenant的四元组，但物理防重保留现有`UNIQUE(target_peer_id,channel_message_id,account_id)`，并以FK/写门禁保证`fact.tenant_id=account.tenant_id`；不重建一套语义不同的fact unique。binding、observation与LifetimeIdentityOwner的Task/obligation导航FK同样按RESTRICT→SET NULL迁移，逻辑owner/due key和transition identity永久保留。Task delete operation必须先创建不可变`ChannelViewContractTombstone(logical_task_id,enrollment/route/ledger/source/target/due/fact/binding/observation/lifetime-owner/lifetime-transition identity-set counts+hashes,deleted_at,version)`；同request可证未transport的pre_gateway owner才可在delete adoption中安全转available，call-issued/unknown/confirmed/conflict owner必须永久保留。operation回填fact/binding/observation/owner/transition tombstone pointer并证明现有三元物理unique、四元逻辑set hash与全部count覆盖，之后才SET NULL导航FK并删除runtime义务；所有attach、最大匹配、Gateway和E4始终查询canonical fact+global owner，不以Task/obligation FK是否为空决定identity可用。late Tx C/reconcile或重复观测继续按tombstone逻辑requested/owner key推进同一owner并append observation/projection，只读历史，不复活Task或释放lifetime identity。

Tx A在数据库短事务先锁全局LifetimeIdentityOwner并CAS同owner/request到call-issued，再创建Attempt/journal/绑定hold并提交；外部view调用无数据库事务；Tx C先对owner做expected-version no-op CAS取得最前层行锁，进入Task/route层后append fact、binding/observation及规范required ProjectionState，最后在仍持有的owner锁上写终态transition；这样`remote_fact_id`可使用普通即时FK而不要求先引用尚未insert的fact，也不会形成RemoteFact→Owner反锁。`view_obligation` projector还必须按remote identity查唯一global owner：若canonical fact到达时owner属于其他due且仍为pre_gateway，按expected owner/binding/obligation version安全terminalize旧Action，并在**同一事务**把owner从旧pre_gateway直接转移为`confirmed + owner_kind=remote_fact + remote_fact_id`，绝不能出现中间`available`提交；原due转unmaterialized、递增materialization version并永久排除该identity。若旧owner已call-issued/unknown，则走上述`remote_identity_conflict` observation+blocker，但canonical fact仍以最高优先级把global owner终态写为confirmed，冲突request保留在ActionBinding/obligation/blocker/transition中，不把owner降为available；尚无canonical fact的多hold守恒冲突才使用永久`conflict`状态。fact、owner、projection state与该有界收口同一幂等projector contract，settlement anti-join会等待它完成。调用已发出但结果不明只能unknown/reconcile，禁止按超时回`unmaterialized`。safely-not-executed只接受同request的明确pre-transport证据，并且所有重开/materialize/Tx A/外部调用前最后owner CAS都必须在数据库时间满足`now < deadline_at`。

## 4. Planner 与排期修复

### 4.1 禁止 future-tail 锚点

浏览新批不得读取 `max(open scheduled_at, latest success executed_at)` 后整体平移。已有 future Action 只占自己的 due unit 与账号级时间约束；它不占用整个 Task 此前或此后的时间区间，也不阻止其他 distinct 账号物化。

Planner先在只读快照对`MaterializationGap`按`target/source revision -> due ordinal`稳定排序并做最大匹配，只产出候选edge`(target,due_ordinal,account,slot)`，此阶段不锁/写due unit或owner。每条edge用独立短事务，第一层先insert-or-CAS精确`ChannelViewLifetimeIdentityOwner available->pre_gateway`，再按§6.4进入Task/route/target并insert-or-read唯一due unit、绑定ActionBinding/Action；due/version/资格任一CAS失败整事务回滚，包括owner transition，不留下pre_gateway孤儿。并发重复只能有一个owner+due winner；当前edge失效后继续只读结果中的其他合法edge。若未来优化预建due unit，预建事务必须完全不触达global owner/Action，真正物化仍按owner-first执行。

### 4.2 浏览节奏粒度

- Task级由当前ledger的curve/hour bucket给每个due unit生成`preferred_at`，`task_pairwise_min_gap=0`；不再对`channel_view`使用template 180秒Task全局串行，也不调用append-after-latest reservation；
- 账号级 `(task_id,account_id,view_message)` 使用当前配置解析并冻结的 `account_min_gap`，授权级真实FloodWait/限流可以跨Task延后该账号。不同账号Action可具有相同`scheduled_at`，Dispatcher以`scheduled_at,task_id,obligation_id/action_id`稳定排序；
- 对每个匹配edge，从`max(database_now,preferred_at)`扫描该账号已有同日future reservations前、中、后的合法空隙，同时满足account gap、quiet-hours、显式bucket和deadline。找到后以due unit materialization version CAS绑定；冲突回读重算。禁止只看max future、禁止整批平移、禁止改写旧future Action；
- 账号自身的授权、Session、代理、FloodWait、全局硬容量和 Gateway inflight 在 claim/Gateway 前复核；这些硬边界不得转成 Task 全局 180 秒间隔；
- 同一 `(target_peer_id,channel_message_id,account_id)` 已有 open/unknown/fact 时永远不补第二条。明确且可证明的 pre-Gateway 失败只重开原 due unit、递增 materialization version，并可按新快照匹配另一合法账号；旧 Action/Attempt不改写；
- 已存在的 23:57 future Action 不批量提前、不删除、不改写；新批直接使用当天仍合法的软时间，避免历史 Action 尾部继续制造饥饿。

排期输出少于可物化候选时必须记录 `scheduling_capacity_shortfall` 和被截断身份数；不能返回 0 且保持空 `last_error`。这不是降低 due，后续 tick 仍重评估原身份，deadline 后进入 ledger `missed/shortfall`。

### 4.3 批次与公平

浏览 Planner 一次只处理本轮从真实数据库空闲槽得出的有界候选集合，提交后轮转其他 Task；批次上限只能来自既有 Planner/interaction 实际空闲槽和 claim batch contract，不能用 `messages_per_round`、task template 间隔或“最晚 future Action”充当数量上限。不得一次预建 future target，也不得每轮只固定建 1 条。

## 5. 来源状态

频道来源状态闭集：

| 状态 | 判定 |
| --- | --- |
| `source_ready` | 冻结/append来源集中至少一个仍可继续累计due的消息 |
| `waiting_for_source` | 最近一次频道采集成功，当前规则窗口确实没有 active message |
| `listener_stalled` | 本轮应采集但缺账号、Gateway/Session/权限失败，或最新持久采集结果为失败，不能证明没有新消息 |
| `source_unresolved` | 已采集消息但目标/revision/权限无法绑定 |
| `source_empty_terminal` | finite initial selector已由fresh成功poll证明为空且`listen_new_messages=false`；零TargetSet但配置意图未履约，任务日结算为`missed_no_source`而不是met |
| `source_window_expired_shortfall` | 来源窗口已结束，但TargetSet/DueSet仍有未完成unit |
| `source_completed` | 有限来源集合全部TargetSet已由typed fact完成且无unknown/shortfall |

现有`ListenerSourceState(source_type='channel',source_peer_id=OperationTarget.id,account_id=collector)`只作某collector的采集证据，不再作为订阅identity。新增tenant+频道逻辑owner `ChannelViewListenerSource(tenant_id,operation_target_id,source_peer_id,current_collector_account_id nullable,collector_epoch,observation_version,last_heartbeat_at,last_cursor_verified_at,last_cursor_advanced_at,last_success_poll_at,last_poll_result=nonempty|empty|failed,message_high_water_id,message_high_water_revision,last_event_at,last_remote_message_id,last_error,collect_window_seconds,version,updated_at)`，物理唯一固定为`(tenant_id,operation_target_id)`；所有ledger subscription只绑定该逻辑source。collector Session/权限切换以expected source/collector epoch CAS更新同一owner并递增collector_epoch，旧/新`ListenerSourceState`仅作为event evidence，不换subscription key。worker活着但未poll只推进heartbeat；成功poll无论非空或空都推进`observation_version/last_success_poll_at/last_cursor_verified_at/last_poll_result`并append对应Event，只有high-water实际前进才推进诊断字段`last_cursor_advanced_at`；失败poll写typed error但不伪造success/cursor。进程内`should_collect_listener()`只能防抖，不能作为E4真相。

freshness读取闭合合同中的版本化`listener_freshness` policy并冻结`heartbeat_stale_after/cursor_stale_after/success_poll_stale_after/revision`。cursor freshness明确使用每次成功poll都会推进的`last_cursor_verified_at`，`last_cursor_advanced_at`只作“多久无新消息”诊断，不能使持续成功空poll的安静频道变stalled。任一必需时间缺失、超过对应阈值或policy缺失均为`listener_stalled`及精确reason；只有heartbeat/cursor-verified/success-poll三项都新鲜且最近poll明确成功为空，才可判`waiting_for_source|source_empty_terminal`。禁止使用代码默认阈值、容器本地时间或仅凭worker进程healthy推断fresh-empty。

每个ledger另以additive`ChannelViewSourceProjection`唯一`(task_day_ledger_id)`保存`task_id/target_id/logical_listener_source_id/listener_observation_version/collector_epoch/policy_revision/selected_set_hash/active_set_hash/source_state/source_state_reason/version/calculated_at`。selected/active hash都对规范排序的`ChannelViewDailyMessageTarget(target_peer_id,channel_message_id,source_revision,target_revision,source_state,version)`计算；target append、expire/unresolved或新的采集observation必须以expected projection version CAS重算，并同事务推进`ChannelViewReadModelRevision(task_day_ledger_id unique,current_version,target_set_hash,due_set_hash,materialized_set_hash,source_projection_version,settlement_version)`。详情/E4先验证projection、read-model与target rows/逻辑source observation+collector epoch一致；不一致报stale，不能用旧waiting掩盖worker停止。

listener每个受配置上限约束的poll事务先按规范顺序锁逻辑source与当前collector evidence，更新两者后append`ChannelViewSourceObservationEvent(logical_source_id,collector_source_state_id,collector_epoch,observation_version,event_seq,delta_count,delta_set_hash,first_delta_seq,last_delta_seq,payload_hash,state=pending|processing|completed,lease/version)`，数据库唯一`(logical_source_id,observation_version)`及`(logical_source_id,event_seq)`，并写其不可变`ChannelViewSourceObservationDelta(event_id,delta_seq,channel_message_id,source_revision,transition=message_observed|source_deleted,remote_observed_at,payload_hash)`；共享delta只表达tenant+channel层可观察事实，绝不写Task-specific `expired|unresolved`。delta唯一`(event_id,delta_seq)`与`(event_id,channel_message_id,source_revision,transition)`，按`channel_message_id/source_revision/transition`的C序计算set hash。零消息成功poll也必须有`delta_count=0`的event。这样collector切换仍沿同一observation stream，crash replay从持久delta重建，不按最新主表全量猜测消息差异，也不在listener事务内向同频道所有Task无界fan-out。

每个active ledger有`ChannelViewSourceSubscription(logical_listener_source_id,task_day_ledger_id,observed_version,state,version)`，数据库唯一`(logical_listener_source_id,task_day_ledger_id)`；bootstrap/takeover按logical source ID的C序先锁当前`ChannelViewListenerSource`，读current observation version并提交subscription/projection后才解锁，因此event-before被初始快照吸收、event-after只能在subscription可见后提交。collector切换不重绑subscription，只由下一event携带新collector epoch。recovery按event keyset/lease创建`ChannelViewSourceFanoutItem(event_id,subscription_id,state=pending|processing|completed|blocked,delta_cursor,discovered_count,applied_count,retired_count,input_hash,result_hash,lease/version)`，数据库唯一`(event_id,subscription_id)`，并逐delta有界推进；item恒等式为`discovered_count=applied_count+retired_count`后才能completed，blocked不退出热守恒。单delta事务遵循§6.4全局顺序。若在deadline前、route可写且settlement仍pending，append新target必须同事务创建唯一SettlementTargetItem、CAS operation target-input version/hash并推进source projection/read-model；settlement已processing|blocked|completed或deadline已到时把delta计retired并留给下一合法ledger，不能让新target逃逸结算。event只有规范subscription集合的全部item completed、item/discovered/delta hash readback相等后才completed；期间新增subscription若observed已覆盖该version无需补item。必须有event/delta、subscription、fanout item的partial index、lease恢复、heartbeat、count/hash守恒、collector切换与三事务event-before并发测试。

每个target创建时还必须同事务创建唯一`ChannelViewTargetExpirySchedule(daily_message_target_id unique,active_until,activation_ready,state=pending|processing|completed,next_retry_at,lease/version,result_hash)`；正常active bootstrap/dynamic target写`activation_ready=true,next_retry_at=active_until`。takeover apply固定写`activation_ready=false,next_retry_at=NULL`，并为route创建唯一`ChannelViewTargetExpiryActivationOperation(route_id unique,activation_ready=false,state=pending|processing|completed,schedule_cursor,expected_count/applied_count/schedule_set_hash,next_retry_at,lease/version,result_hash)`。class activation只需在常数级最终事务把该operation置`activation_ready=true,next_retry_at=database_now`；recovery按operation claim有界扫描manifest冻结的schedule keyset，逐行CAS schedule为ready并写`next_retry_at=max(active_until,database_now)`，全部count/hash相等才completed。它避免最终CAS无界更新所有message target，同时保证崩溃可续。schedule recovery只按`(next_retry_at,active_until,id) WHERE activation_ready=true AND state IN ('pending','processing')`领取，使用AccrualClock segments精确计算`active_until`时的elapsed/due，并按全局锁序CAS target expired、source projection/read-model与settlement input version/hash。activation operation/schedule字段与集合hash纳入bootstrap/takeover result、checkpoint/readback；preparing期不热循环也不改manifest。Task-specific unresolved由fanout应用target policy时写projection/blocker，不回写共享delta。expiry处理延迟、pause/resume、activation fan-out和settlement并发都只能有一个winner；deadline/settlement completed后schedule只记审计完成，不改immutable结果。

投影判定优先级固定：不可解析事实→`source_unresolved`；freshness不满足→`listener_stalled`；存在继续累计due的target→`source_ready`；全部窗口结束且已有TargetSet/DueSet欠量→`source_window_expired_shortfall`；fresh-empty且允许动态追加→`waiting_for_source`；fresh-empty、finite selector且禁止dynamic append→`source_empty_terminal`；非空有限集合全部typed fact完成→`source_completed`。`obligation_open/execution_blocked`属于fulfillment state，不得混入source state。

没有选中来源时不创建空目标/义务；`waiting_for_source`是正常等待但不能报任务完成，`listener_stalled|source_unresolved`是明确blocker，`source_empty_terminal`写operation级`source_intent_shortfall_count=1/status=missed_no_source`并终结本ledger，绝不能因SettledDue=0判met。已经进入TargetSet的消息即使后来expire也保留accrued due和shortfall；来源恢复后沿用原Task/ledger规则append新来源，不重建或删除旧TargetSet。

## 6. 结构性容量与读模型

每次规划后在 `Task.stats.channel_view_runtime` 写可重建快照缓存，详情/E4的权威值仍由 ledger、message、obligation、Action、Attempt、fact和当前资格事实重算：

```text
task_day_ledger_id / calculated_at / source_state
active_message_count / expired_message_count
expected_due_count / materialized_count
confirmed_count / unknown_hold_count / valid_open_count
materializable_deficit_count / due_unmaterialized_count
eligible_distinct_identity_count / structural_capacity_shortfall_count
scheduling_capacity_shortfall_count / deadline_at
```

`capacity_warning` 兼容字段保留展示，但不能替代 typed counts/codes。结构性不足时 Task 继续处理 `I_m` 中可执行身份，不能因目标大于池子就返回 0；耗尽后保持 `structural_capacity_shortfall`，账号事实变化会唤醒重评估。不得自动修改每日/累计目标。

结构短缺集合至少按互斥首因拆分：`view_unique_account_shortfall|account_time_slot_shortfall|source_window_expired_shortfall|explicit_task_policy_shortfall`。它们来自due unit到账号-时间slot最大匹配的unmatched集合；不减少TargetSet/DueSet或confirmed，deadline时进入settled known shortfall。来源窗口过期时`effective_target_count-accrued_due_count`只记`unaccrued_target_expiry_gap`产品风险，不进入DueSet或settled shortfall；已累计DueSet继续守恒到deadline，不能以该风险字段掩盖欠量或反向扩充required。

### 6.1 Deadline settlement

每个有ledger的浏览Task在ledger bootstrap或takeover apply同事务建立唯一`ChannelViewSettlementOperation(task_day_ledger_id unique,activation_ready,state=pending|processing|blocked|completed,deadline_at,next_retry_at,target_input_version,input_hash,source_intent_shortfall_count,aggregate_status=met|missed|closed_with_unknown_shortfall|missed_no_source,aggregate_due/on_time/late/unproven/unknown/known_shortfall counts,aggregate_set_hash,result_hash,settled_at,lease_owner/epoch/expires_at,version)`。每个message target有唯一`ChannelViewSettlementTargetItem(operation_id,daily_message_target_id,state,unit_cursor,pre_gateway_discovered_count,pre_gateway_safe_released_count/pre_gateway_safe_released_set_hash,issued_or_unknown_preserved_count/issued_or_unknown_set_hash,drain_input_hash,drain_complete_at,settled_due_count,settled_due_set_hash,on_time_count/on_time_set_hash,late_count/late_set_hash,unproven_count/unproven_set_hash,unknown_count/unknown_set_hash,known_shortfall_count/known_shortfall_set_hash,status,projection_barrier_hash,settled_at,result_hash,lease/version)`；drain字段按chunk expected-version CAS推进，final结果字段只允许final CAS写一次，是API/E4的immutable settlement owner，不能在late fact到达后从可变业务表重算。正常bootstrap在route原子active时写`activation_ready=true,next_retry_at=deadline_at`；takeover apply固定写`activation_ready=false,next_retry_at=NULL`，最终enrollment/route activation CAS同事务核对完整target/item/input hash后写`activation_ready=true,next_retry_at=max(deadline_at,database_now)`并纳入result hash。recovery不论Task是running/paused/stopped都只按`activation_ready=true AND next_retry_at<=database_now`的deadline keyset领取；preparing期即使deadline已过也不能提前写immutable settlement或形成NULL永久漏领。没有obligation row的due ordinal仍必须由target的隐式DueSet结算，不能因Planner未物化而漏掉；finite empty source以operation级source shortfall收口，不能伪造target item。

deadline分owner-first有界drain与不再触达identity owner的finalize。recovery先只领取SettlementOperation/Item lease并提交；每个item按`due_ordinal,id`发现下一批A_m，随后严格按§6.4先锁对应LifetimeIdentityOwner/Transition（peer/message/account C序），再锁Task/enrollment/route→SettlementOperation/Item→target→obligation/ActionBinding/Action→journal/read-model。owner仍pre_gateway且同request无call-issued/journal mutation证据时，同事务写owner`pre_gateway->available` transition、Binding/Action terminal、obligation=`terminal_shortfall(deadline_not_issued)`并推进safe-release count/set hash/cursor；Tx A先赢、owner已call_issued或unknown/fact存在时只归入preserved set并进入unknown/reconcile/on-time分类，绝不释放。CAS失败整chunk回滚；其他Task Planner也先抢同owner，不存在available双占。全部A_m满足`discovered=safe_released+issued_or_unknown_preserved`且set hash/Binding/transition readback一致后，finalize才按`Task/enrollment/route -> AccrualClock/open Segment -> SettlementOperation/Item -> ContractBlocker -> SourceSubscription/Projection/Expiry -> target -> obligation/action binding -> fact binding/projection state -> read-model`关闭deadline clock并写immutable结果，此阶段不得再写owner。drain未complete、count/hash不守恒或仍有pre_gateway owner时final CAS失败。projection retry/poison仍按typed合同pending/blocked并可重放。

每个target在同一快照冻结：

```text
SettledDue = {1..due_at_deadline_or_frozen_accrued_due}
OnTime = bound facts with authoritative on-time evidence and fact committed before settlement linearization
Late = bound late facts already present at linearization
Unproven = bound facts without authoritative time
Unknown = gateway inflight/unknown holds without terminal fact
KnownShortfall = SettledDue - OnTime - Late - Unproven - Unknown

settled_shortfall_count = |SettledDue| - |OnTime|
status = closed_with_unknown_shortfall  if |Unknown| + |Unproven| > 0
         missed                          if |Late| + |KnownShortfall| > 0
         met                             otherwise
```

五个集合互斥并满足`|SettledDue|=|OnTime|+|Late|+|Unproven|+|Unknown|+|KnownShortfall|`。settlement完成后结果、DueSet/hash和状态不可改；后到fact仍占lifetime identity并写late/unproven历史，但不把missed/closed_unknown改成met。continuous dynamic任务的“本日met”只说明本ledger已结算，不代表任务永久完成。

`ChannelViewLedgerBootstrapOperation(task_id,next_period_start_at,enrollment_id,request_revision,caller=first_start|resume_rollover|start_after_stop_rollover|automatic_running_rollover,state=pending|processing|blocked|completed,next_retry_at,lease_owner/epoch/expires_at,expected_task/enrollment/domain-revision/previous-route+settlement versions,result_ledger_id/result_route_id/result_accrual_clock_id/result_source_projection_id/result_settlement_operation_id/result_target_set_hash/result_subscription_set_hash/result_read_model_version/result_hash,version)`以`(task_id,next_period_start_at)`数据库唯一，是新任务日完整bundle的唯一持久owner。所有用户`first_start|start_after_stop`（同period或rollover）都由one-current-row`TaskStartOperation`外层持有，并与Task/route/clock或完整bootstrap结果同事务写/回读ledger、route、route epoch、target-set、lifecycle与result hash，不能只回读ledger ID或让内层已提交而StartOperation仍processing；replace/stale/crash遵守共享start合同。只有持续running automatic rollover不创建/改写TaskStartOperation，只提交BootstrapOperation request由recovery领取；resume继续由lifecycle command持有。

bootstrap winner在一个事务内按§6.4创建或回读完全相同的：`TaskDayLedger + whole target-operation set + per-ledger route + AccrualClock/初始segment + ChannelViewListenerSource的observation/collector epoch/high-water/delta-set快照 + SourceSubscription + SourceProjection + initial selected ChannelViewDailyMessageTarget rows + TargetExpirySchedule + activation_ready=true SettlementOperation及每target item + ReadModelRevision`，随后写route activation hashes、enrollment current-route pointer、Task/TaskStartOperation与BootstrapOperation完整result。collector-specific ListenerSourceState只作审计证据，绝不能成为subscription identity。initial source为空也必须创建带typed waiting/stalled状态的projection、subscription、零item settlement和read-model；禁止active route缺source/settlement owner。任一已存在identity的payload/hash不同使operation blocked且整事务零半成品；提交后按result hash逐对象readback。旧route settlement未completed或两层blocker非0时保持pending/blocked且零新ledger。pause/stop/PATCH赢导致无result的request stale时，可按operation/version把旧request revision审计supersede并冻结新expected版本；任何bundle对象已产生后只允许完全相同结果回读。claim索引固定`(next_retry_at,next_period_start_at,id) WHERE state IN ('pending','processing')`并覆盖lease回收、双worker和每个bundle crash point replay。

### 6.2 API、前端与生命周期

- 不新增运营目标字段。创建/create-and-start继续使用现有单频道target、initial scope、listen-new、daily/total target与active days；结构合法即可创建，账号容量或fresh source不足只进入typed runtime状态；
- 任务详情增加`source_state/source_reason/target_row_count/effective_target_total/expected_due/materialized/on_time/late/unproven/unknown/due_unmaterialized/structural_shortfall/settlement/route_epoch/read_model_version`，分别展示“等待新来源”“采集异常”“有到期量未物化”“账号身份不足”，不能都显示为运行中或普通失败消息；
- `GET /api/tasks/{task_id}/daily-fulfillment`沿用ledger summary并增加上述view聚合；新增`GET /api/tasks/{task_id}/channel-view/targets`与`.../due-units`作只读下钻，固定keyset分别为`target created_at,id`和`target_id,due_ordinal,id`。typed DTO/state filter必须包含闭集中的`remote_identity_conflict`，不能降为generic failed/unknown。cursor HMAC绑定tenant/task/ledger/enrollment/current route epoch/read-model version、规范化filters/state/reason/account、limit/order和last key；每一页在单个repeatable-read snapshot先校验cursor versions再查行，任一漂移返回409 `channel_view_snapshot_changed`，不能混页或OFFSET；
- running/paused/stopped Task不得修改target channel或initial source selector；返回`409 channel_view_current_source_immutable`。`listen_new_messages`从PATCH提交后的source-policy revision生效，不删除旧target；per-message daily/total/active-days与软curve-jitter只影响PATCH提交后新append的message target和下一ledger，current target rows不改写；timezone、schedule与base curve只在下一ledger生效。固定技术cap低于1,000,000、试图启用Task级view间隔、`execution_mode=burst`或改current target返回422并写Audit；

generic Task PATCH与`/group/channel-view`专用PATCH必须共同调用唯一field-family决策器，并以`ChannelViewTaskDomainRevision(task_id unique,next_ledger_revision/next_ledger_snapshot_hash,future_target_policy_revision/future_target_policy_hash,source_policy_revision/source_policy_hash,account_scope_revision/account_scope_hash,display_version,version)`持久分域；禁止把通用`Task.config_revision`同时冒充所有域。字段合同为：

本release与AI专项共享同一个 additive `tasks.version BIGINT NOT NULL`通用row-version migration：历史行在线keyset回填为1，所有Task status/display/lifecycle/config-pointer/delete-fence入口都必须以expected `Task.version` CAS并由winner推进；频道的domain revision只承载分域业务版本，绝不能替代Task row version，也不能拿现有`config_revision`冒充。schema/migration只创建一次，AI与channel服务共同复用；任一遗留Task直接赋值入口未接shared transition即Release Gate失败。

HTTP/前端合同固定为`TaskOut.task_version=Task.version`必填；generic与channel-view专用PATCH及start/pause/resume/stop/delete命令统一携带`expected_task_version`，create-and-start第二事务只用create返回的version。成功响应返回新version，stale统一`409 task_version_conflict + current_task_version`且零domain/route/clock写；后端不得读最新版代填。任务详情表单与生命周期按钮提交当前version，409时刷新详情并要求重新确认。批量、内部调度与TaskStartOperation同样不能绕开expected-version CAS。

| field family | current enrollment语义 |
| --- | --- |
| `name/priority` | 只推进display/Task row version，不唤醒due/source |
| `target_channel_id/target_input/message_scope/initial_message_scope/latest_message_count/message_count/date_from/date_to/message_ids` | first-start前可改并推进next-ledger；已有ledger或running/paused/stopped固定409，不能跨peer改绑历史，也不能用`latest_n`重排已冻结initial set |
| `timezone/scheduled_start/scheduled_end/max_duration_hours/base curve/pacing_config` | current ledger/target/accrual clock不可变；只写规范next-ledger snapshot/revision，bootstrap原子消费。若请求声明current生效则409，不静默延后 |
| `per_message_daily_view_target/per_message_total_view_target/message_active_days/view_count_jitter` | 推进future-target-policy revision；只由提交后新append target和下一ledger消费，既有target的effective target、active_until、DueSet与settlement input不改。`view_count_jitter`只作为同一curve bucket内的确定性软排期偏移，不改变target/DueSet cardinality、账号硬slot或deadline |
| `target_views_per_message` legacy alias | 只在API规范化边界映射一次到`per_message_daily_view_target`；与canonical字段同请求且值不同返回422，值相同只推进一次future-target revision。不得覆盖total target或在持久层保留第二个可写真相 |
| `listen_new_messages/dynamic source policy` | 只对提交后的observation生效，推进source-policy revision；不删旧target。关闭dynamic导致finite empty时同事务更新projection/settlement input为`source_empty_terminal` |
| `account_config/account scope/admission` | 推进current account-scope revision，只影响后续最大匹配edge；不改DueSet、旧binding或fact |
| `execution_mode/task-global view gap/hard-hourly/legacy retry/backoff/failure_policy` | current合同只接受`execution_mode=distribute`；`burst`及其余字段422/409 typed拒绝，不得恢复180秒tail、压缩full-period DueSet、generic Action resurrection或覆盖typed recovery |
| 技术batch/claim字段 | 只约束真实空闲槽且不得改变DueSet、target或删除structural shortfall；低于平台安全下限的旧cap拒绝 |

mixed PATCH先对全部字段做规范化、权限、来源状态和expected Task/domain/enrollment/route版本校验，再在单事务只推进命中的domain revisions；任一字段非法则整请求零写入。Task/domain revision、next-ledger snapshot、source projection/settlement input与inventory open-item allowed epochs必须按§6.4同序CAS并写Audit。direct service、generic API、type-specific API与create-and-start不能绕过该决策器；并发PATCH只有expected-version winner，失败返回稳定409/422而非部分成功。

- pause/stop只有Task状态真正变化的CAS winner在同事务推进lifecycle epoch、关闭唯一open accrual segment、把current route/clock改paused/stopped，并创建唯一`ChannelViewLifecycleAdoption(task/enrollment/route,from-to epoch,command,state=pending|draining|ready|blocked,discovery_complete,cursor/pending/processing/deferred/blocked/completed counts,next_item_seq,lease/version)`，数据库唯一`(task_id,to_epoch)`；命令事务不无界扫描。`ChannelViewLifecycleAdoptionItem(adoption_id,item_seq,trigger_kind,trigger_identity,obligation_id/action_binding_id nullable,state=pending|processing|deferred|blocked|completed,latest_safe_evidence_id nullable,lease/version)`同时唯一`(adoption_id,item_seq)`与`(adoption_id,trigger_kind,trigger_identity)`；item_seq由owner expected `next_item_seq/version` CAS分配，claim固定按`item_seq,id`。recovery initial discovery按old epoch keyset覆盖所有非终态unit：`unmaterialized`直接迁epoch；未call-issued active binding安全终结/释放后迁epoch；Gateway hold保留旧epoch只reconcile并记deferred；不存在“只扫Action而漏掉无Action义务”的分支。blocked item同事务打开enrollment-scope`lifecycle_conservation_blocked`。只有discovery complete且pending/processing/blocked均0才ready，deferred可大于0。

  同request后来取得safely-not-executed时，先append或回读`ChannelViewLifecycleSafeEvidence(deferred_item_id,evidence_identity,request_identity,evidence_hash,state=observed|accepted,result_hash)`；每个evidence identity永久唯一、每个deferred item至多一个accepted。winner按§6.4锁owner+原item，以expected versions将原item`deferred -> pending`并分配大于cursor的新item_seq，写evidence pointer，同时一次性`deferred_count-1,pending_count+1,ready->draining`（或保持pending/draining）。exact重放、双证据与crash不得重复改count或新建第二item；worker再按pending→processing→completed逐步同事务搬移owner counts并迁到current epoch，processing崩溃由同item lease恢复，最后重新判ready。重复pause/stop即使新command ID也只回读，不再次推进epoch或clock；
- resume必须由lifecycle command、start-after-stop必须由one-current-row TaskStartOperation外层持有；两者都等待current epoch adoption ready（takeover paused/stopped同tx建立`imported_baseline ready`），并在Task/route/clock expected-version事务中要求当前无open segment，append下一`ClockSegment(started_at=database_now)`并推进clock seq/set hash后把writer/clock改running；pause/stop则以同一顺序关闭唯一open segment并推进summary/hash。在原deadline前沿用enrollment.current route及同ledger/target/due identity，停顿时长不进入Assembler。跨deadline先完成旧settlement、把旧route/clock closed，再原子bootstrap新ledger/route/clock并CAS enrollment pointer。持续running自然跨日由recovery持久bootstrap owner触发。generic retry/reset对current enrollment固定返回`409 channel_view_contract_managed_recovery_required`且零状态变化；只有fact conflict adjudication、projection poison resolution、safely-not-executed evidence及contract reopen等精确受保护owner可唤醒对应pre-Gateway unit。delete先归档所有identity再走全局202 operation。FleetPolicy、InventoryItem及enrollment/route最小审计tombstone不随Task级联；删除前item/enrollment CAS retired并保留fleet membership hash；
- 普通创建/PATCH/start/pause/resume/stop沿用`tasks.manage`及tenant隔离；详情沿用现有`tasks.view`。takeover preview只读使用现有`system.view`，apply/route unblock/conflict adjudication使用现有`system.manage + approval_ref + expected manifest/blocker/version`，middleware和service双检，403/404与Audit必须覆盖；
- `ChannelViewContractBlocker(enrollment_id,scope=enrollment|route,route_id nullable,kind,identity,state=open|resolved,first_occurrence_identity,snapshot_hash,resolution_id nullable,version)`由固定registry解释：`remote_fact_conflict|projection_poison|source_identity_conflict|lifecycle_conservation_blocked`只能是enrollment scope并跨日fence；`manifest_conflict`只能是尚未激活的preparing route scope。数据库分别以`UNIQUE(enrollment_id,kind,identity) WHERE scope='enrollment' AND state='open'`和`UNIQUE(route_id,kind,identity) WHERE scope='route' AND state='open'`收敛。append-only `ChannelViewContractBlockerOccurrence`保存owner/scope/kind/stable identity、`occurrence_identity=sha256(source_kind,source_identity,source_revision,snapshot_hash)`、source revision/snapshot、linked blocker与observed_at；enrollment/route scope分别以owner+kind+occurrence identity永久唯一。旧occurrence重放只回读原linked blocker，即使已resolved也不得重新加count；新source revision形成新occurrence，当前无同stable identity open行才新建blocker并加count，已有open行只链接occurrence。未登记kind/scope拒绝打开。所有open/resolve先按§6.4锁enrollment owner、route scope再锁route owner，之后才insert occurrence/blocker/resolution并在已持有owner锁下CAS count/revision；禁止blocker反锁owner。Planner/Dispatcher/bootstrap同时要求两层count=0。
- resolution按kind使用append-only typed owner：remote conflict adjudication只确认真实overage；projection poison resolution只在修复SHA重投影及恒等式通过后生效；source identity resolution只接受权威peer/message证据；lifecycle conservation resolution只在原item安全收口并readback后生效；manifest conflict只能supersede同一takeover manifest，不能由runtime reopen激活。受保护`POST /api/ops/channel-view/enrollments/{id}/contract-reopen`使用`system.manage + 独立approval_ref + expected enrollment/route blocker revisions + source/read-model/settlement hashes`，只resolve已有typed resolution覆盖的runtime blocker；它不改fact/confirmed/due/settlement、不启动Task。并发新blocker赢则整笔失败，closed旧route的enrollment blocker仍可处理，preparing route blocker永远不能借reopen变active。普通retry/reset没有此权限或副作用。

### 6.3 数据库约束与热索引

迁移先additive建列/表并backfill，再用并发unique index替换旧full unique：

- legacy：`UNIQUE(task_day_ledger_id,channel_message_id,account_id) WHERE daily_message_target_id IS NULL`；
- current due：`UNIQUE(daily_message_target_id,due_ordinal) WHERE daily_message_target_id IS NOT NULL`；
- global lifetime owner：`ChannelViewLifetimeIdentityOwner`物理`UNIQUE(target_peer_id,channel_message_id,account_id)`全域覆盖legacy/current，tenant一致性由account FK/guard证明；`state=available`只允许新owner CAS领取，`call_issued|unknown|confirmed|conflict`永久不可释放。append transition唯一`(owner_id,transition_seq)`及request transition identity；current obligation上的peer/message/account partial unique仅作本Task二级一致性，不承担跨Task防重；confirmed最终仍由`view_remote_facts`同一物理unique保证；
- current action owner：`UNIQUE(action_id)`与`UNIQUE(view_fulfillment_obligation_id) WHERE state='active'` on `ChannelViewActionBinding`；fact binding为`UNIQUE(fact_id)`与`UNIQUE(view_fulfillment_obligation_id) WHERE binding_state='bound'`；
- CHECK：current row的target/source/target revision/due ordinal/materialization version/lifecycle/deadline全非空；legacy row不得半填current identity；
- lifetime owner可用候选/审计索引`(state,target_peer_id,channel_message_id,account_id,version)`与transition `(owner_id,transition_seq,id)`；target claim：`(task_day_ledger_id,source_state,id)`；due materialization：`(daily_message_target_id,state,due_ordinal,id) WHERE state IN ('unmaterialized','action_bound')`；
- 账号future slot：Action partial index `(task_id,account_id,scheduled_at,id) WHERE action_type='view_message' AND status IN ('pending','claiming','executing','retryable_failed','unknown_after_send')`；
- source projection `(task_day_ledger_id,version)`、read-model `(task_day_ledger_id,current_version)`、enrollment `(task_id,state,version)`、route `(enrollment_id,state,route_epoch,version)` 和TargetSet `(task_day_ledger_id,target_peer_id,channel_message_id,id)` unique/index；route唯一`(enrollment_id,task_day_ledger_id)`，并以partial unique保证每enrollment至多一个current `preparing|active|blocked` route。
- settlement claim `(next_retry_at,deadline_at,id) WHERE activation_ready=true AND state IN ('pending','processing')`、target item `(operation_id,state,daily_message_target_id,id)`、blocker `(enrollment_id,state,scope,route_id,kind,id) WHERE state='open'`、blocker occurrence owner+kind+occurrence unique、fact observation request/evidence unique及`(logical_requested_task_id,logical_daily_message_target_key,due_ordinal,classification,fact_id)`永久排除查询、fact projection barrier的ledger索引必须与实际keyset顺序一致。
- bootstrap claim `(next_retry_at,next_period_start_at,id) WHERE state IN ('pending','processing')`及唯一`(task_id,next_period_start_at)`；blocked且无result的request revision通过显式supersede回到pending，不扫主表。
- logical source唯一`(tenant_id,operation_target_id)`；source event `(state,event_seq,id) WHERE state IN ('pending','processing')`、delta `(event_id,delta_seq,id)`、subscription `(logical_listener_source_id,state,observed_version,task_day_ledger_id)`与fanout item `(event_id,state,subscription_id,id)`必须支持有界claim、lease回收和count/hash完整readback，禁止主表Seq Scan。
- target-expiry activation operation `(next_retry_at,id) WHERE activation_ready=true AND state IN ('pending','processing')`、target expiry `(next_retry_at,active_until,id) WHERE activation_ready=true AND state IN ('pending','processing')`；lifecycle adoption owner `(state,id) WHERE state IN ('pending','draining')`、item `(adoption_id,state,item_seq,id) WHERE state IN ('pending','processing')`及trigger unique查询；accrual clock唯一route、segment唯一`(clock_id,segment_seq)`及`UNIQUE(clock_id) WHERE ended_at IS NULL`，都必须支持命令readback、historical as-of积分、deferred重入、lease回收和resume guard；blocked item不退出守恒或让owner误ready。
- takeover operation `(state,id) WHERE state IN ('fenced','quiescing','manifested','applying','readback')`、manifest item `(manifest_id,item_seq,id)`、chunk checkpoint `(operation_id,state,chunk_seq,id)`、source event `(fence_id,event_seq,id)`与业务event unique必须支持同manifest crash replay；inventory item逻辑/导航partial unique和tombstone index不随Task删除。
- canonical ViewRemoteFact lifetime unique保持全域有效，`obligation_id IS NULL`不得退出其unique/index；fact/binding的logical owner与tombstone查询建立覆盖索引，物理删除Task后同一peer/message/account仍命中防重。

生产DDL必须先做重复/半空preflight，`CREATE UNIQUE INDEX CONCURRENTLY`成功和catalog predicate readback后才短锁attach/drop旧约束；失败保持legacy writer和旧constraint，不允许无唯一保护窗口。真PostgreSQL EXPLAIN需证明due keyset、账号前/中/后slot、source projection和E4 target/due查询命中索引。

### 6.4 事务边界与唯一锁序

所有legacy/current writer固定按`ChannelViewListenerSource -> ListenerSourceState/SourceObservationEvent+Delta -> ChannelViewLifetimeIdentityOwner/Transition（多identity按peer/message/account C序） -> FleetPolicy/InventoryItem -> Task/TaskDomainRevision/TaskStartOperation或LedgerBootstrapOperation -> ChannelViewEnrollment -> TaskDayLedger/Route -> TakeoverOperation/SourceFence/Event -> AccrualClock/Segment -> LifecycleAdoption/Item -> SettlementOperation/Item -> ContractBlocker -> SourceSubscription/FanoutItem/Projection/TargetExpiryActivationOperation/TargetExpirySchedule -> DailyMessageTarget(C序id) -> ViewObligation(C序ordinal) -> ActionBinding/Action -> ExecutionAttempt/GatewayJournal -> RemoteFact/FactBinding/ProjectionState -> read-model`；同层多行按主键bytewise/C序。listener只触达最前两层；subscription bootstrap和source fanout先锁逻辑source。Planner/TxA/TxC/reconcile与deadline pre-gateway drain都必须先锁精确global owner再进入Task层，禁止Task/Action/Settlement反锁owner；settlement lease领取是独立短事务，不在持Task/settlement锁时发现后反锁owner。pre-gateway drain完成后的settlement finalize不再触达owner。各writer列实际触达层，禁止自定锁序或跨外调持事务。

Planner只读matching不写行；每edge物化短事务先CAS global LifetimeIdentityOwner，再按规范层级insert-or-read due unit并绑定ActionBinding/Action，任何后层失败让owner同事务回滚。Dispatcher Tx A提交唯一request/journal hold后释放连接，Telegram view在事务外执行，Tx C无论Task暂停/截止/route blocked都先append真实fact再投影。结果提交时若settlement已completed，fact binding标记post-settlement历史而不改结果；若settlement正在持锁，Tx C等待并在线性化点后分类。数据库死锁、CAS冲突和连接错误必须显式失败/重试同identity，不得吞掉或生成第二Action/request。

## 7. E4、防假绿与验收

`.github/scripts/task_fulfillment_e4_diagnostics.py` 必须调用同一只读 Assembler并返回：

```text
target_row_count / effective_target_total / target_set_hash / expected_due_count / due_set_hash
materialized_count / materialized_set_hash / due_unmaterialized_count
on_time_confirmed_count / late_count / unproven_count
gateway_inflight_count / unknown_hold_count / remote_identity_conflict_count / valid_open_count
eligible_distinct_identity_count / structural_capacity_shortfall_count
structural_capacity_shortfall_reasons / invalid_binding_count
source_state / source_projection_version / active_source_set_hash / read_model_version
active_message_count / expired_message_count
settlement_state / settlement_status / settled_due_count / settled_shortfall_count / settlement_result_hash
attempt_count / remote_fact_count / remote_fact_observation_count / post_release_full_chain_count
deployed_sha / role_capability_matrix / legacy_writer_delta_after_fence
fleet_policy_state / inventory_open_enrolled_retired_counts / takeover_class_counts
source_event_delta_fanout_pending / source_oldest_lag / source_lease_recovery_count
target_expiry_pending / bootstrap_pending / adoption_pending_deferred_blocked
settlement_pending_blocked / fact_projection_pending_poison
each_owner_heartbeat_at / oldest_item_age / expired_lease_count
```

阻塞码固定：

- `channel_view_due_unmaterialized`：存在可推进 identity 却未物化；
- `channel_view_structural_capacity_shortfall`：distinct identity 不足；
- `channel_view_waiting_for_source`：健康采集但无 active source；
- `channel_view_listener_stalled|channel_view_source_unresolved`：来源链故障；
- `channel_view_source_empty_terminal`：finite selector经fresh poll证明为空且禁止dynamic append，本ledger为missed_no_source；
- `channel_view_remote_fact_missing`：confirmed 与 typed fact 不守恒；
- `channel_view_post_release_fact_missing`：发布后尚无真实远端闭环。

`required_count`不再等于obligation count。短窗口只能通过Planner层/canary层：部署SHA/逐role capability一致、legacy writer在fence后增量0、对应任务`MaterializationGap=0`或只剩明确structural/source blocker、发布后出现`Task -> enrollment/route -> target/due unit -> ActionBinding/Action -> ExecutionAttempt(success) -> ViewRemoteFact -> unique bound on-time fact`、无invalid binding或同peer-message-account双发。source event/fanout、target expiry、bootstrap、adoption、settlement、fact projector任一owner缺heartbeat、超SLA oldest lag、expired lease未回收、count/hash不守恒或poison/blocked未解释时E4失败；fleet inventory/classification也必须闭合。自然日`production_fixed`要求Settlement completed/met、`OnTimeBoundSet=SettledDueSet`且unknown/late/unproven/shortfall/duplicate均0；动态等待下一来源是独立source state。短窗口一条full chain不能冒充自然日完成。

QA 必须包含：

1. anchor精确时due=0，此后同revision的DueSet单调增长；
2. `latest_n`初始集合冻结，新帖只按dynamic合同append，旧帖不被挤掉；expire只冻结accrued due；
3. 已有 future Action 到 23:57 时第二轮仍能在各账号前/中/后合法空隙为distinct due unit建多条Action；
4. 两批 curve 时间重叠不整体平移，旧 Action 时间/hash不变；
5. 1,000/消息不会被task template 180秒隐式压成480/日；同账号跨消息仍满足account gap；
6. 同一due ordinal和同一peer-message-account并发Planner只有一个current绑定/可执行Action；
7. fact/gateway/unknown/action互斥，future/failed/错version不抵扣当前due；
8. active=0时成功空采集为waiting，采集失败为stalled，source unresolved与两者分开；
9. 在冻结资格/Session/slot fixture确为eligible 869、due 1,000时物化869并报131 structural shortfall，不把该生产快照数字当永久产能且不清零整批；
10. E4在31 obligations、due 1,370时必须失败，不得以31完成为绿；
11. current-day manifest/backfill/route CAS、pre/post-Gateway回滚边界用真实PostgreSQL强制交错；
12. 真实PostgreSQL并发、partial unique、索引/EXPLAIN、deadline和跨日ledger回归；
13. 生产canary至少一条发布后typed ViewRemoteFact，随后持续观察materialized/due与Attempt/fact增量。
14. 同一remote identity的两个Gateway observation只能得到一个canonical fact+两个append-only observation，same owner为幂等、不同requested unit打开identity blocker；不同remote identity却请求同一due unit时两个fact都保留，第二binding为unbound conflict。两类并发都不双确认、不丢typed evidence。
15. blocker resolve后精确旧occurrence重放不得重开或加count；新source revision再次poison/conflict必须新建一个open blocker并重新fence，并发新occurrence至多一个open row。
16. shared `Task.version`在线回填后，`TaskOut.task_version`与generic/type PATCH、start/pause/resume/stop/delete的`expected_task_version`端到端传递；channel与AI并发Task mutation只有expected-version winner，stale 409且零写；`config_revision`漂移不能掩盖row CAS，任一旧入口直接写status/display均由契约测试拦截。
17. owner/requested Task删除前后，RemoteFactObservation navigation FK由RESTRICT安全迁为SET NULL，logical requested due key与tombstone pointer/count+hash不变；删除后late/duplicate observation可追加但不复活Task、释放lifetime identity或丢owner-conflict证据。
18. same-identity owner conflict提交后Action/Attempt/Binding terminal、obligation=`remote_identity_conflict`且仍占R_m；无resolution不得重复物化。权威resolution在deadline前只释放同due到新materialization并永久排除旧identity，deadline后只进known shortfall；双resolution/contract-reopen/crash重放不双减blocker、不复用原账号或改canonical fact。
19. 零obligation/部分物化仍由settlement计入完整DueSet；fact commit与projection/settlement、dynamic source append与deadline、late/unproven fact强制交错，结论不假绿也不改写immutable missed。
20. pause/resume/stop/retry/reset/PATCH/delete与route blocker、ActionBinding、settlement并发；非法入口typed 409/422且Gateway后零倒退。
21. pause跨多个segment后DueSet不增长，resume从原active progress继续；expiry/settlement晚处理仍分别按active_until/deadline回算，午夜后零新增due。
22. 一个fact只能bound一个Task due unit；其他Task只失去lifetime identity。owner Task物理删除后fact/binding/observation logical key与global unique仍在，另一Task/Gateway无法重复view。
23. Tx C缺任一required ProjectionState、额外kind或count/hash不等时settlement不能完成；poison走enrollment blocker及精确resolution/requeue。
24. collector切换不换logical source/subscription；持续成功empty poll刷新cursor-verified并保持waiting，finite禁止dynamic则settle missed_no_source。
25. first-start、同period start-after-stop、start-after-stop rollover与automatic running rollover任一crash point重放只得到一套预期Task/ledger/route/clock或完整source subscription+projection/targets+expiry/settlement/read-model bundle；所有用户start都由one-current-row TaskStartOperation同事务写/回读完整result，replace/stale/双请求只有一个winner，automatic rollover不触碰StartOperation。
26. takeover粗preview后强制legacy写入，再preparing/quiescence/final manifest必须吸收；final manifest后hold→fact作为唯一source event delta，未处理delta时activation失败。
27. 全takeover class保持原Task状态；invalid组合blocked，paused/stopped零发送，settling先结算，rollover不复活closed route。
28. exact `scheduled_at=deadline_at`不进入A集合也不能Tx A；deadline前一微秒与deadline后一微秒按半开区间分类。
29. sealed mixed fleet中legacy Planner与current Planner/Tx A并发竞争同一peer-message-account时，只有一个`ChannelViewLifetimeIdentityOwner` CAS winner；safe pre-transport release后另一owner才可领取，call-issued/unknown/fact后永不释放。兼容基线对全部legacy open Action/Gateway/fact backfill/readback零缺owner后才放Dispatcher，任一无owner Action均fail-closed；真PG反向输入无check-then-insert双调用或死锁。
30. takeover apply创建的过期TargetExpirySchedule与SettlementOperation在activation前均不进入claim索引；class activation常数级事务只唤醒单一ExpiryActivationOperation与SettlementOperation，expiry fan-out按manifest keyset逐行写`max(active_until,database_now)`并以count/hash完成，任一崩溃可续且schedule只领取一次。same-period running/paused/stopped在final CAS前/恰等于deadline/后一微秒交错，只有`database_now < deadline_at`可激活或开clock segment；跨界保持preparing并用新manifest重分settling-closed/rollover-eligible，旧class发送为0。
31. Task物理删除前，global owner/transition与fact/binding/observation一并进入tombstone count/hash；仅可证safe pre_gateway释放，call-issued/unknown/confirmed/conflict保留并SET NULL导航FK。删除后late Tx C/reconcile仍推进同一owner，Planner始终fail-closed且不会因Task FK消失重用identity；canonical fact抢占pre_gateway时同事务直接转confirmed，无available可见窗口。
32. takeover class黄金fixture覆盖draft、scheduled、stopped-never-started、same-period running/paused/stopped、running+closed settlement completed、live expired unsettled、`target_reached|wrapping_up`有/无ledger及settled/unsettled、failed/completed/deleted仍有未结远端identity；每条输入恰好命中一个class。zero-history不得进入rollover，terminal不得进入live settling/rollover；terminal settlement/archive完成前发送增量为0，完成后只retire且late reconcile仍可达。
33. `channel_view_contract_hash_v1` golden vectors在不同进程、locale、timezone及API/worker/CLI入口产生相同canonical bytes/hash；打乱输入顺序不变，改变任一registry业务identity/version/count必变。未知version、同count不同identity、漏字段、float/local timestamp或旧hash混用必须使activation/settlement/replay/E4 fail-closed。
34. 真PostgreSQL强制交错deadline settlement、Tx A、同Task Planner与其他Task争抢同一LifetimeIdentityOwner：可证未transport的pre_gateway identity只被owner-first drain释放一次，并在同事务终结Binding/Action/due unit；Tx A先赢、call-issued/unknown/fact identity永久保留。`discovered=safe_released+issued_or_unknown_preserved`及set hash不成立、仍有pre_gateway owner或drain未完成时immutable settlement不得提交，任一输入顺序都无双占、identity泄漏或死锁。
35. 真PostgreSQL强制把两个Task对同一identity的只读matching、owner CAS、due insert与另一Task/Tx A反序交错：只有owner-first事务winner能创建due binding/Action；due CAS失败时owner transition随事务回滚为原状态，预建due事务不触达owner，任一顺序无Task/obligation→owner反锁、孤儿pre_gateway或双Action。

## 8. 发布、存量处理与回滚

新增：

- `ChannelViewPlannerFleetPolicy(tenant_id unique,contract_version,state=legacy_allowed|migration_only|due_unit_only,inventory_status=building|sealed,cutoff_created_at,cutoff_task_id,legacy_inventory_count,legacy_inventory_hash,runtime_state_hash,version)`；每tenant只有一条current policy，hash不能替代逐Task membership查询；
- immutable membership `ChannelViewLegacyInventoryItem(policy_id,logical_task_id,task_record_id nullable,frozen_task_created_at,membership_item_hash,state=open|enrolled|retired,allowed_task_status,allowed_task_version,allowed_lifecycle_epoch,allowed_config_revision,allowed_domain_revision,tombstone_id nullable,version)`，唯一`(policy_id,logical_task_id)`及`task_record_id IS NOT NULL`的partial unique。logical identity、created-at与membership hash不可改；Task导航FK为`ON DELETE SET NULL`，retired/tombstone行继续参与原inventory hash。sealed后尚open的legacy Task每次合法lifecycle/PATCH必须在同事务以expected item version推进allowed status/version/epochs并重算policy runtime-state hash；guard只允许Task当前值与item allowed值完全相等，禁止sealed manifest漂移或新建漏网Task；
- `ChannelViewPlannerEnrollment(task_id unique,contract_version,state=preparing|current|retired,current_route_id nullable,open_contract_blocker_count,blocker_revision,version)`是Task跨ledger current-contract owner；never-started current enrollment允许current_route_id为空；
- `ChannelViewPlannerContractRoute(enrollment_id,task_id,task_day_ledger_id,contract_version,state=preparing|active|blocked|closed,writer_state=running|paused|stopped,route_epoch,activation_target_set_hash,activation_due_set_hash,activation_materialized_set_hash,expected_lifecycle_epoch,manifest_hash,open_blocker_count,blocker_revision,version,activated_at)`，唯一`(enrollment_id,task_day_ledger_id)`。route_epoch在该ledger内不可变；动态target append、DueSet增长和materialization只推进read-model revision，不改epoch或activation hashes；rollover先settle/close旧route再建新route并CAS enrollment current pointer，旧route保留给late fact/reconcile；

Release Gate固定两项可机读能力，不能以“同SHA/healthy”代替：`channel_view_due_unit_fence_v1`覆盖backend(API create/start/PATCH/lifecycle/delete/takeover及legacy/current global identity owner helper)、worker-planner、每个dispatcher replica、worker-recovery中的projector/bootstrap/adoption/settlement/expiry drain以及worker-listener对enrollment/fleet/source scope的写门禁；`channel_view_source_event_producer_v1`覆盖worker-listener的logical source/observation/delta writer及backend中会推进source-policy/target-input的入口。runtime实例清单必须从本次compose/systemd/runtime manifest导出，逐entrypoint→role/container/replica记录expected writer bitset，不能手写少一类。

inventory building前逐实例readback`deployed_sha,worker_role,instance_id,两项capability bits,heartbeat_at,current lease/claim`，旧SHA/缺bit实例和其未退lease必须为0。兼容SHA上线后，所有legacy/current新Planner/Tx A已强制获取global identity owner；Dispatcher对无owner旧Action先fail-closed。受保护backfill按canonical fact→call-issued/unknown→合法pre-Gateway Action证据优先级为全部存量identity insert/CAS owner，冲突fence对应Task；重复扫描至upper bound，分别证明`canonical fact identity set = confirmed owner identity set`、每条call-issued/unknown request恰有匹配owner、每条合法非终态legacy Action恰有pre_gateway owner，且各类count/hash闭合后才允许Dispatcher继续。随后执行受控empty/nonempty source poll，证明`logical source -> event/delta -> subscription/fanout -> projection/read-model`count/hash闭合。Release artifact保存能力矩阵、global owner分类backfill count/hash与readback。rollback workflow对同两项能力hard gate；任一目标SHA/实例缺bit即拒绝，不能靠停Task或健康检查绕过。

`legacy_allowed|migration_only`时只有sealed inventory中的open item且enrollment/route缺失可继续legacy；`migration_only`额外禁止创建新的legacy Task，新Task先建current enrollment，首次start/bootstrap建立due-unit route。enrollment/route任一preparing后legacy/current业务writer都fail-closed，只允许manifest、已有Gateway reconcile和settlement projection。current业务写/新Gateway必须同时满足Task running、enrollment current、route active+writer_state running、epoch一致且两层blocker为0；paused/stopped route只准adoption/reconcile/settlement。全部inventory item enrolled/retired并readback后CAS policy到due_unit_only，从此enrollment/route缺失全角色fail-closed。旧SHA不识别这些fence，因此任何inventory/preparing前，backend、planner、dispatcher、recovery、listener全部实例必须先部署兼容SHA、旧SHA实例为0并完成capability readback；该SHA才是唯一回滚基线。

inventory由受保护builder而非Alembic创建：先验证全部writer/producer实例为fence-compatible SHA，再CAS policy absent/旧state→`inventory_status=building`并fence Task create/lifecycle；以`created_at,id` cutoff keyset扫描全部legacy channel_view Task，随后执行in-flight create/lifecycle barrier并补扫至cutoff，逐项写membership，按logical Task ID的C序重算count/hash并核对数据库现存集合。只有零漏项/重复且各allowed epoch与Task同快照一致，才在保持create fence的同一受保护transition CAS `building,legacy_allowed -> sealed,migration_only`；从该提交起新Task必须原子创建current enrollment，存量open item继续按allowed epochs走legacy，随后才解除短fence。不存在sealed+legacy_allowed且create已放开的窗口。builder crash按policy/cutoff/cursor续同一inventory；不能人工改hash或让Alembic填业务item。

存量接管先按manifest冻结以下互斥class；任何status/ledger/settlement组合不命中恰好一类即`contract_migration_blocked`：

| takeover class | 源条件 | 激活后Task/enrollment/route/clock/settlement | 新发送 |
| --- | --- | --- | --- |
| `never_started` | `draft|pending|scheduled`（或无历史start的stopped）且无历史ledger/Action/fact/TaskStart成功 | 精确保留源status、scheduled_start与TaskStartOperation空结果；active enrollment、current_route为空，不建ledger/clock/settlement | 否；到时调度或显式first-start走完整bootstrap |
| `same_period_running` | running且当前ledger未到deadline | 原地preparing导入；active route writer=running、clock以manifest due为baseline并从activation开segment、settlement activation-ready | 是，activation readback后 |
| `same_period_paused` | paused且当前ledger未到deadline | active route writer=paused、clock paused、imported-baseline adoption=ready、settlement activation-ready | 否；resume后 |
| `same_period_stopped` | stopped且当前ledger未到deadline | active route writer=stopped、clock stopped、imported-baseline adoption=ready、settlement activation-ready | 否；start-after-stop后 |
| `settling_closed` | `running|paused|stopped`已有权威start/ledger lineage，latest ledger已到deadline且settlement/unknown/projection未收口 | 保持源Task状态；active old route writer stopped、clock按deadline closed、settlement activation-ready立即drain | 否；只reconcile/settle |
| `rollover_eligible` | 仅`running|paused|stopped`，已有权威historical-start及prior ledger/route lineage，prior settlement completed且不存在next-period ledger/route；明确排除zero-start/zero-ledger | active enrollment；latest route closed，不提前建新ledger | 仅原running由automatic bootstrap后；paused/stopped等显式命令 |
| `terminal_settling` | `target_reached|wrapping_up|failed|completed|deleted`仍有未结ledger、hold、fact、projection或settlement identity | 保持terminal源状态，route closed且writer stopped；将远端identity、late reconcile与settlement/archive owner纳入manifest/tombstone。未来deadline的settlement只写activation-ready并以deadline为next-retry，不提前结算；到期后立即drain，全部readback前不得retire | 否；只reconcile/settle/archive |
| `terminal_retired` | `target_reached|wrapping_up|failed|completed|deleted`已无未结owner，或`terminal_settling`完成settlement、projection与tombstone archive/readback；含审批放弃 | item/enrollment retired并保留fact/fleet/contract tombstone；无active route | 否 |

分类manifest冻结Task status/version/lifecycle/config/domain revisions、historical-start lineage、ledger/source/target/Action/fact/settlement/archive identity与hash；class predicates必须互斥且完整，不能用表顺序决定winner。zero-start/zero-ledger只可进入`never_started`，不会因“没有current ledger”进入`rollover_eligible`；`target_reached|wrapping_up|failed|completed|deleted`只进入`terminal_settling|terminal_retired`，不会进入live `settling_closed|rollover_eligible`。class-specific final CAS保持原业务状态，绝不把paused/stopped或terminal状态自动改running。`same_period_running|paused|stopped`的final CAS必须另以数据库时间校验`database_now < ledger.deadline_at`，通过后running类才可append activation clock segment；等于/晚于deadline时保持preparing且旧manifest superseded，按最新projection/settlement事实重建`settling_closed|terminal_settling`，live状态只有immutable settlement已完成才可重分`rollover_eligible`。`settling_closed -> rollover_eligible`只由immutable settlement/reconcile结果推进；`rollover_eligible`禁止把closed route改running，必须走新ledger bootstrap。terminal类即使settlement完成也不触发automatic bootstrap、resume或start，只有完成archive/readback后转`terminal_retired`。

接管不是无状态脚本。新增持久`ChannelViewTakeoverOperation(inventory_item_id unique,task_id,takeover_class,state=previewed|fenced|quiescing|manifested|applying|readback|blocked|activated,preview_hash,final_manifest_id,source_fence_id,expected_task/item/policy versions,chunk_cursor,expected/applied/readback counts+hashes,lease/version)`；不可变`ChannelViewTakeoverManifest(operation_id,manifest_revision,as_of,static_revision_vector_hash,source_fence_version,source_event_set_hash,target/source/due/materialized/fact identity-set hashes,manifest_hash)`及`ChannelViewTakeoverManifestItem(manifest_id,item_seq,source_kind,source_identity,classification,canonical_order_key,payload_hash)`唯一`(manifest_id,item_seq)`与`(manifest_id,source_kind,source_identity)`；每个`ChannelViewTakeoverChunkCheckpoint(operation_id,chunk_seq,first_item_seq,last_item_seq,input_hash,output_hash,state,version)`可崩溃重放。preview/apply/readback API必须提交expected operation/version/hash，不能只靠route.manifest_hash或本地文件续跑。

`enrollment/preparing`同事务创建`ChannelViewTakeoverSourceFence(operation_id unique,current_version,next_event_seq,event_set_hash,version)`。此后legacy/current Planner、Generation与新Tx A全部fenced；workflow等待所有pre-call claim/lease退出，已call-issued request只允许按原identity收口。每个仍可提交的Attempt/Gateway/fact/binding/projection变化必须先按§6.4追加唯一`ChannelViewTakeoverSourceEvent(fence_id,event_seq,source_kind,source_identity,source_revision,transition,payload_hash)`并CAS fence，重复业务event只回读，不二次bump。quiescence完成后才在同一fence as-of生成final immutable manifest；粗preview永远不能直接apply。chunk匹配final manifest、source fence与允许的hold→fact/projection delta hash，其他A类static/source漂移blocked；activation最终锁fence，证明所有source event已进入readback、pre-call owner=0、旧writer/Gateway新增量=0，再CAS class-specific状态。这样manifest前后并发事实不会漏绑或错分ordinal。

1. 从合并时最新 `origin/master` 建干净实现 worktree；不在设计分支或用户脏 release checkout开发；
2. 先部署 additive schema、兼容reader/writer/Gateway/source fence与新 E4；验证全部role实例同SHA/capability后，按上文builder建立building inventory、cutoff keyset+inflight barrier、count/hash/readback后sealed并CAS policy `legacy_allowed -> migration_only`；Alembic只建schema，不创建membership业务数据；
3. 生产先对受影响Task做只读粗preview；随后按单Task CAS enrollment/preparing+source fence，等待pre-call claim/lease quiescence，并在fence as-of生成最终不可变manifest，冻结ledger、逻辑来源、target/due、obligation/Action/Attempt/fact/settlement与最晚排期；以固定排序为legacy行分配target/due ordinal，逐项分类`fact|gateway|unknown|valid_pre_gateway|released|conflict`；
4. 对单个canary的同一TakeoverOperation按final manifest分块backfill target、due unit、logical source/subscription/projection/read-model、clock baseline、ActionBinding、fact binding、adoption baseline、activation-ready=false/next-retry-null settlement、expiry schedules及单一ExpiryActivationOperation，并引用兼容基线已建立的global lifetime owner，不改confirmed/Gateway/unknown或旧Action字段；每chunk保存checkpoint并吸收允许的source delta。readback class-specific target/due/materialized/global-owner/source/clock/adoption/settlement/expiry/fence hash完全一致且两层blocker为0后，常数级同事务写enrollment current、适用的current-route pointer、route active/closed、settlement与ExpiryActivationOperation activation-ready/next-retry、item enrolled、operation activated并保持原Task lifecycle；schedule由后续durable fan-out有界激活，无ledger class不伪造route/settlement；
5. 激活后以Task ID readbackSHA/enrollment/current route/ledger/hash，只有原running Task才精确CAS`next_run_at=database_now`；paused/stopped保持原状态且零新Action。连续两个Planner周期证明due增长、前/中/后账号空隙物化和零重复，再按sealed inventory扩大；never-started只建current enrollment且current_route为空，不伪造ledger，首次start原子bootstrap；`target_reached|wrapping_up|failed|completed|deleted`有未结远端/settlement identity时只进terminal-settling收口归档，readback完成后才retire，任何阶段都不被自动启动；
6. canary必须出现`Task -> target/due unit -> Action -> ExecutionAttempt(success) -> ViewRemoteFact`。无Attempt/fact、hash漂移或重复source identity时立即停止该route新增物化并进入blocked；旧Action和远端事实不回滚、不删除；
7. 全inventory enrolled/retired、全部active route/readback与new-task bootstrap capability证明后才CAS fleet `migration_only -> due_unit_only`；Release artifact保存policy/inventory hash+count、每role SHA/capability、每Task route/manifest/settlement和E4结果；
8. policy building后只允许回到fence-compatible SHA；sealed且fleet=`legacy_allowed|migration_only`时，没有enrollment的open inventory item仍可按其allowed epochs走legacy。某Task一旦写enrollment/preparing route，该Task即永久forward-only、legacy/current writer按Task fence fail-closed，绝不恢复旧max-tail writer；其他尚open item不受其误伤。fleet=`due_unit_only`后全部missing enrollment fail-closed。active后无论current Action是否进入Gateway都只能pause/stop、保留reconcile并前向修复；rollback SHA也必须具备同一inventory/enrollment/source-event fence capability；
9. `production_fixed`只在真实远端闭环与due/materialized/settlement守恒成立后写；外部账号不足或无来源保持`blocked`，不能因代码修好改成pass。

## 9. 开发交接

最小代码面：

- `backend/app/services/task_center/executors/channel_view.py`：共用 due snapshot、去除 task-global future-tail reservation、记录 typed runtime；
- `backend/app/services/task_center/schedule_reservation.py`：保留其他任务现合同；浏览不再走 append-after-latest路径，禁止用全局改动破坏评论/AI；
- `backend/app/services/task_center/executors/common.py` 与 `listener_runtime.py`：持久 channel source observation；
- `backend/app/services/task_center/channel_view_due.py`（新增、单一职责）：权威 due/materialized/capacity assembler；
- `backend/app/services/task_center/channel_view_contract_hash.py`（新增、单一职责）：唯一`channel_view_contract_hash_v1` registry/serializer与golden vectors；所有API/worker/CLI只能调用该helper；
- `backend/app/models/fulfillment_facts.py`及按责任拆分的新channel-view模型、Alembic：peer-qualified message target、due-unit字段、legacy/current共用LifetimeIdentityOwner+Transition及其RESTRICT→SET NULL/tombstone保留、Action/fact binding、append-only RemoteFactObservation、source event/subscription/fanout、read-model、accrual clock、LifecycleAdoption/Item、settlement/bootstrap、TargetExpiryActivationOperation+Schedule、双scope ContractBlocker+Occurrence、fleet/inventory/enrollment/per-ledger route与partial unique；每个模型文件≤500行；
- `backend/app/services/task_center/channel_view_takeover.py`（新增）：fleet inventory、manifest/preview/apply/readback、legacy Action binding与enrollment/route CAS；
- `backend/app/services/task_center/channel_view_settlement.py`（新增）：deadline lease、owner-first pre-gateway unit cursor/drain、safe release/issued-preserved守恒、projection barrier、immutable target settlement与crash replay；
- `backend/app/services/task_center/details.py`：返回 typed source/capacity/materialization状态；
- `.github/scripts/task_fulfillment_e4_diagnostics.py`：调用同一 Assembler，删除 obligation-as-required假设；
- 定向 no-PostgreSQL纯函数测试、真实 PostgreSQL并发/唯一/查询测试和生产 E4。

不新增目标配置、mock fact、自动降目标、silent fallback或“只要有 Action 就算启动”的完成路径。
