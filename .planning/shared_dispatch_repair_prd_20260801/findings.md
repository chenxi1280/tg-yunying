# Findings

## Confirmed Production Facts
- 当前 release `87fe0bf0` 的三个 AI 活群在新容器启动后未新增业务确认。
- 搜索点击仍增长，说明 Dispatcher/Gateway 不是整体停机。
- 搜索 fulfillment writer 使用默认 `ACCOUNT_SHARD_TOTAL=1`，两个 Dispatcher 使用 `ACCOUNT_SHARD_TOTAL=2`；共享窗口存在两套分片拓扑。
- scope capacity 为 52，而两个 Dispatcher 受 DB pool 限制合计有效并发约 26。
- 跨 epoch allocation 合计会保留旧 unclaimed，导致容量继续占用。
- PostgreSQL 出现 Scope→Task FK 与 Task FOR UPDATE→Scope 的反向锁序 deadlock。
- AI 还有 scope contract、listener watermark、reply target、群管准入等第二层 blocker。

## Existing Product Truth
- `all-task-fulfillment-recovery-prd.md` 已要求统一锁序、配置不一致 fail closed、搜索虚拟 `(1,0)` 与运行分片路由分离。
- 同 PRD §10.1 已要求：未物化搜索 obligation 不得进入通用 Dispatcher demand；只有 bound search unit 受保护，unbound search reservation 必须释放。
- 当前修复属于实现与存量接管缺口，不应改为固定 search>AI 或 AI>频道，也不应放宽验证码、准入、unknown 防重。
- PRD 已规定 `DISPATCHER_SCOPE_CAPACITY` 是全部共享 worker 的真实合计在途上限，配置不一致必须 fail closed。
- 生产当前有效并发公式为每 worker `min(20, 5+10-2)=13`，两 worker 合计 26；默认 52 是四 worker 拓扑遗留值。
- 既有合同版本发布栅栏要求禁止新旧 solver 混跑；本次需要提升 `dispatch_rebuild_contract_version` 并执行全量 fence 发布。
- 既有存量接管允许：证据完整且未进 Gateway 的 Action 补等价内容快照；证据不完整则 `content_contract_replan_required`，不得修改 success/unknown/Gateway-started。

## Document Scope
- 新专项 PRD作为本事故的实现交接真相源；主 PRD保留产品总口径并增加引用。
- 数据流索引需要新增本次合同版本、迁移和锁序说明。
- 生产运行文档在代码实现/发布阶段再同步具体配置和 runbook，本轮 PRD定义必须同步的交付要求。
- 初次选择 `DF-200` 时发现该 ID 已用于 `GET /api/tasks/{task_id}/ai-cycles`；完整索引最大已用 `DF-323`，专项数据流改为 `DF-324`。

## Phase 5 Audit Findings (in progress)
- 专项 §5.4 把全部 `claimed=0,bound=0` 旧 Reservation 直接交给通用 release，但主 PRD明确规定 search fulfillment Reservation 从 Window ready 到首次 SearchClickAssignmentEpoch finalize 期间由搜索物化流程独占，即使尚未 bound 也不得被通用 reclaimer 触碰；两者存在 P0 合同冲突。
- 专项 §5.5 规定 Gateway 返回后“先释放 claim，再同步 Action/Attempt/业务事实”，这把同一业务终结拆成两个可独立提交的状态，可能留下 claim 已释放但 Action 仍 executing/Gateway-started 的持久空窗；需要继续对照实现确认影响。
- 专项数据模型对 `effective_unclaimed_count` 仍写成“新增或复用现有字段”，物理真相源未冻结，暂不满足可直接开发的 Product Design Complete。
- Stage A 第 5 步先启动全部新 worker，Stage B 才做 AI 历史 Action apply；这与仓库现行“worker 全部 fence -> takeover preview/apply -> 启动 worker”的安全顺序相反。存量 Action 可在 preview/apply 前被 Dispatcher/AI generation/Recovery 抢占并改写，导致分类 hash 漂移或 Action 被错误终结。
- Preview/apply 同时要求全量 expected counts/hash 不漂移、又要求小批独立提交；PRD没有持久化 takeover batch/item/cursor。进程在中间批次崩溃后，数据库已变化，原 preview hash 必然无法原样重算，设计没有可恢复路径。
- `claimed=0,bound=0` 的通用 release 要求写 `release/exclusion`，但现有 `DispatchAllocationExclusion` 是搜索逐 unit 事实，强制需要 `fulfillment_lane_claim_ordinal` 与 carrier。普通 Action Reservation 没有该 unit 身份，专项把两种释放事实混成了一个不可直接实现的合同。
- 当前实现的 reclaimer 已显式保护未 finalized 的 search Reservation；专项若替换该逻辑会形成行为回退，而不是修复。
- Transaction B 虽列为一个事务，但“先释放 claim，再同步 Action/Attempt/业务账本”的文字没有明确禁止中间 commit；开发若按顺序拆成两个提交，会产生 claim-free + Action executing/Gateway-started 的持久空窗。应冻结为同一 post-Gateway 原子事务，任何失败整体回滚并由 remote reconcile 接管。
- `remote_reconcile_required` 只有“远端核验、不自动重发”，没有定义核验输入、found/not_found/inconclusive 三类结果、CAS 更新、业务义务释放/确认和审计。unknown 会一直 hold 数量/coverage 槽，相关任务可能永远不能结算。
- topology fingerprint 只说“至少覆盖”若干字段，未冻结规范化 payload、字段版本、排序和是否排除 heartbeat identity/time；作为 fail-closed 等值合同，这会让不同 writer 计算出不同 hash 并把全 scope 停住。
- 设计只定义“首次建立要有两个 heartbeat”和“临时 recycle 不变容量”，没有长期 shard 缺席的阈值、`dispatcher_shard_unavailable` 状态和恢复/重建语义。某 shard 长期不可用时仍会持续向其账号归属分配，任务可反复 expiry 而无准确 blocker。
- §9 自动化测试缺少 Stage B 中途崩溃续跑、Stage A 与迁移并发、Gateway 后提交故障、search+AI+频道混合高债务跨 Window 公平四类关键验收；这些正是本修复最可能回归并导致任务继续不完成的路径。

