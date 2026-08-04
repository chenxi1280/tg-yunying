# 生产任务履约合同闭合专项 PRD

## 1. 文档状态与优先级

| 项目 | 内容 |
| --- | --- |
| Intake ID | `intake-2026-08-03-task-fulfillment-contract-closure-002` |
| 需求级别 | L3：生产多任务长期欠量与恢复反复失效 |
| 设计状态 | `product_design_complete` |
| 开发交接 | `dev_handoff_ready=true`；须与分类恢复 PRD 一并实现和验收 |
| 生产状态 | `production_blocked`；本文件不代表代码、发布或 E4 已完成 |

本文补齐 `task-fulfillment-classified-recovery-prd.md` 中尚未闭合的执行合同，并 supersede 与下列规则冲突的旧表述：

- 搜索依赖 Window 才产生 assignment，或在 Window 结束后预扣、重算份额、等待二次容量 confirm；
- AI 同群并发仅依靠全局 `context_version`，使第一条提交后其余结果全部作废；
- 暂停、停止、删除仅修改 Task 状态或删除运行行，不建立 worker/Gateway fencing；
- `abandoned_for_day` 既是当日永久终态又能无条件自动复活；
- 本地找不到引用评论即可推定 Telegram 评论已删除；
- 旧 Task 运行状态迁移、quarantine 或 shadow 双写；当前合同只允许 fence、最小远端 tombstone、物理删除和新 Task 重建。

### 1.1 并发一致性原则：无显式跨表锁

当前合同不使用 `SELECT ... FOR UPDATE`、全局 Task/账号锁、跨表锁链、中央 Window/Reservation 或“先锁资源再执行”的调度方式。并发一致性只依赖：

1. 不可变业务身份和数据库唯一约束，重复创建统一 `INSERT ... ON CONFLICT` 回读同一事实；
2. 单行 `version/owner_fencing_epoch/task_lifecycle_epoch` 条件更新，使用 `UPDATE ... WHERE expected_version ... RETURNING` 领取或推进；
3. Gateway 前只复核当前 Task、义务、授权、准入和远端 mutation identity 的版本，不等待其他 Task；
4. 远端结果先追加唯一权威事实，Attempt、Action、义务、coverage、lease 和统计均为可重放投影，不做跨表原子结算；
5. CAS 冲突只回读最新事实并结束本次提交，不持锁等待、不反向加锁、不在同一事务重排另一任务。

PostgreSQL 对单行写入产生的数据库内部瞬时行锁不可消除，但产品合同不把它作为业务互斥、容量分配或公平机制；事务只能包含当前事实的一次短写入，禁止持有数据库事务执行 Provider、Telegram、OCR 或其他网络调用。

## 2. C1 动态账号范围的最终状态合同

### 2.1 状态、目标与事实版本

账号范围唯一键为 `(task_id,target_group_id,account_id,task_day_ledger_id)`，每次资格事实变化推进 `scope_fact_version`。当前必达数和有效目标为：

```text
current_required_account_count =
  count(latest_state in {eligible, recovering, completed})

planned_daily_target =
  max(configured_daily_message_target,
      current_required_account_count)

effective_daily_target = planned_daily_target

target_reduction_overage_count =
  count(confirmed/gateway-started fact legal under an earlier target revision
        but above the later reduced current target)
```

每次 `planned_daily_target` 因任务内账号事实变化而改变，必须递增 `planned_target_revision` 并记录 before/after/reason/effective_at。不再分配或持久化 `completion_ordinal`；稳定数量义务就是唯一执行身份。Planner 只按当前目标幂等补齐或取消未进 Gateway 的稳定义务；Gateway 前仅对当前义务执行 `action_bound -> gateway_started` 单行 CAS 并冻结当时 target revision/target，不更新或锁定任务日总账。数量不靠预扣控制，而由“目标对应的稳定主义务总数 + 每义务最多一次远端 mutation”控制。

明确 `remote_mutation_started=false|pre_accept_rejected` 时，同一义务从 `gateway_started` 释放回 `open`，不新增义务、不烧掉名额；unknown 保留该义务占位。账号后来被权威放弃使目标下调时，旧版本内合法开始的事实写入 `target_reduction_overage_count`，不算 scheduler oversend。目标下调先以账本 version CAS 发布新 target revision，再由幂等收敛器按“额外数量优先、最新创建优先”取消足量未进 Gateway 义务；各义务独立 CAS，已进入 Gateway 的旧版本事实只对账，不建立中央预算锁。

状态含义固定如下：

| 状态 | 是否计当前必达数 | 是否可生成/发送 | 收口规则 |
| --- | ---: | ---: | --- |
| `eligible` | 是 | 是 | 当前任务准入、在线和可发事实均满足 |
| `recovering` | 是 | 否 | 存在已验证自动恢复路线，按事实事件复探 |
| `completed` | 是 | 否 | 已有本任务、该账号、该任务日真实成功 |
| `abandoned_for_day` | 否 | 否 | 当前事实版本没有合法自动恢复路线，终结未进 Gateway 义务 |

`abandoned_for_day` 对该 Task、该目标、该任务日为终态，不允许复活旧 Action，也不允许同日因定时扫描、worker/Task 重启或授权记录刷新而重新加入分母。次日只按新的任务日事实重新评估。该状态绝不写成账号全局冻结；同一账号在其他 Task/目标的资格独立计算。

### 2.2 恢复与放弃判定

- `need_relogin/session_invalid/session_revoked/session_unauthorized`：Telegram 或 Session adapter 权威确认当前授权已不能发送时，立即写 `abandoned_for_day`；不得在同一 Task 日内自动重登、换授权后复活旧义务。
- `cannot_send/write_forbidden/account_restricted/account_banned`：Telegram 权威返回当前账号对该目标不可发送时，立即写 `abandoned_for_day`。入群前尚可执行的 follow/join/confirmation 属于 admission 流程，不等同于权威 `cannot_send`。
- `target_dissolved/peer_invalid/target_deleted`：这是目标级终态；原子终结该 Task/目标下全部未进 Gateway 义务并标记 `target_terminal`，不把参与账号写成全局失效。
- `disabled/deleted/banned/identity_invalid`：直接放弃当前任务日；不得用 AI、签到、代理切换或换号绕过。
- FloodWait/SlowMode 是 Telegram 给出的延后时间，不等于 `cannot_send`；只在 `retry_at < deadline_at` 时保持 deferred，跨过 deadline 则该任务日放弃。网络超时、listener 缺口、数据库异常和 unknown 不是 Telegram 权威不可发送证据，保持 `recovering|unknown_hold`。

权威事实与任务状态必须分层：复用 `tg_account_authorizations` 保存 `(account_id,authorization_slot_id,fact_version)` 版本化权威事实，可被多个 Task 读取；每个 Task 都要按自己的 `task_id + target_id + task_day_ledger_id` 独立物化 `abandoned_for_day`，不得写全局 `frozen_account` 或残留分母。只有使用同一已失效授权槽位的 Task 会分别放弃；使用另一条仍有效授权的 Task 仍按自己的事实判断。目标解散同理：每个引用该目标的 Task 独立进入 `target_terminal`，不把账号判废。

adapter 必须把 Telegram/Session 权威结果标准化后再决策，不能靠模糊字符串或尝试次数：

| 权威结果类别 | 标准事实 | 当前合同动作 |
| --- | --- | --- |
| `AUTH_KEY_UNREGISTERED/SESSION_REVOKED/SESSION_EXPIRED/USER_DEACTIVATED` 或登录状态明确要求重新认证 | `session_invalid|session_revoked|need_relogin` | 所有使用该授权槽位的运行 Task 分别物化当日 abandon |
| `CHAT_WRITE_FORBIDDEN/USER_BANNED_IN_CHANNEL/USER_RESTRICTED`，或 Telegram 成员/权限事实明确该账号不能写 | `write_forbidden|account_banned|account_restricted|cannot_send` | 仅当前 Task/目标/账号当日 abandon |
| `CHANNEL_INVALID/PEER_ID_INVALID` 或独立 peer 事实确认删除/解散 | `target_deleted|target_dissolved|peer_invalid` | 当前 Task/目标终态；不写账号失效 |
| `CHANNEL_PRIVATE` 等无法单独区分“账号无权”与“目标不存在”的结果 | `cannot_send_scope_unknown` | 先终结当前 Task/目标/账号执行路径；不得传播成全局账号或全目标事实，只有独立 peer 证据才能升级为 `target_terminal` |
| `FLOOD_WAIT_X/SLOWMODE_WAIT_X` | `telegram_retry_at` | deadline 内 deferred，deadline 外当日 abandon |
| timeout、connection reset、listener/cursor gap、数据库异常 | `transport_or_observation_unknown` | 不 abandon；保留 unknown/recovering 与原 mutation 防重 |