## Phase 6 Design Decisions
- search source 在首次 outcome finalize 前使用 `search_materialization_owned_count` 全量保护；finalize 后才收敛为 bound/claimed/released，普通 reclaimer 永不替搜索 solver 写 exclusion。
- topology/capacity fingerprint 采用带 schema version 的 canonical JSON SHA-256，明确排除 heartbeat、worker identity、时间和随机值。
- 新增稳定的 `DispatchRuntimeShardState` liveness 投影；配置拓扑与瞬时 live capacity 分离，stale shard 不再获取新份额且不发生跨 shard 账号接管。
- post-Gateway 采用 B0 防重边界 + B1 原子终结；B1 内 claim release、Action/Attempt 和业务账本禁止中间 commit，失败整体回滚后由 Recovery 转 remote reconcile。
- AI takeover 采用持久 batch/item/cursor；全量 hash只验证冻结 preview，已提交 item不参与再次重算，crash 后从 pending item续跑。
- remote reconcile 只接受 unique exact remote fact 或权威 `remote_mutation_started=false`；历史中未找到消息属于 inconclusive，禁止自动补发。
- 普通旧 epoch不能一刀切：当前合同且仍due的继续claim并计数；旧拓扑/版本binding释放后以同Action/义务进入新demand；无到期事实才直接释放。三类都不能写search exclusion。

## Phase 7 Implementation Baseline
- 当前 checkout 为 `release@87fe0bf0`，生产事故专项文档和其他用户工作均未提交；实现必须只追加本专项代码/测试，保留无关 dirty 文件。
- 当前生产默认仍是 `DISPATCHER_SCOPE_CAPACITY=52`、非Dispatcher默认 `ACCOUNT_SHARD_TOTAL=1`；两个Dispatcher compose override为total=2，代码尚无统一 `DISPATCH_RUNTIME_SHARD_TOTAL`。
- 现有模型只有 Scope/Window/TaskAllocation/ShardAllocation/Reservation；尚无 runtime shard state、takeover batch/item、remote reconcile case，也无 scope activation/fingerprint字段。
- 当前仓库已有搜索首次 outcome/release batch、调度公平、claim锁序与takeover基础测试，可在其上做最小增量，不重写已有solver。
- 额外发现：`Action` 和 `ExecutionAttempt` 都没有通用行版本列；搜索 release 现有 `expected_action_version=int(action.retry_count)` 只能观察 retry 次数，无法检测 status/payload/result/claim 的并发变化。专项PRD已经先修订为 canonical state hash CAS，明确禁止拿 `retry_count`、PostgreSQL `xmin` 或进程时间冒充稳定版本。
- 额外发现：现有 allocation/reservation 只有不可逆 rebuild hash，没有可比较的合同版本；实现无法可靠区分 current-contract 与 stale-contract。专项PRD和DF-324已先补入 `DispatchClaimShardAllocation.dispatch_contract_version` 冻结字段，历史空版本明确按 stale 处理，禁止从 hash 猜测回填。

### PRD to implementation map

| Contract | Primary code | Verification |
| --- | --- | --- |
| topology/capacity/fingerprint/liveness | `config.py`, `dispatch_claim_contract.py`, new `dispatch_runtime_contract.py`, heartbeat integration | canonical hash, production validation, liveness budget tests |
| effective unclaimed/cross-epoch conservation | dispatch models, ledger, allocation, reconciliation, rebuild snapshot | current/stale/no-longer-due and search ownership tests |
| Gateway B0/B1 | dispatcher execution wrapper, dispatch claim release, new recovery reconciliation service | transaction fault injection and unknown hold tests |
| AI scope takeover | new batch/item models and `ai_content_scope_takeover.py`, controlled script | preview/apply/drift/crash/idempotency tests |
| activation fence | scope activation service, worker drain gate, release script | preparing zero-write and atomic active tests |
| remote reconciliation | new case model/service and controlled script | evidence/state-hash CAS and three-result tests |
| schema/release truth | migration 0134, compose, production runtime/dataflow/structure docs | upgrade replay, compose assertions, doc checks |

### Baseline test evidence

- `test_dispatch_claim_reservations.py`, `test_dispatch_claim_release_lock_order.py`, `test_search_click_dispatch_window_demand.py`, `test_takeover_all_task_fulfillment_script.py`: 41 passed in 4.27s before implementation.

## Post-implementation reverse audit

- 远端权威no-mutation原先会把AI Action写成终态却不释放CycleSlot/quantity/message memory，原义务无法再规划；现已改为原槽replan并显式失效unknown stance。
- 候选只包含shard 0/1时，历史shard 2/3行会触发数组越界而非形成发布结论；现仅忽略stale越界历史，新鲜旧writer仍阻断激活。
- view/reaction成功没有新Telegram消息ID，B1回滚后旧设计无法证明并恢复业务成功；现用源消息`remote_fact_id`、完整payload hash和类型化远端事实恢复。
- 如果B0只冻结identity、不冻结request/target fingerprint，Action payload可在Gateway后漂移并使journal证据错误绑定；现B0同时冻结两类fingerprint，任何漂移进入conflict。
- `.env.production.example`仍保留事故前容量52，会使首次生产模板与26/2 fail-closed发布合同冲突并在Stage A前失败；现已修正模板并加入release gate测试。
- 新调度scope过滤实现缺少SQLAlchemy `and_`导入，只会在真实AI scope claim路径触发；定向公平测试暴露后已修复。
- 当前剩余未证明项不是已知可用代码fallback：PostgreSQL并发/独立journal事务、Actions、生产激活、Telegram事实和自然日E4都保持blocked/unproven。

## Phase 14 completion audit findings

### Candidate A: preparing fence可能未覆盖listener

- `worker.drain_once()`只通过`dispatch_writer_allowed()`统一门控一次；`FENCED_WRITER_ROLES`当前仅含`all/task_center/planner/dispatcher/recovery/ai-generation`。
- Stage A启动列表同时启动`worker-listener`，但listener不在fence集合。需要继续检查listener是否会更新Action/Attempt/admission/coverage等takeover分类或业务账本；若会，则preparing期间“全部相关业务writer零写”不成立，可能造成preview/apply drift或在激活前改变业务终态。
- Resolution: 当前listener drain只做Telegram只读抓取，并写GroupContextMessage、listener cursor、GroupBotAdmission observation/state和Task.stats；未发现其创建/claim/终结Action、写Attempt/coverage/content/search账本或调用发送型Gateway。专项Stage A禁止的Window/claim/generation/recovery terminal/Gateway发送均由现有fence角色覆盖。因此listener不在集合是允许的动态输入采集，不构成本轮任务不完成缺陷；若未来listener增加Action业务写入，必须同步加入fence。

### B0/B1 initial audit

- 普通Action的ExecutionAttempt创建时会冻结request identity、完整payload hash和target hash；`_mark_gateway_call_started`默认独立commit。Gateway结果由`_finish_execution_attempt`先写独立journal，再由调用者继续B1业务终结。
- 仍需逐个核对所有`commit=False`调用是否在真实Gateway之前有显式commit，以及所有Gateway action类型是否都经过`_finish_execution_attempt`；任何漏项都会使进程崩溃后没有可靠B0或journal，任务永久unknown或重复执行。

### Finding P0-B: 群管频道关注与确认callback绕过B0/journal

- `_dispatch_group_bot_required_channel_follow()`直接调用`gateway.follow_group_bot_required_channel()`，没有创建ExecutionAttempt、没有持久`gateway_call_started_at`、没有冻结request/target fingerprint，结果也通过不带attempt的`_apply_operation_result()`收口。
- `_click_refreshed_group_bot_confirmation()`直接调用`gateway.click_group_bot_confirmation_button()`，同样没有Attempt/B0/journal；`_finish_group_bot_confirmation_button()`也不接收attempt。
- 影响：进程在Telegram已关注/已点击后、DB提交前崩溃时，Recovery无法区分“未调用”和“远端已发生”，Action可被重试或准入状态永久等待；这会直接阻断AI群任务进入可发送状态。现有Gateway原子性/remote reconcile测试没有覆盖这两个action type。
- Severity: P0，且属于现专项B0/B1“所有Gateway业务动作”合同的实现遗漏。

### B0 coverage resolution for implemented main task actions

- group send、target send、view、reaction、comment的`commit=False`均位于reserve helper内，并在真实Gateway前紧接`session.commit()`；delete/invite/membership主链使用默认commit或先提交再mark，未发现这几条主链的B0未落库。
- post-send visibility probe是Recovery阶段只读Telegram查询，不产生远端mutation，不要求ExecutionAttempt。
- AST扫描确认当前显式缺少attempt marker的业务写Gateway入口以两个群管准入Action最明确；其他无marker helper多数运行在已有membership/invite/search Attempt内部，仍需从remote reconcile可终结性继续审查。