每次判定必须保存 `reason_code/evidence_type/evidence_version/decided_at/next_probe_trigger`。禁止仅按尝试次数、等待时长或旧错误字符串放弃账号。

## 3. C1 任务生命周期 fencing 与删除合同

### 3.1 生命周期版本

Task 每次启动、继续、暂停、停止、删除都原子推进 `task_lifecycle_epoch`。Planner、GenerationJob、Action、worker claim/lease 和 Attempt 必须固化创建时 epoch。

```text
领取前：task row exists
       AND task.status == running
       AND work.task_lifecycle_epoch == task.task_lifecycle_epoch

Gateway 前：再次执行同一原子 CAS
```

暂停、停止或删除提交后，旧 epoch 的工作只能执行下列收口：

- 未进 Gateway：写 `cancelled_by_task_lifecycle`，释放 worker claim/lease、ContentMix 绑定和实际活跃槽位；
- 已写 `gateway_call_started_at`：禁止假定未发送，进入 `unknown_hold/remote_reconcile`；
- 已取得远端事实：写入与 Task 外键解耦的最小远端 tombstone，不能倒灌到新 Task。

worker 在数据库事务外持有旧对象也必须在 Gateway adapter 的最后边界重新读当前 Task epoch；旧 epoch 不得发起远端 mutation。

### 3.2 删除与 tombstone

删除任务后，运行查询、Planner、Generation、Dispatcher 和容量统计必须立即不可见。删除不是一个跨崩溃的伪“单事务”，而是由持久 operation 驱动的分阶段短事务；正式状态机为：

```text
fencing -> snapshot_committed -> tombstone_verified -> deleting -> committed
任一非终态 -> failed(resume_stage)（Task 继续被 fence，不能恢复运行）
failed --同 snapshot hash 的受保护 resume--> resume_stage
```

1. `fencing` 使用带 `expected_task_lifecycle_epoch` 的单行 CAS 推进 epoch，并写 `delete_state=fencing + task_delete_operation_id`；该 CAS 成功后，所有运行查询和 Gateway writer 都必须排除该 Task；
2. `snapshot_committed` 事务在 writer fence 后冻结待 tombstone 与待删除集合，逐项写 operation item、count/hash 和稳定业务身份；相同 `task_id + expected_lifecycle_epoch` 只能回读同一 operation，不得重新取漂移集合；
3. 对 `success|gateway_started|unknown` 只幂等写 `remote_mutation_tombstones`：保存 tenant、mutation kind、不可逆 mutation-key hash、gateway request identity hash、remote-started、terminal state、可选 remote fact identity/hash、reconcile state 和时间；不复制 Task 配置、义务、ContentMix、普通日志或可恢复运行 payload。open reconcile case 以 `original_task_id` 值关联 tombstone，不反向外键到 Task；
4. 校验 tombstone 的逐 item、count/hash 与冻结快照完全一致后提交 `tombstone_verified`；任一缺件或错绑只回滚当前批并将 operation 置 `failed`，Task 继续不可运行，禁止进入删除；
5. 只有 `tombstone_verified` 可进入 `deleting`，按冻结 item 幂等物理删除全部可重建义务、Action、worker claim/lease、ContentMix、投影、配置子表和 runtime detail；每批保存删除 checkpoint，禁止重新扫描扩大范围；
6. 所有冻结 item 均核销后，最后物理删除 Task 主记录及剩余可重建子表，并把 operation 原子终结为 `committed`。

任何物理删除都不得使旧 assignment、callback 或远端 mutation 以相同副作用身份重放。系统不保留 Task 配置 tombstone；持久 delete operation 只保存删除授权、冻结集合 hash/count/checkpoint 和审计，remote tombstone 只保存最小远端防重/对账身份。删除后不恢复原 Task；运营需再运行时重新提交当前配置，创建新 Task ID、新 lifecycle epoch 和新任务日账本。同日重建明确从 `confirmed=0` 开始，旧 Task 成功不抵扣新目标。

物理删除接口必须要求 `tasks.manage + explicit_delete_confirmation`，确认内容至少绑定当前 `task_id + task_title + expected_task_lifecycle_epoch`；同日删除重建还必须确认 `same_day_recreate_resets_progress=true`。服务端以 epoch CAS 启动 operation，记录操作者、理由、审批引用、before/after hash 和 tombstone 计数；fencing 成功后响应 `202 Accepted`，只有 `committed` 才标记物理删除成功。Recovery 只按 `task_delete_operation_id + stage_version + frozen item/checkpoint` 继续，已提交阶段不回滚、不重复写 tombstone、不扩大集合。

删除是异步持久 operation，不允许一个 HTTP 请求等待全部 tombstone 和物理删除批次：

- `DELETE /api/tasks/{task_id}` 在 `fencing` 事务成功后返回 `202 Accepted + task_delete_operation_id + state=fencing`；此时 Task 已从运行查询和新 Gateway writer 中隐藏，但前端只能显示“删除处理中”；
- `GET /api/task-delete-operations/{operation_id}` 返回当前 stage/version、冻结总数、已写 tombstone/已删除计数、checkpoint、last_error 和最终 `committed_at`，读取要求 `tasks.view` 且仅限同 tenant；`GET /api/task-delete-operations?original_task_id=...` 可在客户端丢失 202 响应后找回同一 operation；
- `POST /api/task-delete-operations/{operation_id}/resume` 只接受 `failed`，要求 `tasks.manage + approval_ref + expected_stage_version + expected_snapshot_hash`，只恢复原 `resume_stage`；
- 查询到 `committed` 后才显示“物理删除完成”。`failed`、进程退出或客户端断线都不得解除 Task fence、返回可恢复配置或创建第二个 operation。

### 3.3 Recovery/worker 所有权 fencing

所有可接管工作统一固化 `owner_id + owner_fencing_epoch + lease_expires_at + owner_policy_version + work_version`。owner 续租、takeover 和终结提交使用同一合同：

1. 续租 CAS 当前 owner/epoch/version，无法匹配则旧 worker 立即停止写入；
2. takeover 先用 `populate_existing=True` 或新 Session 强制重读 work 和 `WorkerHeartbeat`，禁止使用 identity map 旧值；不执行显式行锁；
3. 只有 heartbeat 与 lease 均按固化 policy 过期时，才以旧 `owner_id + owner_fencing_epoch + work_version + lease_expires_at` 为条件执行单行 CAS，递增 epoch 并替换 owner；
4. 任何 ready/success/failed/cancelled/Recovery apply 必须带 owner epoch 和 work version，旧 owner 晚到结果统一写 `stale_owner_rejected`，不改写业务状态；
5. Gateway-started 工作不因 owner 过期而重发，只创建 remote reconcile case。

所有 lease/heartbeat 的时间判断只使用同一 PostgreSQL 事务内取得的 `db_now=clock_timestamp()`；`heartbeat_observed_at`、`lease_expires_at` 和审计时间均存为 UTC-aware `timestamptz`。takeover 条件固定为 `heartbeat_observed_at + heartbeat_stale_after <= db_now AND lease_expires_at <= db_now`，禁止使用 worker 本地时钟、Python naive datetime、容器时区或先后两个不同的 now 值参与 CAS。policy 的时长可以配置，但不能改变数据库时钟的唯一权威性。

### 3.4 同账号并行的 transport 隔离

同一账号跨任务并行不等于共享可变客户端上下文。Telegram Session transport 必须支持并发 request/future 正确关联；每次 RPC 固化 `rpc_id + account_id + authorization_id + task_id + action_id + remote_mutation_key`，响应和异常只能回写对应 Action/Attempt。adapter 若不能证明并发安全，必须为并行 RPC 建立彼此隔离的 transport channel/client instance，禁止退回账号级全局串行，也禁止复用会被下一请求覆盖的 mutable request context。