### Candidate C: 无消息ID的membership/invite类远端成功无法reconcile confirmed

- Gateway journal只有`remote_mutation_state=true`且存在`remote_message_id`或`remote_fact_id`才生成`remote_confirmed`。现有remote history只支持纯文本group send。
- view/reaction已补`remote_fact_id`，但membership/invite主链调用`_apply_operation_result()`时没有传入类型化fact；即使Gateway已远端成功，B1 rollback后的journal只有mutation=true而没有fact ID，只能永久inconclusive。
- 需要继续确认这类Action是否作为频道任务/AI群管准入的前置义务；如果是，unknown hold会阻断后续view/reaction/comment或AI发送，应升P0并补类型化membership事实/权威只读确认。

### Finding P0-C: membership远端成功在B1崩溃后永久阻塞后续任务

- Confirmed: `ensure_channel_membership/ensure_target_membership`已有B0 Attempt，但成功通过`_apply_operation_result(..., attempt=attempt)`时未写`remote_fact_id`；journal的mutation=true且无message/fact ID不会生成confirmed，Telegram history又明确返回unsupported。
- `channel_membership._should_create_membership_attempt()`对`unknown_after_send`固定返回false；全账号membership admission则把它置为`waiting_approval/manual_required`。因此B1 rollback会永久占住该账号/目标，不创建replacement，频道浏览/反应/评论和AI群准入都可能无法继续。
- 同类invite/admin rescue链也没有类型化fact，但membership是五类任务的直接前置，严重度确定为P0。需要设计`MembershipRemoteFact`或使用账号+冻结peer+membership state/version的权威只读确认；不能把“已经在群”无时间/归属证据直接当本次义务成功。
- Re-audit note: service.py存在旧的`_recover_existing_unknown_membership_actions`补偿reprobe路径，可能避免“永久”阻塞。必须核对它是否对本次B0 identity/remote case做CAS、是否能区分本次join前已在群、以及与新case并发是否互相冲突；P0-C结论暂保持但待该路径复核后定级。

### Finding P1-C (reclassified): membership存在两套互相冲突的unknown恢复协议

- 旧Recovery会对membership unknown执行只读`probe_target_capabilities`；当前状态满足时直接把Action/Attempt改success、写membership joined并推进后续任务，因此“必然永久阻塞”不成立，P0-C降为P1-C。
- 但新B0/B1路径同时已经创建`RemoteReconcileCase`。旧Recovery不读取case、不核对expected hashes/evidence hash、不更新case state；它改写Action/Attempt后，case仍pending，后续受控apply必因state hash漂移变conflict。
- 影响：任务可能继续，但远端核验账本永久不闭合、release/审计出现pending或conflict，且同一unknown动作由两个状态机竞争。需要把membership权威reprobe纳入RemoteReconcileEvidence的类型化协议，或明确从新case范围排除并迁移旧case，不能两条链并存。

### Candidate D: 正常发布可能被刚停止的旧heartbeat阻塞

- `verify-ready`要求最近120秒内所有fenced role的active WorkerHeartbeat合同版本等于candidate；`compose stop`之后会立刻启动backend/new worker并调用verify-ready，没有显式等待旧heartbeat过期或把旧worker标记stopped。
- 若worker优雅退出不会更新heartbeat status，正常旧版本最后一次心跳在120秒窗口内会被判`old_dispatch_writer_active`，使每次发布在Stage A稳定失败。需要检查worker shutdown heartbeat和现有发布测试是否只验证了字符串顺序而未模拟真实旧heartbeat生命周期。

### Finding P0-D: Stage A正常发布存在确定的旧heartbeat自阻断窗口

- Confirmed: worker退出`finally`只停止本地heartbeat线程，不更新DB `WorkerHeartbeat.status`；`record_worker_heartbeat()`只会写active。compose也未设置稳定`TG_OPS_WORKER_ID`，新容器通常用新hostname/pid生成不同worker_id，不能覆盖旧行。
- `compose stop`后没有等待或受控retire步骤，启动新worker后立即`verify-ready`；因此旧planner/dispatcher/recovery/ai-generation最近一次active heartbeat在120秒内会触发`old_dispatch_writer_active`。现有发布测试仅检查脚本文本顺序，runtime测试反而证明fresh旧行必阻断，没有覆盖正常stop->stage->ready的端到端生命周期。
- 影响：GitHub Actions部署很可能在Stage A每次等待不足120秒时失败，修复代码无法上线，所有业务任务仍旧不完成。Severity: P0 release blocker。

### Remote reconcile workflow boundary

- 仓库中remote case只有受控CLI preview/apply，没有后台worker自动apply。此点与专项PRD“受保护环境、显式actor/hash/approval、禁止自动重发/自动选边”一致，不算实现遗漏。
- 但它意味着任何`inconclusive`都允许业务目标保持held并导致E4失败；这是明确的安全边界，不应在修复时用自动重试或“历史没找到”绕过。P0-B/P0-C的问题在于本可形成确定证据的动作没有进入该闭环，而不是要求自动处理不确定证据。