账号级 FloodWait、authorization revoked 和 session invalid 作为版本化远端事实广播给该账号所有后续 Gateway 校验；群级 SlowMode 只约束对应 peer。一个 RPC 的取消、timeout 或重连不得取消、重发或改写另一个 RPC。数据库事务和远程调用之间不得持有账号互斥锁。

## 4. 搜索 assignment 连续产生并直接执行合同

### 4.1 事件驱动 solver 与执行边界

搜索 solver 由开放义务、候选事实版本变化、assignment 终态和 search worker 空闲事件持续唤醒；只要存在一个开放 click 义务和一条新鲜可执行路径，就立即以单个事务固化：

```text
assignment_id + obligation_id
account_id + authorization_id + proxy_binding_version
keyword/target path + task_lifecycle_epoch
solver_input_hash + assignment_version + search_execution_policy_version
obligation_deadline_at
```

assignment 成功落库即取得到 `obligation_deadline_at` 为止的一次直接执行权。合同中不存在 solver Window、handoff grace、跨窗预扣或二次分配。

超过业务截止时间永不开始 Gateway。solver 每次只按真实空闲 search worker 槽位物化有限 assignment，禁止把全天目标一次预绑定成陈旧路径；同一义务唯一键必须排除已落库或 Gateway-unknown assignment。

assignment 行本身就是持久待执行工作，不依赖进程内事件或消息队列消息存活。search worker 通过有界候选 ID 查询后，对每条 assignment 执行带 state/version/owner 条件的 `UPDATE ... RETURNING` 单行 CAS 领取；通知只用于缩短唤醒延迟。进程重启、通知丢失或 worker 崩溃后，其他 worker 依据数据库时间和 owner fencing CAS 接管过期 lease，继续同一 assignment，不创建替代身份。

### 4.2 直接领取与取消

Dispatcher 以单行 `UPDATE ... WHERE state='open' AND version=:expected RETURNING` 直接领取已落库 assignment/Action，只执行下列 CAS：

- assignment/Action 仍为 open 且未被其他 worker 领取；
- task lifecycle、业务 deadline、账号资格、授权与 proxy binding version 仍一致；
- obligation 未 confirmed、未 Gateway-started/unknown；
- assignment 固化的 policy revision 和路径身份完整。

不校验 Window、中央份额或二次容量。assignment 单行 CAS 成功即取得执行权；Action 只投影该 claim identity，不与 assignment 建立跨表原子状态切换。仅 lifecycle/deadline/资格/binding/dedupe 失效才以唯一取消事实终结，原义务随后由连续 solver重新求解。Gateway-started/unknown 仍占用原 click 义务防重，不换账号/代理/路径重发。

### 4.3 极搜页面状态持久化

每个 assignment 唯一拥有一个持久 `SearchProtocolSession`，正式 phase 为：

```text
assignment_created -> keyword_sent -> hot_list_page -> group_category
  -> verification_required -> result_page -> target_found
  -> click_started -> click_unknown | completed | failed
```

每次远端发送、页面转换和 callback 前后都以 `phase_version + request_identity + page_fingerprint` 做 CAS，并在进入下一步前提交当前 phase；worker 崩溃后只能从已提交 phase 继续。`keyword_sent/click_started` 后结果不明分别保持原 request 的 unknown，不得重发关键词或目标点击。

`hot_list_page` 是正式协议状态：只允许点击版本化 `BotProtocolSample` 中明确批准且与当前页面 fingerprint 匹配的“群聊/群组”类型 selector；selector 缺失、样本过期或页面不匹配写 typed failure，不使用 OCR/AI 猜按钮。`result_page` 的下一页 identity、目标公开引用和 challenge fingerprint 均持久化；分页、验证码和点击都不能依赖仅存于 worker 内存的页面对象。

## 5. 搜索 transport 与代理事实链

搜索链路必须分别保存以下事实，禁止合并成“代理失败”：

```text
subscription_parse_fact
  -> proxy_node_egress_fact
  -> account_authorization_fact
  -> active_binding_fact
  -> assignment_binding_version
  -> gateway_transport_fact
  -> target_click_observed
```

- 节点以 `subscription_id + node_key` 对账；被外键引用的旧节点退役，不批量删除。
- `proxy_node_egress_fact` 必须来自该节点实际 SOCKS/Mihomo 公网出口测试，不以配置存在代替。
- assignment 固化账号、授权槽、代理节点和 binding version；Gateway 前逐项复核。
- parse、egress、authorization、binding 和 transport 事实都必须保存 `observed_at/fact_version/stale_after/policy_version`；超过 stale-after 不得作为当前可执行证据。assignment 固化的 fact/policy version 在 Gateway 前必须仍新鲜，否则按 binding/资格失效取消，不静默直连或换绑。
- binding 失效且未进 Gateway 时释放原 assignment，按新事实进入下一求解；不得把旧 Action 换绑后继续执行。
- 连接超时、reset、代理鉴权失败分别记录；仅目标节点故障不得污染其他节点或 interaction lane。
- Gateway unknown 保留原 click 义务的 `unknown_hold`，不能换代理重发。

## 6. C2 群管提示恢复与配置变更

C2 分成两层，禁止把远端事实锁死在某个 Task：

- Task 配置/投影层：`tasks.group_ai_prejoin_channel_ids UUID[] NOT NULL DEFAULT '{}'` 持久化 0～3 个运营配置频道；`TaskGroupBotAdmission` 仅保存该 Task、账号、目标群当前采用的 policy/version、所引用事实和 ready 状态。
- Task 无关远端事实层：按账号自己的 Telegram 视角持久化 `configured_channel_follow`、`dynamic_channel_follow`、`requirement_confirmation`、`post_follow_visibility` 四类事实。事实业务键不得包含 `task_id`，但必须包含账号、目标 peer/频道、远端 mutation 或 observation identity、fact version；多个 Task 可引用同一仍新鲜事实，不能互相改写 Task 状态。

配置频道必须来自 Task 表字段，不能只放通用 JSON、缓存或群级规则。Task 创建/编辑时校验稳定 OperationTarget ID、去重且最多 3 个；空数组表示没有运营预关注要求。

`fact_first_v3` 的 AI 正文生成前置门禁只能调用 `TaskGroupBotAdmission + AccountGroupAdmissionFact` 新链路；禁止继续读取旧 `GroupBotAdmission/group_bot_global_rules` 或把旧 `group_bot_admission_state` payload 当作当前 Task 准入结论。未 ready 时本轮只能创建/推进当前 Task+账号 observation 并立即释放 GenerationJob/Action claim，禁止加载 Provider 凭据或调用 Provider；ready 后把 `task_group_bot_admission_id + version` 固化到 Action，再允许正文生成。`TgAccountAuthorization` 缺行只是本地投影缺口，不能当成 Telegram 不可发送事实；存在账号 Session 时仍以 `account_id + session identity hash` 建立 30 秒 viewer surface。只有 Session 缺失/失效、需重登、账号停用、目标群解散/不可访问或 Telegram 明确拒绝等权威结果才只放弃当前 Task 内该账号并释放未进 Gateway 义务，不得形成跨 Task 封禁。

Task 准入必须有独立物化入口，不能依赖已经 ready 的 coverage 或既有正文 Action 偶然触发：`pending_admission` coverage 在 `fact_first_v3` 中可物化当前账号的空正文 Action，由 AI generation lane 先推进 C2、释放 claim，ready 后再生成。历史因 `current_authorization_missing` 误放弃的 Task admission/coverage 必须在同一 Task 内自动重开 observation；对应 `replan_required` 数量槽可释放重建，不能继续把 691 个 missing admission 留在等待态。

存量 `replan_required|unmaterialized` CycleSlot 的选择必须与本轮可推进账号精确相交：先按 `ready -> 已到 30 秒的 observing/requirements_pending -> observing -> admission missing` 选择账号，再只重建这些账号各自冻结的主数量槽。禁止先按旧 cycle 顺序截取固定批次、随后才过滤账号；否则批次前部的 abandoned admission 会持续遮挡后部账号。`fact_first_v3` 不得把 admission=`abandoned` 的 waiting 账号回填为 Planner 候选；未建 admission 的账号仍可物化空正文 Action，已到期 observing 必须由该 Action 在 AI generation lane 优先复查并闭合 30 秒观察。