### Candidate E: 生产配置未fail-closed禁止embedded/legacy writer

- 模板和compose默认`ENABLE_EMBEDDED_WORKER=false`，但生产Settings/`docker-env.sh`没有拒绝共享`.env`显式设为true。backend在stage之前启动并等待healthy；若旧环境漂移为true，embedded `role=all`可能在旧active合同下短暂写业务，随后才被stage fence。
- `VALID_WORKER_ROLES`包含`legacy`，但`FENCED_WRITER_ROLES`与old-writer heartbeat检查不含`legacy`。需要确认legacy drain是否触及本专项Task/Action；若会，preparing与old-writer归零证明有缺口。

### Finding P1-E: embedded worker配置漂移可形成stage前写窗口

- `legacy`角色只处理旧MessageTask/profile/account/archive等链路，不进入Task Center共享dispatch，故不纳入本专项fence可接受。
- `ENABLE_EMBEDDED_WORKER=true`时backend以role=all运行Task Center，进入preparing后会被`task_center`角色fence；但backend启动到`stage`提交之间仍沿用旧active合同。生产模板默认false，却没有Settings/部署脚本硬拒绝true。
- 影响依赖环境漂移，不是默认必现；定级P1。为满足release fail-closed，应在生产配置校验和deploy入口明确要求false，避免旧共享`.env`使迁移窗口出现新业务写。

### Activation ledger audit

- active dispatch claim在确认时只绑定到已进入executing的Action；fence recovery先把Gateway-started改unknown或把pre-Gateway改pending，再调用统一finalize释放claim，未发现`release_dispatch_claim`因status=executing拒绝而泄漏的路径。
- reconcile会逐Window/epoch重算reservation unclaimed、active和scope总数，并在激活前再次验证`active+effective<=26`。这一部分当前静态合同与定向测试一致，未新增会导致任务无法领取的缺陷。

### Finding P0-F: 新worker会覆盖掉自己的合同版本心跳

- worker主循环先用`_worker_heartbeat_metadata()`写`dispatch_contract_version`；但合同active后，Planner、Dispatcher、Recovery的具体drain入口又调用`record_worker_heartbeat(metadata={"limit":...})`，Planner逐Task刷新还会写`phase/task_id`，这些upsert会整体覆盖`heartbeat_metadata`并删除合同版本。
- preparing时drain被挡住，所以`verify-ready`可能短暂通过；activate后2秒循环开始业务drain，DB里大多数时间的planner/dispatcher/recovery heartbeat均无version。`verify-active`和GitHub post-deploy按空版本!=candidate判定`old_dispatch_writer_active`。
- 影响：即便解决P0-D的旧worker退役，新worker也会把自己识别成旧writer，导致激活后校验稳定失败或发生秒级竞态；release无法完成。Severity: P0。现有测试只直接构造heartbeat，没有覆盖真实run_worker->drain metadata覆盖。

### Fairness / persistent cursor audit

- `allocate_window()`使用scope级`opportunity_cursor + 1`作为实际轮转输入，并在分配落库后推进scope cursor；因此不是只依赖单Window内的allocation epoch。
- parent-first share按`tenant_id + business_task_id + cursor`轮转，每个仍有需求的父任务先取一个名额，剩余容量再按未满足量做平衡分配；超大搜索债务不能吃满26个名额。
- 现有测试连续8个cursor验证搜索与AI/comment/reaction/view共存，另有60轮轮转覆盖；这部分满足“跨多个Window最小机会”口径，未发现新的任务饥饿缺陷。

### Existing test-suite blind spot confirmation

- 运行runtime activation、release gate、群管follow/control button和remote reconcile现有定向集合：`34 passed in 3.27s`。
- 这些测试全绿但没有覆盖真实`run_worker -> role drain -> heartbeat metadata overwrite`，也没有断言群管follow/callback生成ExecutionAttempt并在Gateway前落B0，说明P0-B/P0-D/P0-F不是被现有测试否定，而是生命周期/原子性测试盲区。
- 当前shell无GNU `timeout`；测试通过Python `subprocess.run(..., timeout=60)`执行，满足后端测试硬超时要求。

## Phase 15 repair contract

- 群管follow/callback必须与其他远端mutation一致：Gateway前持久B0，Gateway后先写独立journal，再在B1同步Action/Attempt/准入事实；不增加盲重试或silent fallback。
- worker退出保留heartbeat历史行但显式写`status=stopped`；发布verify只阻断新鲜active旧合同writer。新进程仍使用独立identity，不能靠覆盖旧行伪装退役。
- 所有同一worker的heartbeat metadata写入必须保留`dispatch_contract_version`；由统一heartbeat层合并，而不是要求每个业务drain重复拼合同字段。
- membership权威reprobe需要生成类型化`remote_fact_id`并通过现有RemoteReconcileCase expected hash/CAS应用；旧路径不能先改Action再遗留pending case。
- production Settings与deploy env双层拒绝`ENABLE_EMBEDDED_WORKER=true`，消除backend启动到Stage candidate落库前的写窗口。

### Phase 15 implementation observations

- `GatewayResultEvidence`和`_apply_operation_result`已支持`remote_fact_id + remote_mutation_started`，两个群管动作可复用现有B0/journal基础，不需要新增表或fallback。
- 群管follow成功不仅终结Action，还调用`mark_channel_follow_completed`推进admission；callback成功写`confirmation_click=accepted_waiting_bot_confirmation`。因此remote confirmed不能只把Action改success，必须由类型化finalizer重放这些准入事实。
- Telethon follow/callback成功当前未填写`remote_mutation_started=True`，明显pre-call拒绝路径也不总是`False`；要让journal形成confirmed/absence证据，需要同步修正Gateway返回合同。
- 通用remote reconcile当前只同步Action/Attempt和已有coverage/content/channel账本；membership confirmed还必须在同一CAS事务写`ChannelMembership` joined，群管follow还必须推进admission，否则case虽关闭、后续任务仍会等待。
- `WorkerHeartbeat`已有`status`和JSON metadata，不需要迁移。修复可保留历史行、增加精确worker退役API，并把upsert从metadata整体替换改成合并。
- 首次从旧binary升级时不能只依赖新worker的`finally`；发布必须在`compose stop`成功后、candidate worker启动前由新backend执行受控`retire-stopped-writers`，只处理停止时点之前的fenced role heartbeat。新compose同时写稳定`TG_OPS_WORKER_ID`，后续优雅退出可精确退役自身。
- `fresh active`旧合同测试必须继续失败；新增测试只允许显式`stopped`被忽略，不能用批量删除或放宽stale阈值让闸门通过。
- membership Recovery在probe前会用通用`claim_recovery_actions`提交临时claim owner/token/expiry，而RemoteReconcileCase的Action hash包含这些字段；若直接把probe evidence交给case，会把Recovery自己的控制lease误判为业务漂移。
- 修补必须让case CAS显式理解“它自己持有的recovery claim”：先证明移除本次token后的Action仍匹配case expected hash，再把case expected hash推进到已claim状态，随后才允许probe/evidence apply；不能全局从Action hash删除claim字段，否则会削弱takeover和其他并发检测。
- 测试选择审计确认：群管、activation、release、worker roles、config、remote reconciliation和recovery backpressure文件均有全局`no_postgres`；`test_task_center_role_drains.py`只有部分node标记，整文件选择会触发PostgreSQL schema reset。应按marker/node拆分，保持数据库证据为blocked而非跳过后误报全绿。
- 角色drain的2个失败不是业务回归：两处monkeypatch仍定义`fake_dispatch(session, action)`，而同一工作区Phase 10已把真实调用改成`dispatch_action(session, action, project_task_stats=False)`以把统计投影移到事务C。应更新测试double接收该关键字，继续验证原断言，不能回退生产事务边界来迁就旧double。

### Phase 15 self-audit finding: recovery claim normalization过宽

- 当前首版`remote_reconcile_action_state_hash()`只要`claim_owner`以`recovery:`开头就删除claim三字段，无法证明该claim就是正在执行membership reprobe的本次token；这与PRD“只规范化本次控制lease”的口径不一致。
- 若另一个Recovery owner/token并发接管，通用case CAS会错误忽略这次真实控制权变化。修复必须恢复通用remote case使用完整Action hash，仅在membership持有明确`RecoveryClaim.token`时验证“移除这个精确token后匹配旧expected”，再把case expected推进为包含当前claim的完整hash；不能保留全局前缀忽略。
- 发布退役查询当前排除了`process_type=all`，但worker实际把role `all`记录为`task_center`，因此旧embedded进程仍会以`task_center`被检查/退役；不需要为不存在的`all` heartbeat行放宽查询集合。
- membership inconclusive还存在一个二阶生命周期缺口：evidence apply与冷却结果刷新发生时Action仍带本次recovery claim，外层随后释放claim若不再次推进case expected hash，下一轮即使使用精确新token也无法匹配。修复点必须位于外层release之后；仅在probe helper内部刷新不够。

### Phase 15 code-metric audit

- 新增测试文件、heartbeat、runtime hash与dispatch runtime control的新增函数均低于50行；但本轮扩展后的membership recovery主函数为80行、群管follow终结函数59行、worker主循环58行、release CLI分发53行，超过项目硬限制，必须在不改变行为的前提下抽取命名helper。
- `remote_reconciliation.py`因类型化业务投影增长到589行；应把membership/group-bot类型事实identity与投影拆到单独模块，让核心case CAS保持单一职责并回到500行内。
- `service.py`/`dispatcher.py`/`worker.py`整体文件大小和若干旧函数已有历史性超限；本轮只对实际触碰且新增复杂度的函数做外科式拆分，不扩散为与当前故障无关的大重构。