C2 空正文 Action 物化时，`pending_admission` coverage 必须直接以同一 Action 的唯一业务身份进入 `reserved`，与 `ready` coverage 使用同一 reservation token/Action 唯一绑定；这只是防止同一覆盖义务重复建单，不是容量、速率或预算预扣。不得先要求 coverage=ready 才允许建 Action，否则 C2 永远没有执行载体并持续制造 `unmaterialized` 槽。Action 等待观察期间保留该绑定；准入 ready 后原 Action 继续生成和发送，权威不可发送终态或 pre-Gateway 失败则按原 coverage 身份释放/放弃，禁止另建替代义务。

配置频道全部成功且已确认在群后，从数据库时间记录 `observation_started_at`、当前 viewer cursor、`observation_version` 和不可变 observation surface identity，建立连续 30 秒的账号视角观察。v1 只允许：

```text
surface_kind = target_group_control_stream
surface_peer_id = target_group_peer_id
viewer_account_id + viewer_authorization_id
listener_instance_epoch + listener_policy_version
observed_start_cursor + observed_end_cursor
surface_identity_hash
```

私聊 bot、其他群/频道、普通成员转发、前端缓存和非该授权槽 viewer 的事件不属于本观察面，只保留审计，不能驱动通过或失败。不得在数据库事务内阻塞 30 秒；持久化 `no_prompt_pass_at = observation_started_at + 30 seconds`，由 worker 到时执行闭合 CAS：

- 连续 30 秒内没有出现属于该账号的可信群管控制提示，且 cursor 连续、`observation_gap=false`，写四类事实中的 `post_follow_visibility(outcome=no_prompt_30s_passed)`，该账号视为已通过群机器人验证；
- 30 秒内出现可信提示，立即进入 requirement 解析、关注与确认流程，不能再按“无提示”放行；
- 网络断开、Session 失效、listener/cursor gap、surface peer/authorization/listener epoch 变化或无法证明完整观察，不是“没有提示”，必须递增 observation version 并重建连续观察区间，或按账号不可发送合同放弃；
- ready 后、首条正文 Gateway 前出现新的可信提示，推进 requirement/admission version，使旧 ready 失效。

### 6.1 失效提示恢复

原 `source_message_id` 精确读取为空或已移出窗口时，必须使用该被拦截账号自己的 Telegram 视角读取最新可信群管控制消息：

1. 校验 viewer peer、可信 bot、message fingerprint，以及“归一化展示名精确匹配 + 原提示中群聊要求链接/按钮”的组合收件人证据；
2. 找到匹配新提示时，旧 callback 写 `superseded`，当前 admission version 原子重绑新 source；
3. 没有匹配提示时，废弃旧 source，以当前 viewer cursor 重新开始同账号连续 30 秒观察；期间出现新提示即重绑，完整 30 秒零提示且零 observation gap 则写 `no_prompt_30s_passed`；
4. 网络、Session 或 listener 读取异常写 `group_bot_confirmation_live_fetch_failed`，不能当作提示不存在；
5. callback unknown 只复探原 fingerprint，不再次点击。

### 6.2 已在群内与配置 revision

- 账号已在目标群时不重复 join；仍先补齐配置频道，再从当前 viewer cursor 执行群管提示/visibility 流程。
- 配置频道、可信 bot 或 policy revision 变化时递增 `admission_version`；旧 version 未进 Gateway 的 Generation/Action 失效并释放，Gateway-started/unknown 继续原版本对账。
- 0～3 个配置频道彼此无依赖时并发关注；所有配置关注成功后才进入 join/群管提示阶段。动态频道、确认 action 和可见性探针按 requirement 依赖图并发推进，不使用固定顺序轮询。
- 动态群管频道和 requirement action 不受“运营配置最多 3 个频道”或“一条提示最多 0–1 个点击”限制，但每一项都必须来自同一可信提示并固化 `source_message_id + fingerprint + requirement_action_key`。无法完整执行时显式 blocked，不能截断集合；每个 action key 最多一次 success，unknown 只复探不重点击。
- requirement action 按依赖图调度；自身频道关注等依赖满足后，所有不同 `remote_mutation_key/callback/button identity` 的 action 并发执行。只对同一远程副作用做幂等串行；单个 action blocked/unknown 不得暂停其他无依赖 action。
- 组合收件人证据必须在当前同群 `blocked/admission_pending` 账号中唯一；若多个账号拥有相同归一化展示名且同一提示链接无法区分，写 `recipient_ambiguous`，等待 reply relation、viewer-specific prompt 或新可信提示补证，不猜测点击账号。
- “数量不限”只表示不设业务数值上限，不表示无限重扫：单个不可变 source fingerprint 只物化该消息快照内的有限 action key 集合；新提示必须形成新的 source/fingerprint/version。重复按钮、URL 规范化等价项和已成功 key 不得再次物化；可信 bot 持续产生新要求时 admission 保持未完成并受任务 deadline 管理，不能在单个事务或循环内无界执行。

### 6.3 requirement 集合闭合与 ready CAS

每次可信控制消息、按钮集合、配置频道、observation cursor 或 surface identity 变化都递增 `requirement_set_version`，并按规范化排序计算 `requirement_set_hash`。进入 `ready` 不显式锁 admission，只允许一条带 expected version/hash 的单行 CAS 同时满足：

```text
expected_admission_version == current_admission_version
expected_observation_version == current_observation_version
expected_observed_end_cursor == current_observed_end_cursor
expected_surface_identity_hash == current_surface_identity_hash
expected_requirement_set_hash == current_requirement_set_hash
observation_gap == false
all requirement actions == success
open_or_unknown_requirement_action_count == 0
post_follow_visibility_fact.version == current requirement/observation version
AND (all observed requirements success OR post_follow_visibility_fact.outcome == no_prompt_30s_passed)
```

任一 CAS 不匹配保持 admission 未完成并重新物化差异 action，不能使用旧 visibility 事实放行。ready 提交前或首条正文 Gateway 前观察到新的可信提示时，原子递增 requirement set/admission version，旧 ready 失效；已 Gateway-started 的正文仍按原身份对账，不以新要求伪造未发送。

## 7. C3 义务投影与事实先行完成

`FulfillmentObligationProjection.state` 完整集合为：

```text
open | recovering | materializing | action_bound | executing |
unknown_hold | confirmed | abandoned_for_day |
cancelled_by_task_lifecycle | blocked | shortfall | remote_reconcile_only
```

投影不是第二套总账。所有状态更新以任务专用义务账本的 version CAS 为准。

数据库唯一约束统一为：`FulfillmentObligationProjection` 对 `(obligation_type, obligation_id)` 唯一；ContentMix 投影对 `(obligation_type, obligation_id, materialization_version)` 唯一；Action 对同一义务只允许一条非终态记录的 partial unique index。应用层先查再写不能代替这三条约束，唯一冲突必须回读现有记录继续同一物化过程。

物化允许分为多个短事务：义务 CAS `open -> materializing`，按义务/version 幂等创建 ContentMix 投影，再幂等创建并绑定 Action。每步提交后都可恢复；任一步失败只释放 materialization lease 并从缺失步骤继续，不确认、复制或丢失主义务。所有步骤均为单行 CAS 或唯一键插入，不使用显式行锁。

`visible_confirmed/target_click_observed/typed_remote_fact` 使用“事实先行、投影收敛”，不做跨表原子事务：

1. Gateway 结果先以 `remote_mutation_key + gateway_request_identity + fact_kind` 唯一键追加 `fulfillment_remote_facts`；重复结果只回读同一事实；
2. 该事实一旦提交就是唯一业务完成真相。任何 worker 在执行前发现同 mutation/义务已有 confirmed/unknown 事实，都不得再次进入 Gateway；
3. projector 分别以 `fact_id + expected_version` 单行 CAS 终结 ExecutionAttempt、Action、主义务、可选 coverage 和 worker lease；顺序不影响业务完成，不在一个事务中更新多张业务表；
4. projector 崩溃或 CAS 冲突时保留事实并重放缺失投影，不能回滚远端事实、创建第二条 Action或再次发送；
5. Task stats 只从事实及已收敛投影异步生成。允许短暂出现 `fact=confirmed, Action/obligation=projecting`，但不允许 `Action/obligation=confirmed` 且没有唯一 remote fact。

因此最终结算没有跨表锁序；并发 worker 只竞争唯一事实插入或各自单行 version CAS，不因另一个 Task/Action 的提交顺序互相等待。