### Phase 15 regression finding: legacy membership unknown can lack Attempt

- 旧Recovery回归fixture和可能的生产存量允许`Action.status=unknown_after_send`但不存在`ExecutionAttempt`。统一RemoteReconcileCase要求Action+Attempt，首版因此在probe前返回，五个backpressure/cooldown测试失败且真实存量会永久不再复检。
- 不能恢复case外直接成功。正确升级路径是：对membership存量unknown在Recovery持有claim后，先持久建立标记为`legacy_unknown_read_only_recovery`的result-unknown Attempt，冻结Action payload/request identity并创建case，提交后才执行权威只读probe；confirmed/inconclusive仍只经统一evidence CAS落账。

### Phase 15 release-script precision finding

- `compose stop`完成后记录的`workers_stopped_before`当前只有整秒精度；若旧binary最后一次heartbeat发生在同一秒的小数部分，`last_seen_at > 截断后的cutoff`，受控退役会漏掉该旧active行，Stage A仍可能被120秒窗口阻断。
- 该cutoff必须使用生产GNU date的纳秒ISO时间，不能靠放宽查询或等待自然过期；新worker尚未启动且backend已fail-closed embedded，因此stop完成后的高精度边界能精确覆盖全部已停止writer。

### Phase 15 membership evidence semantic finding

- `probe_target_capabilities()`当前总是检查发言权限；但membership payload已有`require_send`。对于只要求joined/可访问的私有频道或关注目标，账号能读取却不能发言是正常状态，现实现会错误返回permission denied，使unknown永远无法confirmed并持续占用准入义务。
- Gateway probe必须显式接收`require_send`：`false`时fresh Session成功resolve且授权即为可访问证据；`true`时继续执行现有发言权限检查。membership Recovery和membership Dispatcher的reprobe必须传冻结payload值，其他调用保持默认发言能力语义。

### Phase 15 recovery entrypoint parity finding

- membership reprobe有两个受claim入口：`_recover_claimed_unknown_action`和Gateway-started stale-executing recovery。首版只在前者释放claim后刷新inconclusive case hash；后者释放同一精确claim后也必须刷新，否则下一轮仍被旧token阻断。
- case expected与“排除本次精确claim”的shadow hash仍不匹配时，首版只返回false，外层可能继续写普通failed/stale投影而case保持pending。该状态已证明存在业务/Attempt漂移，必须在probe前用统一apply将case置`conflict`并写审计，且不得调用Telegram。

## Phase 16 production release audit

- `origin/master`是`origin/release`祖先；release额外包含`ccd9605d`和`87fe0bf0`两次已部署合并。为避免回退稳定线上树，本次应把最终候选SHA先快进到master，再推同一SHA到release触发生产部署。
- 最近release部署run `30677938596`在`87fe0bf0`成功；当前无在途Deploy Production run。
- 工作区的`.planning/.active_plan`切换和AI humanization运行记录删除属于并行任务，不进入本次修复提交；共享调度代码、0134迁移、发布脚本、专项PRD/索引/runtime以及本专项计划属于候选范围。
- 生产目标为硅谷，SSH alias `prod-silicon-root`/`silicon-valley-production-server`指向同一主机；发布仍只经Actions，不在服务器热补代码。
- workflow已新增部署后`verify-active`；生产合同还要求Stage A先停旧worker、纳秒cutoff退役旧heartbeat、stage/verify-ready，再takeover/activate/verify-active。容器healthy或Actions green均不能替代Telegram/任务账本E4。
- 2026-08-01 11:04生产旧证据显示三个郑州AI任务小时success归零、日shortfall 716-741，Action执行被freshness/duplicate/reply缺口阻断，search_join主要阻断为图片验证，且Window存在4条负unclaimed。该证据是本次上线后的对照基线，不是修复完成证据。
- `.planning/production_root_cause_20260801`和`prod-diagnosis.md`记录的就是本次incident原始E4与根因链，应随专项PRD纳入审计提交；`2026-07-31-production-stable-baseline.md`属于更早基线任务，虽然当前origin文档已引用但文件未跟踪，本次不把该并行用户改动混入修复提交。
- 共享专项PRD明确要求三个AI活群出现部署后新的`ExecutionAttempt + remote_message_id`并正确确认原账号coverage；完整自然日五类目标才允许`production_fixed`。因此首轮上线后即使新消息恢复，也只能先判runtime/canary pass，不能提前结束监控。

### Actions run 30690191293 failure classification