### 7.1 unknown 的权威收口矩阵

`unknown_hold -> open` 不是普通重试。RemoteReconcileCase 必须固化 `task_type + mutation_kind + gateway_request_identity + expected_action_hash + evidence_policy_version`，并只接受下表对应的权威结论：

| mutation | `confirmed` | `safely_not_executed`，才可重开原义务 | 无法证明 |
| --- | --- | --- | --- |
| AI 群消息/频道评论 | 同账号、同 peer、同 reply/source、同正文 fingerprint 的远端 message id | 仅接受 Gateway journal 证明 `remote_mutation_started=false`，或 Telegram/adapter 返回绑定同 request identity 的明确 pre-accept rejection；历史中不存在消息不能证明未发送 | 保持 `unknown_hold` |
| 点赞/reaction | 同账号对同 source revision 的目标 reaction 可读存在 | 仅接受 mutation RPC 未开始/在服务端接受前被明确拒绝的回执；当前 reaction 不存在不能证明从未执行。若 adapter 是“设置期望状态”的真实幂等 API，只能重放同一 mutation identity，不能创建新义务 | 保持 `unknown_hold` |
| 浏览 | 类型化远端 fact 或 adapter 明确返回已应用 | 仅接受调用前终止或服务端明确未接受；当前/聚合 view 状态均不能形成 negative evidence | 保持 `unknown_hold`，不得按聚合 view 数猜测 |
| 搜索 callback/目标点击 | 原 challenge/page transition 或 `target_click_observed` 与 request identity 匹配 | 仅接受 callback RPC 未发起或 Telegram 返回绑定同 request identity 的明确未接受；相同 fingerprint、相同页面或无 transition 都不能证明 callback 未执行 | 保持原 click 义务的 `unknown_hold` |

reconcile apply 先以 `case_id + expected_case_version + evidence_version` 单行 CAS 追加唯一 reconcile decision fact，再由 projector 分别推进 Action/Attempt/义务；不跨表加锁。迟到或较弱证据只写 `stale_reconcile_evidence_rejected`。`safely_not_executed` 必须保存 `transport_started=false` 或服务端 pre-accept rejection receipt、request identity、adapter contract version 和 policy version；“未查到”、历史窗口完整、当前状态不存在、页面没变化、超时或换账号看不到均不是安全重开证据。

### 7.2 永久 unknown 的运营终态

`unknown_hold` 在业务 deadline 前持续按原 request identity 对账；不得建立替代义务。到 `deadline_at` 仍无权威正/负结论时，不再保持运行态占用 worker：

- 追加 `unknown_deadline_closed` decision fact，将义务投影为 `remote_reconcile_only`，任务日投影为 `closed_with_unknown_shortfall`；
- 释放执行 lease/实际槽位，但保留 Gateway journal、RemoteReconcileCase 和最小 `remote_mutation_tombstone`，永久阻止自动重放；
- `Action`、搜索 assignment 与原始业务义务统一写短终态 `closed_unknown`；任务日与履约投影才写产品终态 `closed_with_unknown_shortfall`。禁止把长产品态写入 `Action.status`，也不得用字段截断或异常重试代替收口；
- Recovery 先以 `action_id + action_version` 单行 CAS 关闭 Action，再追加唯一 `unknown_deadline_closed` decision fact，随后分别收敛义务、assignment、reconcile case 和任务日投影。原始业务义务即使被历史 writer 错写为 `open`，只要仍绑定该 unknown Action 且没有 confirmed fact，也必须关闭为 `closed_unknown`，不得重新进入执行；
- 搜索空闲取数必须跳过已有 `action_bound/claiming/claimed/executing/gateway_unknown/confirmed/closed_unknown` assignment 的义务；仅 `safely_not_executed/released` 可重新分配。不可重放义务不得因 ordinal 更小而长期占住批次首页、饿死后续开放义务；
- confirmed 不增加，任务日 E4 为 `shortfall`，前端展示 unknown 数、mutation kind、首次/最后对账时间和“等待权威证据”，不能显示成功或继续执行；
- deadline 后到达正向权威事实时，只修正历史 confirmed/shortfall 投影并保留 late fact 审计，不触发新发送；到达 `safely_not_executed` 时只把历史原因收口为明确未执行，过期任务日仍不重开；
- 运营只可查看证据、补充权威 evidence 或接受业务短缺；不提供“强制成功”“清空 unknown”“换账号重试”入口。

## 8. C5 AI 独立并发与单 Provider Key

### 8.1 GenerationJob 生命周期

```text
pending -> generating -> ready
                    \-> superseded | failed | cancelled
```

领取 `pending -> generating` 时用 GenerationJob 单行 CAS 同时写 `generation_owner_id/generation_lease_epoch/generation_started_at/lease_expires_at/generation_lease_policy_version`，并让其他 worker 立即可见。owner 按 policy 续租；takeover 以数据库时间读取已过期候选，再直接 CAS 旧 `owner_id + generation_lease_epoch + job_version + lease_expires_at` 后递增 epoch，不执行显式行锁或跨 job 锁。失去 lease 的旧 worker 续租、ready、failed 或取消提交均写 `stale_generation_owner_rejected`。Provider 调用不持有数据库、任务、账号或 Telegram 锁。

GenerationJob 的候选筛选和 CAS 到期条件由 PostgreSQL 比较 `lease_expires_at <= db_now`；Python 仅在已读取行上做二次判断时，必须先把 PostgreSQL 返回的 offset-naive 北京时间墙钟与应用 offset-aware 时间规范到同一 `Asia/Shanghai` 语义。ORM UPDATE CAS 必须使用 `synchronize_session=false`，禁止 SQLAlchemy 再用 Python evaluator 重算数据库 where 条件。禁止直接比较两种 datetime、禁止把 naive 值误标为 UTC，也不能因单个过期 job 的时间表示差异让整个 generation worker drain 失败。三个 generation worker 任一轮失败都必须暴露完整异常；E4 需要看到三个 worker 持续 claim/finish 且多个 running AI Task 都产生远端事实。

每个 GenerationJob 读取目标群最近真人上下文时，查询固定为 `tenant_id + group_id + is_bot=false + content<>''`，按 `coalesce(sent_at,created_at) DESC,id DESC` 取最多 `chat_history_depth` 条；数据库必须提供与该过滤、表达式排序完全一致的 partial expression index。禁止依赖只含 `sent_at` 的旧索引后再做并行排序，也禁止通过降低 worker 并发、缩小上下文深度或增大 PostgreSQL `/dev/shm` 掩盖查询缺索引。E2 必须以 PostgreSQL `EXPLAIN` 证明无 `Seq Scan/Sort/Gather`；E4 必须证明三个生成 worker 并发读取时不再出现 `could not resize shared memory segment`，并且 GenerationJob、Action 与 Telegram 远端消息事实持续增长。

### 8.2 direct 独立提交与强上下文 CAS

Planner 为可并发计算的义务分配稳定 `generation_sequence` 和 `context_snapshot_version`；sequence 只用于审计和复现，不是发送闸门。同群生成与提交规则为：

- direct 且没有引用/素材强依赖：每个结果 ready 后直接进入自己的发送前重检与 Gateway，不等待较早 sequence；发送前基于最新上下文执行重复、矛盾、质量轻量重检，不要求当前 context version 等于生成快照；
- reply、强上下文或素材绑定：目标 revision/context 变化即 superseded，重开原义务；
- 任一结果只能完成自己的义务，不能被后到结果覆盖。

一次上下文推进不会无条件废弃同批所有 direct 结果；较早 job 的 pending/failed/superseded/cancelled 不得堵塞后位 direct。并发不允许跳过发送前准入、内容质量和重复检查。

### 8.3 Provider 容量

系统任一时刻只允许一个 active AI Provider key version。所有模型和全部 generation worker 使用同一个 active key，模型只是请求参数，不得各自配置另一把 active key：

```text
provider_key_version = (provider_id, secret_ref, version, active)
shared_key_bucket    = (provider_key_version, quota_policy_revision)
optional_model_bucket = (shared_key_bucket, model_id, model_policy_revision)
```

数据库以 partial unique index 保证 `active=true` 最多一行；Secret 只保存密钥管理引用，不落明文。请求按共享 key bucket 领取真实 inflight/RPM/TPM，模型子桶仅在 Provider 明确存在模型级限制时启用；任一 token 领取失败不得形成半消费。轮换在一个事务中停用旧 version、激活新 version；旧 in-flight job 继续按旧 version 对账，新 job 只读新 active version。缺失 active key、存在多个 active key 或 policy 过期均显式阻断并报警，不静默选 key。

同一 active Provider key 可以用请求参数调用该 Provider 同 family 的多个模型；例如 active 行保存 MiniMax-M3 时，MiniMax-M2.5 fallback 复用同一 base URL、header 和 secret，只覆盖请求 `model`。不得为同 family 的每个模型各建一条 active Provider，也不得拿另一 family 的 key 冒充共享 key。创建或激活新 Provider 时，同一事务先停用其他 active 行、把租户默认 Provider 指向新行，再提交；数据库唯一索引作为并发最终闸门。

## 9. C6 OCR 安全收口

- `BotProtocolSample` 必须为每个 verification 变体声明 `refresh_mode=challenge_replaced_on_rejection|approved_refresh_callback|none`；`challenge_safety_policy` 只限制预算/deadline，不能凭空创造 refresh 方法。
- A 无输出/非法/不命中候选时，B 可在同 fingerprint 上识别；A callback 被拒绝且页面仍是同 fingerprint 时才允许 B 提交。若拒绝已经产生新 fingerprint，旧 challenge 立即终结，新 challenge 从 A 开始，禁止对旧 fingerprint 再试 B。
- OCR A 与 OCR B 对同一 fingerprint 得到相同答案时，A 已被权威拒绝后不得再次点击相同答案。
- B 无安全答案或被拒绝后：若 Telegram 已返回新 fingerprint，将该远端转换记为 `challenge_replaced_on_rejection` 并从 A 重开；若仍是同 fingerprint，只允许点击协议样本中明确审批的 refresh callback；`refresh_mode=none` 或当前极搜样本没有独立 refresh callback 时写 `refresh_not_supported` 并结束 Attempt。
- `/cancel`、`/start`、重发关键词、点击未知按钮或仅刷新本地页面都不是 refresh。refresh callback unknown 保持原 fingerprint `unknown_hold`，不得再点。
- policy 必须显式、版本化、可审计，不得在代码内使用隐式无限循环或静默默认次数；只有权威取得新 fingerprint 才能重新从 A 开始。

## 10. C8 来源与引用远端判定

`reply_target_missing` 先表示 `local_target_unresolved`，不能直接写“Telegram 已删除”。处理顺序为：

1. 校验同一 `source_message_id + source_revision` 的本地索引和 listener cursor；
2. listener cursor 落后时触发幂等 resync，不重建重复义务；
3. 从同租户、已确认加入对应 peer 且 Session 可用的候选账号池中随机打乱一次，持久化 `probe_run_id + random_seed + candidate_snapshot_hash + random_order + viewer_account_id + source_message_id + source_revision`后按该顺序精确读取；同一 probe run 重试复用已固化顺序，不每次重新抽样。
4. 账号权限/未入群/Session 终态失败时只将该账号从本次候选池删除，`remaining_probe_accounts - 1`后随机取下一个；同时保存带 `reason_code/peer_scope/fact_version/observed_at/stale_after` 的 typed fact。后续 probe run 按仍新鲜的账号授权事实或账号—peer membership/visibility 事实过滤候选，但不得把本次失败写成全局冻结或跨任务资格终态；
5. 网络/timeout/unknown 不减候选池，但本轮 cursor 继续尝试尚未探测的其他账号；全部候选仅为 unknown 时写 `source_unresolved/probe_transport_unknown`，按 listener policy 复探同一固化顺序。确定性候选耗尽写 `source_unresolved/probe_accounts_exhausted`，两者都不写 `remote_target_deleted`；
6. 任一合格查看账号远端读取到原消息，立即修复本地索引并重新物化原义务；只有 Telegram 对已确认可见该 peer、且该账号的 `history_visible_from <= source_created_at` 的查看账号返回权威“消息不存在”时，才形成候选负面证据。最终写 `remote_target_deleted` 前，以 `expected_source_revision + expected_source_state_hash + expected_listener_cursor + probe_run_version` 对 probe run 执行单行 CAS；不显式锁 probe run 或来源义务。CAS 同时要求不存在更新 revision、更晚正向 probe/listener fact，且 listener cursor 未以正向事实越过本次快照。任一条件变化写 `stale_negative_probe_rejected` 并回读新事实，不得覆盖刚完成的 resync。账号入群时间晚于来源时间、history gap 或不可证明历史覆盖时仍为 `source_unresolved`。

Listener policy 必须配置 `heartbeat_stale_after/cursor_stale_after/success_poll_stale_after` 并版本化。三个阈值任一超期进入 `listener_stalled`；只有全部新鲜且窗口确实无来源，才能显示 `waiting_for_source`。

## 11. 资源空闲即执行与 E4 验收

运行合同只计算当前事实，不计算或持久化预扣、目标速率、静默权重、搜索 Window、任务份额或预计完成时间：

```text
remaining_target = max(0, planned_daily_target - confirmed_count - gateway_started_count - unknown_hold_count)
generation_free = max(0, healthy_generation_slots - generating_count)
interaction_free = max(0, healthy_interaction_slots - executing_interaction_count)
search_free = max(0, healthy_search_slots - executing_search_count)
ocr_free = max(0, healthy_ocr_slots - running_ocr_count)
```

- 所有开放义务立即可执行。Planner、Generation、search solver、OCR worker 和 Dispatcher 都由“新义务/事实变化/终态/资源空闲”唤醒，并以数据库轮询兜底；每一阶段只按自己的真实空闲槽 JIT 补满，不等待时间窗。
- 每轮先从每个 running Task 领取至多一个 ready 义务，再按 `opened_at, task_id, obligation_id` 填满该阶段剩余槽位；这是无持久份额、无预扣的简单轮转，不存在任务抢账号，也不创建或恢复 `DispatchReservation/TaskAllocation`。
- Provider token、worker lease 和 Telegram/OCR 实际执行槽都是当前阶段的真实运行事实，不是为未来工作预留的 Task 配额。上游不得以“下游可能繁忙”为由预扣；下游槽空闲时才 JIT 物化下一批。
- `4000+5000+800+800=10600` 是四个独立 Task 的总欠额；四个 Task 同时推进。页面只展示当前欠额、open/generating/executing/unknown/confirmed 数和实际并发，不显示 required rate、静默权重、获配份额或 ETA。
- 完整 AI 任务日 E4 必须满足：`confirmed_count >= planned_daily_target`、`scheduler_oversend_count=0`、达标后新 Gateway 为 0、当前必达账号全部 `completed|abandoned_for_day` 且 abandoned 有类型化原因、open/generating/executing/unknown_hold 为 0、重复远端为 0。
- 搜索 E4 必须满足单目标 `target_click_observed_count == daily_click_target`，且每条确认均能沿 `Task -> obligation -> assignment -> Action -> ExecutionAttempt -> target_click_observed` 回溯；评论、点赞、浏览使用各自 typed remote fact 等式。
- 已删除旧 Task 必须满足主记录和所有可重建 runtime/config 均为 0；只允许最小 delete operation 与远端 mutation/unknown 防重 tombstone 存在，且它们不能重新产生运行行为。
- 不在 canary 或运行前计算“能否完成”、预计速率、预计确认数或容量折损。新 Task 直接按真实结果执行；实际资源不足只在运行中暴露 typed blocker/shortfall，不能改小目标、伪造成功或恢复串行调度。P95/P99 仅用于发现数据库实现退化，不参与业务放量计算。

## 12. 数据模型、旧 Task 删除、激活与发布闸门

### 12.1 必须落库的合同