- no-PostgreSQL分区完整结果为`2544 passed, 18 failed, 785 deselected`；build/deploy未运行，生产仍是`87fe0bf0`。
- 15条行为失败来自旧AI测试Action未携带新强制`group_content_scope_v1`，因此符合设计地在claim阶段被过滤或在`dispatch_action`前被takeover fence拒绝；不能删生产fence，应更新有效Action夹具显式携带合同版本。包括direct dispatch终结、fairness、group slots、hard-hourly优先级和unknown stance。
- 2条rank-deboost测试仍向`_mark_stale_executing_action`传已删除的`task`参数；生产实现已从Action/reservation关系收口，测试调用需同步签名。
- 迁移单head测试仍固定断言0133，应更新为本次前向head `0134_shared_dispatch`。
- 旧发布顺序测试要求takeover发生在worker启动前，与新PRD Stage A/B冲突；新合同要求worker先以fenced readiness启动并`verify-ready`，再takeover，最后activate。测试应断言`stop < start < verify-ready < takeover < activate`，不能改变生产顺序迁就旧断言。
- 上述18条同步后本地精确复跑`18 passed in 3.98s`；未改生产fence。unknown AI测试同时改为断言memory保持unknown且不建立未确认stance，符合远端事实安全口径。
- PostgreSQL分区为`750 passed, 19 failed, 14 skipped, 2 xfailed`。失败继续分组：4条comment PG清理违反新journal FK、1条journal测试缺tenant/task/account父行、若干旧AI夹具缺scope、1条test double缺`project_task_stats`关键字；其余membership legacy identity与channel remote mutation语义需逐条查明，不能直接改断言。
- PG fixture复核：comment清理确实先删Attempt后删journal，需要只调整测试清理顺序；journal测试应分阶段commit Tenant及Task/Account，避免无relationship对象同flush时的父子排序歧义；journal FK继续保持禁止删除审计证据。
- stale membership用例代表真实存量形态：已有Gateway-started Attempt但缺新版本`gateway_request_identity`。当前只升级“完全无Attempt”的legacy unknown，因此此形态直接抛`gateway_request_identity_missing`并会永久阻断Recovery。这是实现遗漏，不应只改测试；需先补PRD为“无Attempt或旧Attempt缺冻结identity均建立新的read-only recovery Attempt”，保留旧Attempt审计且仅做权威只读probe。
- channel/comment的`unknown_after_send`失败来自测试stub只返回failure type、没有填写新`remote_mutation_started=false`；只有能证明调用未发生远端mutation的permission/unavailable结果才允许重排。需核对真实Gateway是否已填false，再让stub模拟相同权威合同，不能在dispatcher猜测failure type。
- 真实Telethon reply异常映射已对所有非UNKNOWN已知失败返回`remote_mutation_started=false`，成功返回true；Mock comment unavailable也返回false。因此permission测试stub补false是在模拟真实合同，不是放宽生产判断。
- workflow里的view/reaction成功stub普遍只返回`success=True`却无`remote_mutation_started=true`；新journal因此正确按unknown处理。成功stub必须补true；要验证可重排的首轮失败，也必须显式返回false。保留`None`时旧“自动重试成功”断言不再安全。
- 另外5条dispatch/claim失败仍是旧AI Action缺`group_content_scope_v1`；reassignment测试也因两个pending Action被scope filter排除而未进入原有改派逻辑。均只同步有效夹具。

### Actions run 30690970723 failure classification

- 第二轮no-PostgreSQL完整通过，PostgreSQL仅剩6条失败，deploy/build继续被正确阻断，生产未变化。
- 评论membership明确返回`remote_mutation_started=false`后仍转unknown，独立SQLite复现得到异常`gateway_request_identity_missing`。根因是B0已冻结的Attempt snapshot在`_finish_execution_attempt`中被membership延期后的Action result整体覆盖；修复必须保留Attempt原identity/fingerprint并合并新结果，不能放宽unknown安全规则。
- view/reaction的4条失败来自`SessionLocal(autoflush=false)`：Gateway成功路径先添加RemoteFact，通用B1收尾查询看不到pending对象又添加一份，commit批量INSERT触发唯一键冲突并按Gateway-started安全路径转unknown。远端事实改为只由B1收尾单点创建，Gateway路径只传`remote_fact_id`。
- legacy membership测试仍读取旧Attempt并期待其被改成success，与已批准“旧Attempt不可改、追加read-only recovery Attempt”冲突；断言改为旧Attempt保持gateway-started、新Attempt携带source id并success。

### Actions run 30691867621 production activation finding

- 第三轮frontend、no-PG、PostgreSQL 16和三镜像构建均通过，deploy进入生产Stage A；新backend与两个Dispatcher均healthy，candidate为`dispatch-rebuild-v3/preparing`且两分片live、容量13+13。
- `reconcile-ledger`持续占用preparing fence：生产存在7,971个历史Window、62,405个Allocation、320,819个Reservation；executing active claim已恢复为0，但旧实现仍锁定并逐Window/epoch查询全历史账本，两个Dispatcher heartbeat/Scope锁等待超过数分钟。
- 生产只读统计显示当前无未结束Window，但2,491个已结束Window仍有active投影漂移。激活只需按真实Action批量修复这些active投影；已结束Window的历史unclaimed和search release有原owner协议，不应由发布激活重放。
- 正确修补为：未结束Window完整守恒；已结束且active漂移Window/Allocation批量装载并只修active；validation同范围并要求closed active drift=0。禁止通过延长900秒发布timeout掩盖全历史N+1。