| 表/模型 | 必填字段/约束 |
| --- | --- |
| `tasks` | `task_lifecycle_epoch BIGINT NOT NULL, fulfillment_contract_version, group_ai_prejoin_channel_ids UUID[] NOT NULL DEFAULT '{}'`；数组去重且最多 3 个；新建 epoch 从 1 开始；删除后物理行不存在 |
| `tg_account_authorizations`（复用） | 增加 `fact_version,last_authoritative_error_code,last_authoritative_observed_at`；同一授权槽权威事实变更递增 version，Task 只引用该版本并独立物化当日状态，不新增全局冻结表 |
| 任务日目标账本 | `planned_target_revision, planned_daily_target, confirmed_count, gateway_started_count, unknown_hold_count, changed_at, change_reason`；计数均由远端事实异步投影，不在 Gateway 前锁账本、预扣或分配 completion ordinal |
| `task_delete_operations` | `id,original_task_id,expected_lifecycle_epoch,state,resume_stage,stage_version,tombstone_set_hash,delete_set_hash,counts,tombstone_checkpoint,delete_checkpoint,last_error,created_by,committed_at`；状态只前进；同一 task/epoch 唯一 |
| `task_delete_operation_items` | `operation_id,item_kind,business_identity,source_version,item_hash,tombstone_state,delete_state`；operation/kind/identity 唯一；恢复只消费冻结 item，不重新扫描扩大集合 |
| `remote_mutation_tombstones` | `tenant_id,mutation_kind,remote_mutation_key_hash,gateway_request_hash,remote_started,terminal_state,remote_fact_identity_hash,reconcile_state,observed_at`；只保留 unknown/已开始/已确认远端副作用的最小防重身份，不保存 Task 配置、内容正文或完整运行投影 |
| `task_account_daily_scope_heads` | 唯一 `(task_id,target_group_id,account_id,task_day_ledger_id)`，保存 current fact row/version；切换用 head CAS，保证一个业务键只有一个 current head |
| `fulfillment_obligation_projections` | `(obligation_type,obligation_id)` 唯一；保存 `tenant_id,task_id,task_day_ledger_id,work_lane,opened_at,deadline_at,materialization_version,state,active_action_id,version` |
| ContentMix 义务投影 | `(obligation_type,obligation_id,materialization_version)` 唯一 |
| `actions` | 对 `(obligation_type,obligation_id)` 建非终态 partial unique index；一个义务最多一个非终态 Action |
| `generation_jobs` | `generation_sequence,context_snapshot_version,generation_owner_id,generation_lease_epoch,lease_expires_at,policy_version,job_version`；同义务最多一个非终态 job；sequence 仅审计 |
| `recoverable_work_leases` | `work_type/id,owner_id,owner_fencing_epoch,lease_expires_at,policy_version,work_version`；`work_type/id` 唯一 |
| `search_click_assignments` | `obligation_id,solver_input_hash,assignment_version,execution_policy_version,obligation_deadline_at,binding_version,state/version,owner_id,owner_fencing_epoch,lease_expires_at`；同义务最多一个 open/Gateway-unknown assignment；assignment 行即持久待执行工作，Task 只保存一个 target |
| `search_protocol_sessions` | `assignment_id,phase,phase_version,request_identity,viewer_cursor,page_fingerprint,keyword_id,approved_target_ref,next_page_identity,challenge_fingerprint,protocol_sample_version,owner_id,owner_fencing_epoch,lease_expires_at`；assignment 唯一；phase CAS 只前进或进入显式 failed/unknown |
| `task_group_bot_admissions` | 唯一 `(task_id,account_id,target_group_id)`；保存 admission/requirement/observation version、所引用远端 fact id 和 ready CAS 状态 |
| `account_group_admission_facts` | 无 `task_id`；`fact_kind` 仅为 `configured_channel_follow|dynamic_channel_follow|requirement_confirmation|post_follow_visibility`；按账号、目标 peer/频道、remote mutation/observation identity、fact version 唯一 |
| `ai_group_message_memory`（复用） | 对 `(task_id,group_id,account_id,task_day_ledger_id)` 建 `WHERE content_source='check_in' AND status IN ('open','gateway_started','unknown','confirmed')` partial unique；同账号同群同 Task 日最多真实发送一次签到，不新增独立签到事实表 |
| `remote_source_probe_runs/items` | run 保存 source revision/state hash、listener cursor、candidate snapshot、随机顺序和版本；item 保存 viewer、结果及本次池扣除事实 |
| `remote_reconcile_cases/evidence` | request identity、mutation kind、expected hashes、evidence policy/version、positive/negative/unknown 结论；同 request identity 最多一个 open case |
| `ai_provider_key_versions` | `provider_id,secret_ref,version,active,quota_policy_revision,active_from,retired_at`；partial unique index 保证全系统 `active=true` 最多一行；不得保存明文 key |
| `fulfillment_policy_versions` | `policy_kind,scope_key,revision,payload_hash,effective_at,created_by,reason,approval_ref`；历史只读 |
| `fulfillment_remote_facts` | `fact_id,tenant_id,task_type,task_id,task_day_ledger_id,obligation_type,obligation_id,action_id,attempt_id,mutation_kind,remote_mutation_key_hash,gateway_request_hash,fact_kind,fact_identity_hash,outcome,observed_at`；`remote_mutation_key_hash + gateway_request_hash + fact_kind` 唯一，是成功/unknown/明确未执行的唯一业务真相 |
| `fulfillment_fact_projection_states` | `fact_id,projection_kind,expected_target_version,state,last_error,next_retry_at,projected_at,updated_at`；`fact_id + projection_kind` 唯一，projector 逐投影单行 CAS，可崩溃重放 |
| `task_contract_activation_manifests` | `id,tenant_id,release_train,old_task_ids,new_task_ids,old_set_hash,new_config_set_hash,route_epoch,state,approval_ref,activated_at`；一个 train 只有一个 active route epoch，Gateway 只接受 active manifest 中的新 Task ID |

可重建 runtime/config 对 Task 使用 `ON DELETE CASCADE` 或由 delete operation 显式删除。delete operation、Gateway journal、RemoteReconcileCase 和 `remote_mutation_tombstones` 不反向外键到 Task 主表，仅保存最小 `original_task_id`/hash 用于防重和对账；除此之外不为兼容旧 Task 保留 archive、shadow 表或完整配置副本。

### 12.2 热查询索引与有界 claim

当前合同必须建立以下 PostgreSQL 索引；名称可按迁移规范调整，但列顺序、partial predicate 和用途不得弱化：

```sql
ix_fop_claim_ready
  ON fulfillment_obligation_projections
  (tenant_id, work_lane, opened_at, task_id, obligation_id)
  WHERE state = 'open';

uq_actions_open_obligation
  UNIQUE ON actions (obligation_type, obligation_id)
  WHERE status IN ('pending','claiming','executing','unknown_after_send');

ix_actions_lane_claim_ready
  ON actions (tenant_id, execution_lane, scheduled_at, task_id, id)
  WHERE status = 'pending';

ix_generation_jobs_claim_ready
  ON generation_jobs (created_at, id)
  WHERE state = 'pending';

ix_group_context_messages_ai_recent
  ON group_context_messages
  (tenant_id, group_id, coalesce(sent_at, created_at) DESC, id DESC)
  WHERE is_bot IS false AND content <> '';

ix_search_assignments_claim_ready
  ON search_click_assignments (obligation_deadline_at, id)
  WHERE state = 'open';

ix_admissions_observation_due
  ON task_group_bot_admissions
  (no_prompt_pass_at, task_id, account_id)
  WHERE state = 'observing' AND observation_gap = false;

ix_recoverable_leases_due
  ON recoverable_work_leases
  (lease_expires_at, work_type, work_id)
  WHERE owner_id IS NOT NULL;

ix_remote_reconcile_due
  ON remote_reconcile_cases (next_probe_at, id)
  WHERE state = 'open';

ix_fact_projection_pending
  ON fulfillment_fact_projection_states (next_retry_at, fact_id, projection_kind)
  WHERE state IN ('pending','failed');
```

claim batch 不计算业务速率、未来容量或预扣，只读取当前实际空闲槽：`claim_limit = min(stage_free_slots, stage_claim_batch_limit)`。候选查询只查当前/未截止 task-day 和 partial index 覆盖的非终态窄字段，使用 keyset `(opened_at,task_id,id)`，禁止 OFFSET、全历史 Action 扫描和 JSON 排序。worker 取得候选 ID 后逐行执行 version CAS；冲突项立即跳过并继续下一页，直到填满当前真实空闲槽或候选耗尽，不持有候选查询事务执行网络调用。

Release Gate 的数据库标准固定为：以发布时生产高水位至少 2 倍的数据做 PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)`；上述 claim 查询必须命中目标 partial index且不得对热表 Seq Scan，`claim_query_p95 <= 100ms`、单行 claim CAS `p99 <= 250ms`、projector 单事实收敛 `p99 <= 500ms`，并发测试 deadlock 为 0。阈值仅判实现是否退化，不改变业务执行数量或节奏。

### 12.3 先新建、切换，再删除旧 Task

1. 运营先按当前确认配置直接创建全新的 Task ID，状态为 `prepared`；这不是旧状态迁移，不复制旧账本、账号范围、Action、ContentMix、准入或完成量。创建请求自身保存 `new_config_hash`，同日新 Task 明确确认 `same_day_recreate_resets_progress=true`。
2. activation preview 绑定精确 `old_task_ids + expected old lifecycle/version + prepared new_task_ids + new_config_set_hash + route_epoch`。只校验新 Task 配置完整、目标/账号引用有效和 writer schema 匹配，不计算容量、速率、预计完成数或迁移差异。
3. 激活只 CAS 一条 `task_contract_activation_manifest`：route epoch 成功推进后，Gateway 立即拒绝旧 Task ID并只接受 manifest 中的新 Task ID；随后新 Task 从 0 直接运行。旧、新 Task 不在同一 mutation identity 上双写，也不需要跨 Task 加锁。
4. route 切换后为旧 Task 分别启动删除 operation：终结未进 Gateway 的 Action/lease，仅为 Gateway-started、unknown 或 confirmed mutation 写最小 tombstone，再物理删除 Task 主记录及全部 runtime/config。删除失败不影响新 Task 继续运行，但旧 Task 始终被 route epoch fence。
5. 删除 preview/apply 仍要求 `ops.manage + approval_ref + expected_manifest_hash`；manifest 是不可变删除集，漂移项失败并重新审批，operation 只从原 checkpoint 恢复，不扩张集合。
6. 新 Task 的 confirmed/gateway_started/unknown 全部从 0 开始，旧成功不抵扣新目标。旧 tombstone 只阻止旧 remote mutation identity 重放，不参与新 Task 目标、账号资格、C2 ready 或搜索 assignment 计算。

### 12.4 新合同激活与回滚

激活顺序固定为：部署 inactive-by-default 新 writer → 运营直接创建 `prepared` 新 Task 集合 → 用其中一个真实新 Task ID 做 allowlist canary，按正常义务直接执行并取得至少一条完整 `Task -> obligation -> Action/assignment -> Attempt -> typed remote fact` 链 → 审批 activation manifest → CAS 唯一 route epoch，使全部 prepared 新 Task 运行且全部旧 Task 同时失去 Gateway 权限 → 异步写旧 mutation tombstone并物理删除旧 Task/runtime → 验证旧 Task/runtime 为 0。新 Task 在 route 切换前已经存在，切换后无需等待运营再次重建。

- canary 不是吞吐预测、容量计算、迁移、shadow 或双写。它直接执行真实新 Task，只证明新 writer 能产生完整远端事实链；不计算 P95/P99、预计日完成量、required rate 或折损系数。canary 未产生完整 typed remote fact 链时不得切 route 或删除旧 Task。
- 删除 manifest 的逐项谓词必须同时绑定 `tenant_id + task_type/release_train + fulfillment_contract_version < new_version + explicit_task_ids + expected_lifecycle_epoch/version/item_hash`；漂移项拒绝，禁止扫描扩大到未确认 Task。
- 不存在按 Task 状态迁移、兼容 reader、shadow writer 或新旧双写；worker 领取和 Gateway 前都校验唯一 `fulfillment_contract_version + schema_revision`。
- route 尚未切换时可停止新 writer并删除 prepared 新 Task；route 一旦切换就永久禁止恢复旧 Task/旧 writer，不以新 Task 是否已进入 Gateway 为条件。激活后的故障只允许停止受影响新 Task、前向修复同一新合同或发布修复版本；旧 Task 删除 operation 继续按原 checkpoint 收口。
- 数据库结构保留到后续已验收 release，不在紧急回滚中删表/列；回滚代码也不得重新启用旧 Window、Reservation、旧 Task route 或兼容 writer。

### 12.5 QA 与生产闸门

QA 必须至少覆盖：

- `800 -> 797`、账号恢复回流、Telegram 权威 `session_invalid|need_relogin|cannot_send` 后立即 `abandoned_for_day`；目标解散终结 Task/目标而不全局判废账号；账号状态仅作用于当前 Task 日，不形成全局冻结或残留分母；
- 同一账号的四个 Task RPC 真重叠，response/timeout/cancel 精确回写各自 Action；FloodWait 作用于账号后续 RPC、SlowMode 只作用于 peer；
- 目标上调/下调与多个 Gateway 并发时，不锁任务日账本；稳定主义务数量、单义务 Gateway CAS和唯一远端事实保证不重复发送，目标下调只取消未进 Gateway 的多余义务，旧 revision 已开始事实仅计 `target_reduction_overage`；
- generation/interaction/search/OCR 四类真实槽位分别计算并在释放后补满；不存在 rate、quiet、Window、份额、预扣、`DispatchReservation/TaskAllocation` 或 completion ordinal；
- pause/delete 与 Gateway 并发，旧 epoch 新远端 mutation 为 0；删除阶段逐点崩溃恢复，tombstone 不含配置/正文，Task/runtime 删除后不能复活；
- prepared 新 Task 先创建，route epoch 切换后立即从 0 执行，旧 Task 同时失去 Gateway 权限并异步物理删除；旧成功不抵扣且旧 remote mutation identity 不重放；
- C2 Task 字段 0/1/3/4 个配置频道校验；配置频道并发关注；Task 无关四类远端事实被多个 Task 安全引用且各 Task 独立计算 ready；连续 30 秒必须绑定同一 target-group control surface、viewer authorization、listener epoch 和连续 cursor，29 秒不通过，surface/cursor/listener 变化重开观察；展示名+要求链接绑定、重名拒绝、多可信 action、callback unknown 和 ready CAS；
- C3 三条数据库唯一约束在并发物化、worker 崩溃和恢复时不产生双 projection、双 ContentMix 或双非终态 Action；
- 单目标搜索 Task 拒绝零目标、多目标和目标数组；多个独立 Task 可并行；assignment 落库即成为数据库持久工作，通知丢失/worker 重启可从 lease 与 phase 接管；`hot_list_page -> group_category -> result_page` 均落库，无 Window/二次容量确认，binding 变化取消，Gateway unknown 不换路重发；
- OCR A/B 相同答案不重复；A 无结果才 B，B 错误后仅按已审批 refresh 取得新 fingerprint，未知按钮、`/cancel`、`/start` 和重发关键词调用数为 0；
- 多个 direct GenerationJob 真并发且后位不等待前位；reply/强上下文仍受 revision CAS；全系统 0/1/2 个 active Provider key 分别阻断/通过/约束失败，多模型共享唯一 active key，轮换时旧 in-flight usage 不丢失；
- `签到` 正文精确、完成数量与账号 coverage、不参与普通正文 10 天去重、不计高质量正文、不触发 reply；同 `(task,group,account,task-day)` 最多一次 open/Gateway/unknown/confirmed 签到，全部账号已用且正常内容不足时暴露 `content_capacity_gap`；Gateway unknown 不自动重发；
- local reply 缺失但远端存在、权限失败候选池减 1并保存 typed fact、网络 unknown 不减 1且继续其他候选、候选耗尽、负面 probe 与 listener 正向 resync 竞争；
- 各类 mutation unknown 分别注入 positive、完整 pre-accept negative 和 evidence gap；只有权威 `safely_not_executed` 能在 deadline 前释放原义务。到 deadline 仍 unknown 时转 `remote_reconcile_only + closed_with_unknown_shortfall`、释放槽位、保留 tombstone且永不自动重放；
- canary 只验证真实远端事实链，不做吞吐或容量预测；route 切换后四个 AI Task 按真实目标同时执行并分别取得生产 E4，单目标搜索逐条具备 `target_click_observed`；已删除旧 Task/runtime 为 0且仅剩最小 tombstone；
- 所有 claim 热查询命中规定 partial index，在生产高水位 2 倍数据和并发 worker 下满足延迟阈值、Seq Scan=0、deadlock=0；最终事实并发插入、projector 崩溃重放和 CAS 冲突均不重复远端 mutation。

只有定向 QA、完整回归、旧 Task 删除 preview/apply、`master -> release -> Deploy Production`、生产 runtime SHA 和各任务真实 E4 依次通过，才能标记 `production_fixed`。
