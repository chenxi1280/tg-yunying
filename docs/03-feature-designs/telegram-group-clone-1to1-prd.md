# Telegram 1 对 1 群组实时镜像克隆任务专项 PRD

> **任务类型**：`group_clone`
> **履约合同**：`v2_group_clone`
> **文档状态**：`product_design_complete`
> **设计状态**：`complete`
> **实施状态**：`partial_local_validation`
> **发布状态**：`not_started`
> **版本日期**：2026-08-30
> **兼容边界**：新增独立任务类型；存量 `group_relay/legacy_v1` 不被动态重解释
> **索引同步**：`resync_required=false`；主 PRD、数据流索引和结构索引已同步合同入口
> **下一流转**：`next_route=dev_gap_closure`；完成共享 collector、全生命周期 adapter、割接与集成 QA 后进入发布门禁

## 0. 文档目的与结论

本 PRD 将“监听转发”中的新需求定义为一个独立的 **Telegram 群组 1 对 1 镜像克隆任务**：一个任务冻结一个源群和一个目标群；源群发言人稳定映射到我方受控账号，由受控账号在目标群重新发送经过允许的内容，并同步可支持的回复、编辑、删除、置顶、话题和媒体生命周期。

“1 对 1”有两层含义：

1. **群关系 1 对 1**：一个任务只允许一个源群和一个目标群，目标在任务生命周期内不可自动切换。
2. **发言人映射稳定**：同一源发言人在绑定有效期内只映射一个受控发送账号；这不是对源 Telegram 用户身份的伪造或复制。

本任务不是 `group_relay` 的配置升级。它拥有独立的领域事实和 Planner，但发送、Attempt、Gateway 证据、unknown 对账和成功事实必须复用平台统一履约主链。

## 1. Intake Card

- `source`：用户产品需求。
- `raw_input`：把监听转发改成克隆 Telegram 群的方式；一个源群对应一个克隆群，源群哪个账号发言，就由稳定映射的受控账号在克隆群复制内容发送，并重新设计任务底层逻辑。
- `classification`：L2，新增任务类型 + 数据流重构；影响任务创建、listener、账号池、Telegram Gateway、数据库、详情页和迁移。
- `production_related`：是；但本 PRD 不授权生产变更。
- `current_stage`：dev_gap_closure。
- `next_gate`：实现差距闭合并通过 PostgreSQL/Telegram 集成 QA。
- `out_of_scope_for_this_document`：实现、数据库迁移执行、发布、生产数据迁移和真实群写入。

## 2. 产品目标、非目标与合规边界

### 2.1 产品目标

- 对源群创建后产生的新事件进行持久、可恢复、可审计的增量镜像。
- 在 Telegram 能力允许的范围内保留文本、Caption、媒体、Entities、回复关系、相册、Topic、Poll 题面及消息生命周期。
- 同一源发言人使用稳定受控账号代发，不在运行中修改账号昵称、头像或用户名。
- 任何重启、重复 update、乱序 update、RPC timeout、FloodWait 和割接都不得导致静默双发、漏发、串群或伪成功。
- 任务详情能够从 source event 下钻到 obligation、Action、Attempt、Gateway 证据和 typed remote fact。

### 2.2 非目标

- 不伪造任意第三方 Telegram User ID、`from_id` 或未经授权的 `send_as`。
- 不复制源用户真实账号资料，不动态把马甲资料改成源用户昵称或头像。
- 不绕过 Restrict Saving Content、权限限制、封禁、FloodWait、SlowMode 或 Telegram 服务端限制。
- 不复制 Poll 的既有票数、投票人列表或后续投票结果。
- 不保证 Telegram 不支持复制的 service message、支付、赠品、小游戏等对象能够镜像。
- 不把 `group_relay` 存量任务原地切换成新合同。
- V1 不提供自动紧急换号；账号不可发送时等待、阻断或人工换绑，禁止静默破坏人设。

### 2.3 授权与内容保护

- 创建者必须声明源群为公开可访问、我方拥有，或已取得管理员/内容方授权；保存 `source_authorization_mode`、确认人、确认时间和审计理由。
- 源群启用内容保护或消息被 Telegram 标记不可转发/保存时，source event 仍可保存最小审计元数据，但不得下载媒体、复制正文或进入 Gateway；义务终态为 `blocked(source_content_protected)`。
- 原始 Telegram Updates 不整体写入业务库。业务库只保存任务所需的规范化字段、hash 和脱敏 typed evidence。
- 临时媒体缓存必须加密、按 tenant 隔离、带 TTL；成功、终态失败或任务归档后按数据保留策略清理。

## 3. 不可违反的正确性不变量

### 3.1 单一履约真相源

```text
Task(group_clone, current epoch + frozen config)
  -> TelegramAuthorizationUpdateState + UpdateSubscription + UpdateDelivery
  -> CloneSourceStreamState
  -> CloneSourceEvent
  -> TelegramGroupMutationAuthority + AuthorityHolder
  -> CloneDeliveryObligation
  -> FulfillmentObligationProjection
  -> Action(obligation_type=group_clone_delivery)
  -> TelegramGatewayMutationIdentity
  -> ExecutionAttempt
  -> Telegram Gateway + GatewayRequestEvidenceJournal
  -> OutboundRandomIdMapping / RemoteReconcileCase
  -> FulfillmentRemoteFact(clone_*_observed)
  -> CloneMessagePart / TopicMap / UI projections
```

约束：

- Clone 表负责领域事实，不另造第二套 executor、retry 或 remote success 真相源。
- 每条可执行义务只能有一条 open FOP 和一条 open Action；复用平台现有 `(obligation_type, obligation_id)` 唯一约束。
- `Action.status=success`、RPC 返回、`remote_message_id` 或本地 mapping 任一单独存在都不等于履约完成。
- `succeeded` 必须由成功 Attempt 关联的 `FulfillmentRemoteFact` 确认，并绑定回同一 obligation。
- `unknown_after_send` 不得释放幂等身份、换账号或创建新 mutation。
- source listener 与 sender 使用同一 authorization 时也只能由平台共享 update ingress 消费 common updates；任务 worker 不得各自创建第二个无状态 collector。

### 3.2 群消息绝不串台

每个 obligation 创建时先冻结不可变 `RouteBindingSnapshot`，至少包含：

- `tenant_id`
- `task_id`
- `task_lifecycle_epoch`
- `config_revision`
- `source_internal_group_id + source_operation_target_id + source_peer_type + source_peer_id`
- `target_internal_group_id + target_operation_target_id + target_peer_type + target_peer_id`
- `reply_target_message_id + target_top_msg_id`
- `route_binding_version + route_binding_hash`

sender binding 完成后、Action 物化前，再冻结不可变 `ExecutionTargetBindingSnapshot`，至少包含：

- `route_binding_snapshot_id`
- `account_id + authorization_id + account_target_relation_version`
- `sender_binding_history_id + sender_binding_version`
- `execution_binding_version + execution_binding_hash`

处于 `waiting_binding` 的 obligation 允许只有 Route snapshot，不得伪造 account；没有 Execution snapshot 时不能物化 Action。

Planner 物化 Action 时校验一次 Execution snapshot；Gateway 前必须根据当前 canonical peer 和账号群关系再次校验 Route + Execution 两层冻结快照。任一字段不一致：

- Action 进入 `failed`，错误码 `target_binding_mismatch` 或 `target_binding_unproven`。
- Telegram Gateway 调用次数必须为 0。
- 禁止猜测目标、按标题找群、切换到其他群、去掉 reply 后继续发送或跨群重试。
- 账号人工转派只允许改变执行账号，不允许改变目标群；必须生成新 binding version。

### 3.3 任务与平台级目标写入所有权

- 同一任务固定一个 source peer 和一个 target peer。
- `source_peer == target_peer` 时创建失败，错误码 `clone_source_equals_target`。
- 创建或启动时检测当前 active clone edge，禁止形成 A→B→A 或更长的有向环，错误码 `clone_route_cycle`。
- `TelegramGroupMutationAuthority` 是所有平台自动群写任务共享的权威门禁，不是 Clone 私有锁。`group_clone/group_relay/group_ai_chat/MessageTask/Campaign` 及以后新增的 writer 在任何 Telegram group mutation 前都必须校验它。
- authority 在 `shared` 模式可登记多个存量 writer holder；`group_clone` 必须申请 `exclusive_clone`。只有目标不存在其他 active holder、未收口 executing/unknown Action 或不可证明历史绑定时才能原子切到独占模式，否则 Precheck 失败。
- Cutover 期间只允许一个带 generation 的旧/新 handoff pair；authority 内的 `gateway_admission_side` 决定当前唯一可进入 Gateway 的一方，不能只靠 Task 状态或 worker 内存判断。
- holder、route、generation、authority version 任一不一致时 fail closed，错误码 `target_mutation_authority_conflict|target_mutation_authority_stale`，Gateway 调用必须为 0。
- 外部真人或平台外 Bot 的 Telegram 发言不受本平台 authority 控制；UI 不得把“Clone obligation 严格顺序”描述成“整个目标群绝无外部插入”。受控克隆群的运营规范应限制其他外部 writer，但其存在不改变本平台 fail-closed 责任。

### 3.4 版本与重解释边界

- `task_lifecycle_epoch` 只在显式 reset、recontract 或 cutover 新任务时初始化/递增；worker 重启、容器重启、pause/resume 不递增。
- `config_revision` 每次有效配置修改递增；已观测 source event 和 obligation 保留冻结 revision，不被新配置动态重解释。
- `sanitization_revision`、`sender_binding_version`、`route_binding_version`、`execution_binding_version` 和 `media_policy_version` 均随 obligation 冻结。
- 修改 source 或 target 不属于普通 PATCH；必须创建新任务或走显式 cutover。

## 4. 任务配置与创建合同

### 4.1 `Task.type_config` Schema

```json
{
  "source": {
    "internal_group_id": 101,
    "operation_target_id": "uuid",
    "peer_type": "channel",
    "peer_id": "-1001234567890",
    "listener_account_id": 12,
    "authorization_id": 31,
    "authorization_mode": "public|owned|admin_authorized"
  },
  "target": {
    "internal_group_id": 202,
    "operation_target_id": "uuid",
    "peer_type": "channel",
    "peer_id": "-1009988776655",
    "control_account_id": 40,
    "control_authorization_id": 30
  },
  "sender_pool": {
    "account_ids": [41, 42, 43],
    "active_minutes": 30,
    "guarded_minutes": 120,
    "eligible_release_minutes": 720,
    "minimum_tenure_minutes": 60
  },
  "pacing": {
    "min_delay_ms": 1000,
    "max_delay_ms": 6000,
    "strict_target_order": true
  },
  "content": {
    "rule_set_id": "uuid",
    "rule_set_version": 3,
    "orphan_reply_policy": "drop_subtree|quote_fallback|block_for_review",
    "incomplete_album_policy": "drop_incomplete|send_partial_degraded",
    "unsupported_media_policy": "block|manual_review"
  },
  "lifecycle": {
    "start_mode": "start_from_now",
    "failure_order_policy": "fail_stop|continue_with_visible_gap",
    "unknown_deadline_seconds": 900
  },
  "retention": {
    "source_event_days": 30,
    "media_cache_ttl_seconds": 86400
  }
}
```

Schema 约束：

- V1 只允许 `start_mode=start_from_now`；不接受模糊的“最近 N 条”或无边界历史回放。
- `strict_target_order` 固定为 `true`，不是可关闭开关。
- sender pool 不能为空，账号必须同 tenant、授权有效、Session 可用、在线可恢复、已加入目标群且具备对应媒体发送权限。
- 每个 listener/sender/target control authorization 必须接入平台共享持久 update ingress 与 session coordinator；同一 authorization/session generation 只能有一个 active collector owner，任务不能私建第二套 common update cursor。
- listener account 只需能读取源群；它可以与发送账号不同。
- target control authorization 是冻结的管理员执行角色，只用于 Topic、Pin/Unpin 和 V1 管理员 Delete，不得作为普通消息发送、号池不足或 Edit 的兜底号。启用这些能力时它必须同 tenant、Session 有效、已加入目标群并具备对应权限。
- source/target 必须由 canonical OperationTarget 解析，不能只保存前端传入的标题或 username。
- create/create-and-start 必须支持 `client_request_id + request_fingerprint` 幂等；同 key 不同 payload 返回冲突。
- retention 值在平台数据保留策略允许范围内冻结到 config revision；到期清理正文/临时媒体时保留 event identity、事实 hash、远端映射和审计 tombstone。
- 普通 PATCH 只允许修改 listener 执行账号、sender pool、pacing、content policy、failure policy 和 retention；source/target canonical identity 与授权模式不可 PATCH。
- listener account 修改必须走 stream handoff：冻结旧 owner state、验证新 authorization、从旧 state 完成 difference 后再切 owner；不得把配置字段直接替换后清空 cursor。

### 4.2 创建与启动 Precheck

启动前必须一次性返回以下分类事实，不得只返回总数：

- source peer 可解析、可读取、授权模式已确认、内容保护状态。
- target peer 可解析；frozen target control authorization 的 online readiness、target membership、Delete/Pin/Topic 管理权限和 session generation 可证明；SlowMode 状态已读取。
- sender pool 每个账号的 authorization、online readiness、target membership 和消息/媒体发送能力；不得要求或借用其管理员权限替代 target control。
- source/target 不相等、无 route cycle；共享 target mutation authority 无其他 holder、open executing/unknown writer 或不可证明绑定。
- listener/sender/target control authorization update ingress、raw MTProto random-id adapter、outbound mapping reconcile capability 已就绪；不支持时不得启动。
- rule set 已发布且版本冻结。
- listener stream 能取得初始 state/cursor。
- PostgreSQL 必需表、唯一索引和统一 fulfillment/reconcile 依赖已就绪。

Precheck 只是带版本的预览，不授予目标写权。普通 `create` 只创建 stopped Task，不领取 authority；`create-and-start/start` 必须在单事务中复核 Task/config/authority/holder/open-action fingerprint，以 CAS 领取 exclusive Clone holder，写 `clone_start_state=starting` 并创建 initializing subscription；提交后才执行 Telegram boundary 初始化。任一 Precheck/事务硬条件失败时不得留下 holder/subscription 或创建可领取 Action。

Telegram boundary 初始化是跨系统步骤，显式状态机为：

```text
stopped -> starting -> running
starting -> start_cleanup_pending -> start_failed
```

初始化失败且尚无 Gateway mutation 时，fenced cleanup 事务停止 subscription、停用 holder、把空 authority 置 `vacant/shared/no_admission` 并写审计；cleanup CAS 失败则保持 `start_cleanup_pending` 和 authority fail closed，Task 不物化 Action，也不伪装成 stopped/running。相同 client request id 只返回这条既有启动状态，不能重建第二个 holder/subscription。

## 5. 持久化数据合同

以下是逻辑 DDL 合同。迁移可按项目命名风格拆表，但字段、外键、唯一性和状态语义不得弱化。

### 5.0 平台共享 Authorization Update Ingress

`telegram_authorization_update_states` 是账号授权级 common updates 权威状态，不属于单个 Clone 任务。字段至少包括：

- `tenant_id/account_id/authorization_id/session_generation`。
- `common_pts/qts/date/seq/difference_cursor`。
- `state=initializing|catching_up|live|gap|blocked|stopped`。
- `owner_id/owner_fencing_epoch/lease_expires_at/version`。
- `last_ingress_order_no/last_update_identity_hash/last_applied_at`。

唯一约束为 `(tenant_id, authorization_id, session_generation)`，同一行只能有一个 active collector owner。collector 必须先持久化最小规范化 `telegram_authorization_update_events`，再以 CAS 推进 state；事件包含 `ingress_order_no/update_identity_hash/constructor/pts evidence/routing peer/payload fingerprint`，不保存完整 raw Updates。`ingress_order_no` 由该 state owner 单调分配且在 authorization state 内唯一。

`telegram_authorization_update_subscriptions` 保存 `authorization_update_state_id/task_id/task_epoch/source_peer/start_ingress_order/state/version`，对 `(task_id, task_epoch)` 唯一。`telegram_authorization_update_deliveries` 保存匹配订阅后的 `update_event_id/subscription_id/task_id/normalized_item_index/normalized_payload/payload_fingerprint/delivery_state`；normalized payload 只包含该 Clone source event 所需字段，按 tenant 加密和任务 retention 清理，不保存账号收到的其他聊天内容。对 `(update_event_id, subscription_id, normalized_item_index)` 唯一。

collector 在推进 common cursor 前，必须在同一 PostgreSQL 事务中写入 update event、所有当时 `initializing|active` 且 peer 匹配的 task delivery，以及可提取的 outbound random-id mapping。任务 fan-out 尚未完成时 delivery 保持 `pending` 并可由新 owner 重放；不能出现“common state 已推进但任务 payload 只在内存中”的 crash window。非订阅 peer 只保留推进 state 所需的最小 identity/hash，不保存正文或媒体 payload。

`telegram_outbound_random_id_mappings` 保存 `authorization_update_state_id/gateway_mutation_identity_id/random_id/gateway_request_journal_id/action_id/execution_attempt_id/target_peer/remote_message_or_topic_id/update_identity_hash/observed_at`。唯一约束至少包括 `(authorization_update_state_id, random_id)` 和非空 `(gateway_request_journal_id)`；只有从同一 authorization/session generation 的 `updateMessageID` 或同一 raw RPC 返回中取得的 exact mapping 才能填入。

共享 ingress 同时服务 source task subscription delivery 和 outbound reconcile：任务 source stream 只消费自己的 durable delivery；outbound mapping 按 random_id 关联 Attempt。任何任务不得独立推进、重置或覆盖 authorization common state。collector owner 变化沿原 cursor 接管；session generation 变化创建新 state，旧 generation 的 unknown 只能由旧 generation 的权威证据收口，不能用新 session 的 history miss 判定未发送。

### 5.1 `clone_source_stream_states`

| 字段 | 合同 |
|---|---|
| `id` | UUID/String(36) 主键 |
| `tenant_id` | FK tenant |
| `task_id` | `VARCHAR(36)` FK `tasks.id` |
| `task_lifecycle_epoch` | 当前显式 epoch |
| `source_peer_type/source_peer_id` | 冻结源 peer 复合身份 |
| `listener_account_id/authorization_id` | update state 所属账号与授权槽位 |
| `start_message_id/start_pts` | `start_from_now` 边界 |
| `authorization_update_state_id/last_consumed_ingress_order_no` | 引用共享 ingress 及本任务消费水位，不拥有 common cursor |
| `channel_pts` | source channel/supergroup state |
| `difference_cursor` | difference slice 中间状态；结构化 JSON |
| `state` | `initializing|catching_up|live|gap|blocked|stopped` |
| `owner_id/owner_fencing_epoch/lease_expires_at` | listener claim 与接管 |
| `last_applied_event_hash/last_applied_stream_order_no/last_applied_at` | 幂等推进与任务内连续顺序证据 |
| `version` | CAS version |

唯一约束：`(tenant_id, task_id, task_lifecycle_epoch)`。

### 5.2 `clone_source_events`

| 字段 | 合同 |
|---|---|
| `id` | UUID/String(36) 主键 |
| `tenant_id/task_id/task_lifecycle_epoch` | 同 Task 当前冻结范围 |
| `source_peer_type/source_peer_id` | 源群复合身份，不用 32 位 INT |
| `source_message_id` | 被作用的消息 ID；delete/pin 也使用 subject ID |
| `event_type` | `message_new|message_edit|message_delete|message_pin|topic_create|topic_edit|topic_delete` |
| `ingress_update_identity_hash` | 产生此事件的共享 envelope identity；仅用于追踪 delivery，可随 live/difference envelope 不同 |
| `event_identity_hash` | 与 transport 无关的语义 identity：canonical peer、event type、subject message/topic id、Telegram revision marker 与规范化 payload/state fingerprint |
| `source_pts/source_pts_count` | update 顺序证据；允许空值的 service update 需保存对应 seq evidence |
| `authorization_ingress_order_no/normalized_item_index` | 共享 ingress 顺序与同 update 内稳定 item index |
| `apply_order_key/stream_order_no` | 可重放确定性排序键与该 task/epoch 的连续业务顺序；不得用插入时间替代 |
| `message_revision` | 同消息内容变更 CAS 版本；重复 fingerprint 不递增 |
| `sender_peer_type/sender_peer_id` | new/edit 可有；delete/pin 可空 |
| `reply_to_message_id/source_top_message_id` | 原始依赖与 Topic |
| `grouped_id/media_type/content/entities/poll_snapshot` | 规范化快照；不保存整包 Updates |
| `content_fingerprint` | 内容幂等与 revision 判定 |
| `protected_content` | 保护标记 |
| `config_revision/sanitization_revision` | 观测时冻结版本 |
| `observed_at` | 首次持久化时间 |

唯一约束：`(tenant_id, task_id, task_lifecycle_epoch, source_peer_type, source_peer_id, event_identity_hash)`。

另对 `(tenant_id, task_id, task_lifecycle_epoch, stream_order_no)` 建唯一约束。`apply_order_key` 由 source peer、update domain、连续 pts/seq evidence、Telegram vector/item index 和 semantic event identity 构成；同一 update 多 subject 必须使用稳定 `normalized_item_index`。live update 与 difference replay 即使 envelope constructor 不同，也必须归一成同一个 `event_identity_hash/apply_order_key`。stream owner 只有在确认该 source 前序 gap 已闭合时，才能按 key 在锁定 `CloneSourceStreamState.version` 的同一事务中分配下一个 `stream_order_no`；重复 semantic identity 返回既有序号并补充 envelope 关联，pending gap event 不得提前取得可推进序号。

消息级唯一与版本规则：

- new 重复 update 使用相同 fingerprint 时只返回已有 event。
- edit 只有内容 fingerprint 发生变化才以行锁/CAS 分配下一 `message_revision`。
- delete/pin 不假定携带 sender 或原正文，通过 `source_message_id` 引用此前 new/edit 快照。
- 乱序 edit/delete 可以先落事实并进入 `waiting_source_base`，不得伪造缺失字段。

### 5.3 `telegram_group_mutation_authorities`、Clone Owner Projection、Route Snapshot 与 Execution Snapshot

`telegram_group_mutation_authorities` 对 `(tenant_id, target_peer_type, target_peer_id)` 唯一，保存 `mode=shared|exclusive_clone|handoff/cutover_generation/gateway_admission_side/state/version`。`telegram_group_mutation_authority_holders` 保存 `authority_id/writer_kind/writer_id/route_hash/holder_role/state/version`，对 `(authority_id, writer_kind, writer_id, route_hash)` 唯一。

- `shared` 允许多个已登记 active holder，维持存量 writer 的产品语义；它不承诺跨 holder 全局排序。
- `exclusive_clone` 只允许一个 active Clone holder。领取时锁 authority row，确认其他 holder 已退出且其 executing/unknown 已收口或被显式纳入 Cutover exclusion。
- `handoff` 只允许一对冻结的 `old/new` holder，`gateway_admission_side` 任一时刻只指向一侧。
- `clone_target_owners` 只保存 Clone 领域投影、`authority_id/holder_id` 和 sequencer 导航，不能单独授权 Gateway。

所有平台 writer 的 Gateway admission 必须锁定或 CAS 校验 authority 与 holder version，并把 `authority_id/authority_version/holder_id/holder_version/holder_route_hash` 写入 Gateway journal。没有 authority/active holder、route 不一致、exclusive 模式 holder 不唯一、generation/side 不匹配时拒绝 mutation。Cutover handoff pair 也不能同时获得 admission。

上线迁移必须先按现有 Task/MessageTask/Campaign 的 canonical route backfill `shared` authority 和 holder：多个可证明 writer 可登记在同一 shared authority；无法证明 canonical target 的 writer 进入迁移阻断清单，不能猜测 holder，也不能提供“无 authority 继续发送”的兼容旁路。

`clone_target_route_snapshots` 保存第 3.2 节 Route 字段及 `route_binding_hash`，对 `(task_id, epoch, route_binding_version)` 唯一。`clone_target_execution_snapshots` 引用 Route snapshot，保存 `execution_role=sender|target_control`、执行账号、authorization/session generation、账号群关系、可空 sender binding 及 `execution_binding_hash`，对 `(route_snapshot_id, execution_binding_version)` 唯一。普通 send/edit 必须是 sender role；Topic/Pin/V1 Delete 必须是 frozen target_control role。

任何 Gateway mutation 必须同时引用不可变 Route 与 Execution snapshot；目标配置变化只能创建新 snapshot 和新 obligation revision，不能原地覆盖历史 snapshot。

### 5.4 `clone_account_slots` 与 `clone_sender_binding_history`

`clone_account_slots` 是可锁的账号池权威行，即使账号当前未绑定任何 sender 也存在；字段至少包含：

- `task_id/account_id/authorization_id/route_binding_version/execution_binding_version`
- `state=available|reserved|active|cooling|disabled`
- `projected_transport_blocked_until`，仅作共享 transport state 的任务内显示投影，不是发送权威
- `owner_id/owner_fencing_epoch/lease_expires_at/version`

唯一约束：`(task_id, account_id)`。

`clone_sender_binding_history` 保存：

- source sender 复合身份、account slot、binding version、valid range。
- `active|guarded|eligible|expired|disabled`、VIP 标识、置换理由、最近发言时间。
- 对当前有效状态建立两个部分唯一约束：一个 sender 只有一个 binding；一个 slot 只有一个 sender。

分配算法必须锁 `clone_account_slots` 候选行，不能尝试锁不存在的 history 行。唯一索引冲突后回滚并重新求解，不吞掉冲突。

“最近发言”“未决引用”“账号是否刚发过消息”只从 source event、obligation、binding history 和成功 typed fact 计算，不读取目标群临时最近 20 条作为唯一依据。

平台共享 `telegram_authorization_transport_states` 对 `(tenant_id, authorization_id, session_generation)` 唯一，保存 `blocked_until/reason/source_attempt_id/observed_at/version`；所有任务在发送前都必须校验。目标 SlowMode 另以 `(authorization_id, target_peer_type, target_peer_id)` 保存 target-scoped block，不能把两者混成 task-local 冷却。

### 5.5 `clone_album_manifests` 与 `clone_album_items`

Manifest 保存 `grouped_id`、首末观测时间、quiet deadline、max deadline、refetch range、集合 fingerprint、state 和 frozen policy。Item 保存 source message、排序依据、media snapshot、acquisition state 和 fingerprint。

状态：

```text
collecting -> verifying_source -> ready
collecting/verifying_source -> incomplete_timeout
incomplete_timeout -> failed_dropped | ready_partial_degraded
ready/ready_partial_degraded -> action_bound -> succeeded | unknown | failed
```

Telegram grouped update 不提供可信 `part_total`。quiet window 到达后，listener account 必须 fresh refetch 源群 grouped_id 邻近消息，冻结最终集合 fingerprint；refetch 失败不得宣称完整。迟到且不属于冻结集合的 part 记 `stale_album_part_ignored`，绝不单独插队。

### 5.6 `clone_topic_maps`

保存 `source_top_message_id -> target_top_message_id`、source/target peer、topic title/icon fingerprint、create Action/Attempt/remote fact、状态和 revision。

对 `(task_id, epoch, source_peer_type, source_peer_id, source_top_message_id)` 唯一；lazy bootstrap 先创建/锁定 map placeholder 并按 expected revision CAS，两个 worker 不能各自创建一个目标 Topic。

- MTProto/Telethon 发送使用 `InputReplyToMessage.reply_to_msg_id/top_msg_id`；内部 payload 可用统一 `target_top_message_id`，不得把 Bot API `message_thread_id` 直接当 MTProto 参数。
- General Topic 使用 Telegram 的特殊语义，不伪造普通 topic mapping。
- create/edit/close/reopen/delete 必须校验 `manage_topics` 权限并生成独立 obligation。
- Topic create unknown 时阻断该 Topic 下后续消息，直至 reconcile；不得重复创建同名 Topic 猜测成功。
- Topic create map placeholder 必须引用预先持久化的 `TelegramGatewayMutationIdentity`；`channels.createForumTopic` 使用 frozen random_id，成功后 mapping 的 remote id 是 target topic top message id。
- 首次收到 boundary 后消息但其 `source_top_message_id` 没有 map 时执行 lazy bootstrap：listener authorization fresh fetch 该 source topic 的 canonical top id、title/icon/closed/hidden 状态并冻结 fingerprint，再幂等创建对应目标 Topic。禁止按标题匹配既有目标 Topic。
- lazy bootstrap 查不到 source Topic、source fingerprint 在创建前变化或缺少 `manage_topics` 时进入 `waiting_manual_review|blocked`；不得把消息改发 General Topic。创建成功/unknown 分别沿正常 Topic typed fact/reconcile 处理。

### 5.7 `clone_delivery_obligations`

字段至少包括：

- UUID id、tenant/task/epoch、source_event_id、source message revision。
- obligation kind：`send|edit|delete|pin|topic_create|topic_edit|topic_delete`。
- binding history id、route binding snapshot id、execution target binding snapshot id、album manifest id、topic map id。
- `config_revision/sanitization_revision/media_policy_version/contract_version`。
- sequencer id、dependency obligation id、planned_at、deadline_at、unknown_deadline_at。
- `stream_order_no`、sequencer-head case id、sequencer resolution revision。
- state、degradation reason、error code、opened/resolved time、version。

唯一约束：

- `(task_id, epoch, source_event_id, obligation_kind, materialization_version)`。
- `(task_id, epoch, sequencer_id)`。
- 对应 FOP 使用 `obligation_type=group_clone_delivery, obligation_id=<clone obligation uuid>`。

状态全集：

```text
observed
waiting_source_base
waiting_binding
waiting_album
waiting_dependency
waiting_transport
waiting_manual_review
ready
action_bound
executing
unknown_after_send
remote_reconcile_only
succeeded
degraded
filtered
blocked
failed_terminal
cancelled
superseded
```

未列入全集的状态禁止在实现或 UI 中临时添加。

### 5.7A `telegram_gateway_mutation_identities`

这是 Gateway 前 mutation identity 权威，不是成功投影。字段至少包含：

- `tenant_id/task_id/epoch/obligation_id/materialization_version/mutation_kind/part_index`。
- `execution_role/account_id/telegram_account_peer_id/authorization_id/session_generation`。
- `target_peer_type/target_peer_id`、可空 `random_id`、`derivation_version/collision_nonce`。
- `request_fingerprint/state=allocated|attempt_bound|unknown|closed/version`。

唯一约束：

- `(task_id, epoch, obligation_id, materialization_version, mutation_kind, part_index)`。
- random_id 非空时，`(tenant_id, telegram_account_peer_id, target_peer_type, target_peer_id, random_id)` 跨任务、方法类型和 session generation 唯一。

Telegram 对同一账号、同一 peer 的 random_id 跨 session 去重，已使用值不按任务生命周期过期。因此 identity tombstone 永久保留最小 scope/hash，不因正文 retention、Task archive、session 重建或账号重新导入而释放 random_id。无法证明 canonical `telegram_account_peer_id` 时不得分配或进入 Gateway。

identity 必须在任何 Attempt `before_call` 之前持久化；Gateway journal、ExecutionAttempt、outbound mapping、CloneMessagePart 和 TopicMap 都引用它。无 random_id 的 Edit/Delete/Pin/Topic edit/delete 也必须拥有一行 request identity，不能临时拼接 request fingerprint。

### 5.8 `clone_message_parts`

这是 remote fact 的导航投影，不是独立成功真相源。每行必须引用：

- obligation、Action、ExecutionAttempt、FulfillmentRemoteFact。
- part index、冻结 part total、source message、原始发送 `account_id/authorization_id/session_generation/execution_binding_hash`、target peer/message/topic。
- `gateway_mutation_identity_id`、投影的 persisted `random_id`、gateway request identity、remote confirmed time。

禁止保存完整 raw Telegram response。唯一约束至少包括：

- `(task_id, epoch, obligation_id, part_index)`。
- random_id 全局唯一性由 `telegram_gateway_mutation_identities` 权威约束；本表不得另行分配或释放。
- 非空 `(target_peer_type, target_peer_id, target_message_id)` 的 remote mapping 唯一性。

### 5.9 `clone_manual_review_decisions` 与 `clone_cutover_exclusions`

人工审核行保存 obligation、review revision、decision、actor、reason、before/after fingerprint、时间和审计 ID。内容审核只允许 `release|filter|block`；重复提交按 expected revision CAS。

Cutover exclusion 保存旧/新任务共享的 source event identity、cutover generation、旧 Action/Attempt/unknown、以及哪一侧拥有 mutation 权。任何一侧在 Gateway 前都必须检查 exclusion；这是 rollback 不双发的权威事实。

### 5.10 `clone_sequencer_head_cases` 与决策

`failed_terminal` 或已到 deadline 的 `remote_reconcile_only` 成为当前 Sequencer head 时创建唯一 case，保存 `case_kind=failed_terminal|unknown_deadline_closed/obligation_id/sequencer_id/failure or unknown evidence/remote_mutation_started/authoritative_absence_evidence_id/policy/state/revision`。对 `(task_id, epoch, sequencer_id, case_kind)` 唯一，状态为 `waiting_decision|visible_gap_accepted|retry_authorized|blocked`。

- `continue_with_visible_gap`：系统基于冻结 policy 生成带审计的 `visible_gap_accepted` 决策；原 obligation 保持 failed 或 permanent unknown，不改写为成功，下一序号才可推进。
- `fail_stop`：保持 `waiting_decision`，必须由高风险人工操作选择 `accept_visible_gap` 或 `retry_same_mutation`。
- `retry_same_mutation` 只适用于 `failed_terminal`，且仅在 Gateway 从未开始，或已有 authoritative safely-not-executed/remote absence evidence 时允许；创建新 Attempt，但复用同一 obligation、Route/Execution snapshot 和 mutation identity。unknown/inconclusive/unknown-deadline case 永远不得选择 retry。
- 决策使用 expected revision CAS，记录 actor/reason/evidence hash/AuditLog；不存在通用“跳过并标成功”。

## 6. Source Update、启动水位与 gap 恢复

### 6.1 `start_from_now` 无漏消息流程

Telegram 远端与 PostgreSQL 不存在跨系统原子事务，因此禁止把“读 max message id + 读 PTS + 本地提交”描述为原子快照。正确流程：

1. Precheck 冻结 source peer、listener authorization、session generation 和 Task revision。
2. 取得或初始化该 authorization 的共享 `TelegramAuthorizationUpdateState`；等待其 collector 持有有效 fencing 并开始持久化 ingress，任务 worker 不创建第二个 common update collector。
3. 锁共享 state，在同一 PostgreSQL 事务中创建 `CloneSourceStreamState(initializing)` 和 `TelegramAuthorizationUpdateSubscription(initializing)`，冻结当时 `last_ingress_order_no`；collector 从该事务提交后就为此订阅持久化 delivery，即使 boundary 尚未完成也不丢事件。
4. 使用同一 authorization 取得 source channel PTS 和当前最大 source message id，将它们 CAS 写入 stream state 并激活 subscription，形成可审计 start boundary；初始化期间已落 delivery 按该 boundary 分类，不能直接丢弃内存队列。
5. source stream 从冻结 ingress order 开始消费自己的 durable deliveries；共享 collector 从自身 state 执行 `updates.getDifference`，source channel gap/too-long 由 stream 通过同 authorization 执行 `updates.getChannelDifference`，按 slice/final 持久化中间 state。
6. 只有 gap 闭合且 source event identity 确认在 start boundary 之后，stream owner 才以 CAS 分配连续 `stream_order_no` 并创建 obligation；boundary 前内容只推进消费水位，不回放。
7. authorization ingress 与 source channel difference 都追平、task stream 无 gap 后进入 `live`，Task 才允许 materialize Action。

范围判定以“原始 New 消息是否在 start boundary 之后”冻结：边界前创建的旧消息即使在任务启动后发生 edit/delete/pin，也记为 `out_of_scope_before_start`，只推进 update state，不创建远端 mutation。Topic service event 同样按其 source event identity 与 boundary 判定。

### 6.2 gap 与乱序

- 发现 `seq/pts/qts/channel_pts` 缺口时可等待 Telegram 推荐的短乱序窗口；缺口未自然补齐则进入 `gap` 并停止该 source 后续 obligation 的 Gateway 推进。
- gap recovery 必须分页到 final，逐页提交 stream state 和 source event 幂等事实。
- worker 重启从数据库 state、owner fencing 和过期 lease 接管，不从内存 cursor 继续。
- gap 期间已收到的较新事件可以持久化为 pending，但不能越过缺失顺序进入 Sequencer。
- listener account/session 变化必须创建新的 stream owner version，并先从旧 state 完成 difference 交接；不能清空 PTS。
- `stream_order_no` 是进入 Clone Sequencer 的唯一 source 顺序输入；`observed_at`、数据库自增主键、worker 到达时间和裸 PTS 都不得单独决定顺序。

### 6.3 Boundary 前依赖的按需建立

- boundary 后消息引用 boundary 前父消息时，不回放父消息，但保存 `out_of_scope_parent` 依赖事实并立即进入冻结 orphan reply policy，不得无限等待不存在的 obligation。
- boundary 后消息位于 boundary 前已存在 Topic 时，按 §5.6 lazy bootstrap 只建立 Topic 元数据和目标 Topic，不回放 Topic 内历史消息。
- fresh fetch 无法证明父消息或 Topic canonical identity 时使用 `source_dependency_unproven`，进入 block/manual review；禁止按标题或最近消息猜测。

## 7. 内容处理、媒体与生命周期动作

### 7.1 清洗流水线

```text
immutable source snapshot
  -> protected/unsupported admission
  -> frozen rule-set input filter
  -> link and @mention rewrite
  -> UTF-16 entity rebuild and validation
  -> output policy
  -> immutable sanitized snapshot + fingerprint
  -> obligation materialization
```

- quote fallback 必须重新经过同一冻结 `sanitization_revision`；不得把已过滤父消息内容重新带回目标群。
- 清洗失败显式进入 `failed_terminal(content_transform_invalid)` 或 manual review，不允许退化成原文直发。
- 配置修改只影响修改后新观测事件；已冻结 sanitized snapshot 保持不变。

### 7.2 媒体能力矩阵

| 类型 | V1 合同 |
|---|---|
| text/photo/video/document/audio/voice/video_note/animation | 在源账号可访问、未保护、目标账号具备权限且 acquisition 成功时重新发送 |
| album | 按 Manifest 冻结集合；Telegram 单批上限内原子发送，partial 只能记 degraded |
| sticker | 只有可验证为 Telegram sticker 的原对象才按 sticker 发送；普通 `.webp` 不伪装为 sticker |
| poll | 重建题面、选项、多选/匿名配置；不复制票数和投票人 |
| contact/location/venue | 仅在数据字段完整且规则允许时重建；否则 block/manual review |
| dice/game/invoice/giveaway/paid/service message | V1 unsupported，进入 block/manual review，不用文本假装成功 |

媒体取得路径必须显式记录：

- 同一授权账号同时可读 source、可写 target 时，fresh refetch 源消息并验证 peer/message/media fingerprint 后使用可用 Telegram media reference。
- source listener 与 target sender 不同时，使用 tenant 隔离的加密临时 material cache；保存 source identity、cache version、hash 和 TTL，不保存临时 file reference 作为长期事实。
- 任一路径不可访问、source 已删除、类型变化或 hash 不一致时显式失败；禁止静默改用文本、其他文件、其他 source 或其他目标群。

### 7.3 New/Edit/Delete/Pin

- **New**：创建 send obligation；绑定 sender、Topic/Reply/Album 依赖后进入 Sequencer。
- **Edit before initial Gateway**：更新冻结的待发送 content revision，旧 edit obligation 记 `superseded`；初次发送必须使用最新合法 revision。
- **Edit after initial success**：依赖原 message mapping，使用 mapping 冻结的原发送 authorization 创建 sender-role edit Action；目标消息不存在、原 authorization 已失效或无编辑权限时进入 `failed_terminal` 和 Sequencer head case，禁止换号编辑。
- **Delete before initial Gateway**：原 send obligation 与 delete obligation 均进入 `cancelled(no_remote_mutation_required)` domain terminal，不物化 FOP/Action/Attempt，不写远端成功事实；事件结果在详情中单列“无需远端动作”，不得计 strict remote success。
- **Delete while initial unknown**：等待原 mutation reconcile；确认存在后使用 frozen target control 执行 delete，确认 safely-not-executed 后取消，永久 unknown 则进入 permanent-unknown Sequencer head case，不猜测。
- **Delete after success**：使用 config revision 冻结的 target control authorization 创建 control-role delete Action，只有 delete typed fact 才成功；V1 不在运行时另选管理员。
- **Pin**：依赖目标 mapping，使用 config revision 冻结的 target control authorization 校验置顶权限；pin/unpin 分别保存 mutation kind。依赖消息未成功时等待，不换 control account、不改 pin 到其他消息。
- **Topic lifecycle**：Topic create/edit/close/reopen/delete 全部使用 frozen target control authorization；source topic sender 只作为审计事实，不决定目标管理员账号。
- sender-role Edit 继续引用初始 send 的 `account_id/authorization_id/session_generation/execution_binding_hash`。control-role Delete/Pin/Topic 引用 config revision 的 `control_account_id/control_authorization_id/session_generation/execution_binding_hash`。两者都必须取得各自 authorization transport 串行权并重新校验 membership/permission；当前 sender binding 或其他管理员变化不得改写冻结角色。

## 8. Sender Binding、Transport 与账号异常

### 8.1 生命周期

```text
active <= 30m
guarded <= 120m
eligible <= 720m and no unresolved dependency
expired after safe release
```

实际阈值来自冻结 type_config。VIP binding 只能人工释放。

### 8.2 原子分配

1. 锁定 Task 当前 epoch、共享 target mutation authority 和候选 `clone_account_slots`。
2. 排除 disabled/cooling、无有效 authorization、目标 membership/权限不满足的账号。
3. 从 durable facts 校验最近发言、未决 reply/topic/album/unknown 和最低 tenure。
4. 建立 history version，更新 slot state，并由部分唯一约束兜底。
5. 冲突时事务回滚并重新求解；没有安全账号时义务留在 `waiting_binding`，不使用公共兜底号。

### 8.3 FloodWait、SlowMode 与失效账号

- FloodWait 写 authorization/session generation 级共享 `TelegramAuthorizationTransportState.blocked_until`；同一 authorization 的所有任务和目标都必须等待。Clone slot 只同步显示投影，不能提前解除。
- SlowMode 写 authorization + canonical target 级 transport fact，只阻断该账号对该目标；两类 block 都由 Gateway 前 admission 读取最大有效 `blocked_until`。
- 只有 `blocked_until < obligation deadline` 才自动恢复；跨 deadline 按 failure order policy 明确阻断或 visible gap。
- Session revoked、账号封禁、目标禁言等权威事实把 slot 置 disabled；不得自动把已进入 Gateway 或 unknown 的 obligation 换号。
- 人工换绑必须确认该 sender 无 open reply dependency、无 Gateway-started/unknown mutation，并生成 `persona_discontinuity` 审计；否则拒绝。
- sender-role lifecycle obligation 使用历史原发送 authorization；control-role lifecycle obligation 使用冻结 target control authorization。任一角色不可恢复时进入 `failed_terminal`/Sequencer head case，不因当前 binding/slot 或管理员列表变化自动换号。

## 9. 目标群 Sequencer 与实际 Gateway 保序

### 9.1 序号分配

- Sequencer 作用域是 `(tenant_id, mutation_authority_id, authority_holder_id, cutover_generation)`，不是单账号或 Clone 私有 owner。
- 只消费连续 `stream_order_no` 并分配单调目标序号；相册作为一个原子序号，内部 part 有固定 index。缺号、重复号或跨 epoch 号一律停止 materialization。
- `planned_at` 只决定最早执行时间，不证明顺序。

### 9.2 Gateway 门禁

序号 N 进入 Gateway 前必须同时满足：

1. N 是当前 authority holder 的最小可推进非终态序号。
2. 所有显式 Reply/Topic/Album 依赖已按策略解决。
3. N-1 已进入允许推进的 durable 状态。
4. Route/Execution binding 二次校验通过。
5. 共享 target mutation authority 的 holder、route、generation、gateway side 和 version 二次校验通过，并写入 Gateway journal。
6. 无同 authority holder 的 executing/unknown mutation 越过门禁。

前序状态矩阵：

| N-1 状态 | N 是否推进 | 合同 |
|---|---|---|
| `succeeded` | 是 | typed remote fact 已绑定 |
| `filtered/blocked/cancelled/superseded` | 是 | 已有明确无远端副作用的终态事实 |
| `degraded` | 是 | 用户已显式选择允许 degraded，UI 永久标识 |
| `failed_terminal` | 条件推进 | 必须存在 `clone_sequencer_head_case`；`visible_gap_accepted` 可推进，`retry_authorized` 只允许原 head 重试，直到 retry succeeded 才推进 |
| `waiting_* / ready / executing` | 否 | 等待当前义务 |
| `unknown_after_send` | 否 | 直到确认或 unknown deadline 关闭 |
| `remote_reconcile_only` | 条件推进 | 不再发起 mutation并保留永久 unknown tombstone；必须有 unknown-deadline head case，只有 `visible_gap_accepted` 才推进 |

若 unknown 到 deadline 仍无法判断，追加 `unknown_deadline_closed` 事实并进入 `remote_reconcile_only`，同时按 frozen failure policy 创建 Sequencer head case。后续只读 reconcile 可修正历史统计，不得重新发起旧 mutation；在 `fail_stop` 下人工接受永久不确定缺口前，下一序号仍阻断。

`continue_with_visible_gap` 不是把失败改写为成功，而是由冻结 policy 自动生成可审计的 gap acceptance；`fail_stop` 必须等待人工决策。所有后续 obligation 永久保留其前序 gap case id，UI 与导出不得隐藏该缺口。

## 10. `random_id`、Gateway 与 Reconcile

### 10.1 mutation identity

- messages.sendMessage/sendMedia/sendMultiMedia 每个 part，以及 channels.createForumTopic，在 Gateway 前生成并持久化非零 signed 64-bit `random_id`；输入为 contract、tenant、task、epoch、obligation、mutation kind、part index、materialization version、`derivation_version` 和持久 `collision_nonce` 的稳定 hash。Topic create 使用固定 part index 0。
- 唯一冲突发生在任何 Attempt `before_call` 之前时，锁 `TelegramGatewayMutationIdentity`，以 CAS 增加 `collision_nonce` 并重新派生，直到在 `(telegram_account_peer_id, canonical target peer)` 的跨任务、跨方法、跨 session 永久域内唯一；每次冲突写审计。`before_call` 一旦存在，nonce/random_id 永久不可变，冲突不得通过换号、新 session 或新建 obligation 绕过。
- `random_id` 必须在 Attempt `before_call` 之前持久化；相册每个 input media 有独立 identity/random_id，Topic create 的 identity 绑定 TopicMap placeholder。
- edit/delete/pin 等无 random_id 的方法仍必须持久化 gateway request identity、target fingerprint 和 mutation kind。

### 10.2 raw MTProto Gateway 合同

- Clone send/Topic create 路径必须新增可注入已持久 `random_id` 的 raw MTProto adapter，分别使用与 Telegram 方法匹配的 messages.sendMessage/sendMedia/sendMultiMedia 与 channels.createForumTopic request；不得直接调用一个无法接收 frozen random_id 的高层 helper。
- adapter 输入必须是 immutable request DTO：Route/Execution snapshot、mutation identity、authorization session generation、内容/media fingerprint、reply/topic ids 和 target authority version；adapter 不自行选账号、目标或生成 random_id。
- raw RPC 返回的 Updates 在返回业务层前先交给同 authorization update ingress，提取 exact `updateMessageID` 和目标 message id；Gateway journal 再保存 adapter evidence hash。RPC 返回丢失时仍由同一 ingress/getDifference 恢复。
- source collector、outbound collector 和 Gateway client 必须复用 authorization 级 transport/session coordinator；并发调用不能各自推进 common pts 或覆盖 session state。

### 10.3 unknown 正确处理

RPC timeout、连接断开、worker 丢失或响应无法持久化时：

1. Attempt/Action/obligation 进入 unknown，并创建或复用 `(action_id, execution_attempt_id)` 唯一 `RemoteReconcileCase`。
2. 禁止扫描普通聊天 history 按 random_id 查找；普通 Message 不暴露该字段。
3. Reconcile 绑定 Attempt 冻结的 `gateway_mutation_identity_id/authorization_update_state_id/session_generation`，先查 exact outbound mapping；缺失时由该共享 ingress 按原 common state 通过 `updates.getDifference` 恢复 `updateMessageID(id, random_id)`。Topic create 得到的 id 写入 target top message mapping。
4. Gateway journal 的 exact request identity 和 authoritative adapter evidence 可证明 success、safely-not-executed 或 inconclusive。
5. success 必须写 typed remote fact；只有 authoritative safely-not-executed 才能回到可重试状态。
6. “当前没查到消息”“另一个账号不可见”“多次 history 查询为空”都不是未发送证据。

7. 旧 session generation 已不可恢复且没有 authoritative evidence 时只能保留 unknown/remote-reconcile-only tombstone，禁止使用新 session、另一个账号或 history miss 重试。

### 10.4 typed remote facts

至少定义：

- `clone_message_observed`
- `clone_album_observed`
- `clone_edit_observed`
- `clone_delete_observed`
- `clone_pin_observed`
- `clone_topic_observed`
- `clone_poll_observed`
- `unknown_deadline_closed`

每个事实包含 obligation、Action、Attempt、mutation identity、target binding hash、`execution_role/account/authorization/session_generation`、`authority/holder/version`、target peer、remote message/topic ids、observed time、desired-state fingerprint 和 evidence hash。

`unknown_deadline_closed` 是 `outcome_class=tombstone` 的 typed closure fact，不是 remote success fact，不能把 obligation/Action 改成 succeeded，也不能进入 strict/degraded 成功分子。

### 10.5 非 Send 生命周期 mutation 的 Reconcile

random_id 只用于 Send 类方法。Edit/Delete/Pin/Topic RPC response 丢失时，reconcile 必须绑定 frozen target、message/topic id、execution role 和 request fingerprint，并使用 mutation-specific authoritative readback：

| mutation | desired-state confirmed | safely-not-executed | inconclusive |
|---|---|---|---|
| Edit | exact target message id 的当前内容/entities fingerprint 等于冻结新 revision | adapter 证明 pre-accept rejected | 仅看到旧内容、消息暂不可见或账号不同 |
| Delete | 已有先前 message mapping，target-specific readback 权威确认该 id 不存在/已删除 | adapter 证明 pre-accept rejected | 权限不足、peer 不可见或一般 history miss |
| Pin/Unpin | target authoritative pinned state 精确包含/不包含冻结 message id | adapter 证明 pre-accept rejected | 列表不完整、权限不足或读取失败 |
| Topic create | frozen random_id 的 outbound `updateMessageID` 得到 target top message id，且该 Topic fingerprint 等于冻结 revision | adapter 证明 pre-accept rejected | 只按标题找到 Topic、mapping 缺失或读取不完整 |
| Topic edit/close/reopen/delete | 以已映射 target top message id 读取的 title/icon/open/closed/deleted 状态等于冻结 revision | adapter 证明 pre-accept rejected | 按标题搜索、topic 不可见或读取不完整 |

desired-state confirmed 可写对应 `clone_*_observed`，但 evidence 必须标记 `observation_after_unknown`，不虚构由我方账号完成的因果；其含义是目标状态已满足。除 adapter pre-accept rejected 或其他 authoritative safely-not-executed 外，状态不匹配本身不授权重试。

## 11. Reply、过滤、Topic 与人工审核

### 11.1 Reply 依赖

- 原生 reply 只有父消息 `succeeded` 且目标 mapping 与当前 target binding 一致时可发送。
- 父消息还在 waiting/executing/unknown 时，子消息进入 `waiting_dependency`。
- 父消息 `filtered|blocked|failed_terminal|out_of_scope_before_start|source_parent_missing|parent_mapping_unavailable` 时立即按冻结 `orphan_reply_policy`：
  - `drop_subtree`：子树进入 filtered。
  - `quote_fallback`：父文本摘录重新经过冻结 sanitizer，去除原生 reply 后发送；结果标记 degraded。
  - `block_for_review`：进入 `waiting_manual_review`。
- 不允许把 reply 自动改到“最近一条目标消息”。
- `quote_fallback` 只有 fresh fetch 能证明父消息 canonical identity 且内容授权/清洗通过时可用；否则只能 `drop_subtree|block_for_review`，不能从非权威缓存拼接父文本。

### 11.2 人工审核

- 审核列表展示 source preview、命中原因、sanitized preview、依赖、目标和账号，但敏感字段按 tenant 权限脱敏。
- `release` 生成新 review revision 并重新执行 Gateway 前全部校验；不是直接把旧 Action 改回 pending。
- `filter/block` 生成明确终态事实并按 Sequencer 状态矩阵推进。
- 审核无默认超时自动放行；任务可以因审核长期阻断，UI 必须展示。

### 11.3 Sequencer Head 决策

- 当前 head 为 `failed_terminal|remote_reconcile_only` 时详情页必须展示 case kind、failure/unknown evidence、是否进入过 Gateway、authority/session generation、允许的决策和对后续序号的影响。
- `accept_visible_gap` 只写 gap decision，不创建 Telegram mutation、不改变 failed/permanent-unknown 结果、不计 strict/degraded success。
- `retry_same_mutation` 只有服务端验证 Gateway 未开始或 authoritative safely-not-executed 后出现；执行时重跑 Route/Execution/authority/transport 校验并创建新 Attempt，复用原 mutation identity。
- retry 再次失败时 case 增加 revision 并回到 `waiting_decision|blocked`；retry 自身从不放行下一个 sequencer id。
- unknown-deadline、inconclusive、目标/账号已不可证明或冻结执行角色不可恢复时不提供 retry；只能保留阻断或接受 visible gap。

## 12. 生命周期、Cutover 与 Rollback

### 12.1 Pause/Resume/Archive

- pause 停止新 source event 物化和新 Gateway mutation，但 listener 可按配置保持 update state；已 Gateway-started/unknown 继续 reconcile。
- pause 保留 exclusive authority/holder，防止其他平台 writer 在暂停期接管克隆群；它不是释放目标的操作。
- resume 不增加 epoch，先完成 gap recovery，再从原 sequencer 恢复。
- archive 只允许无 executing mutation，且 unknown 已转 remote-reconcile-only 并按 failure policy完成 head decision；随后在单事务中停 subscription、停用 holder，把无 holder 的 authority 置为 `vacant/shared/no_admission`（保留 authority row 作为后续 claim 串行点），证据和 tombstone 保留。
- delete 走项目现有软删除/审计合同，不级联删除 remote facts；authority 释放条件与 archive 相同，释放失败则 delete 阻断。

### 12.2 `group_relay -> group_clone` Cutover

V1 自动 Cutover 只支持整个旧任务就是一条 1→1 route。Preview 必须把 `source_groups`、`target_group_id(s)`、`target_operation_target_id(s)` 和 routing 规则全部解析为 canonical route manifest；去重后必须恰好一个 source peer、一个 target peer，且不存在动态或运行时扩展目标。否则返回 `cutover_route_scope_unsupported`，不生成可 apply token。V1 不实现“只暂停旧任务中的一条 route”。

1. `preview`：冻结旧任务 revision、唯一 route manifest/hash、旧 listener cursor、source event identity 可用性、全部 open/Gateway/unknown Action、共享 target mutation authority/legacy holder 和其他 holders；任何无法归属于唯一 route 的 Action 都阻断。
2. `apply`：校验 preview token、旧 revision、route manifest hash、authority/legacy holder version 和 open-action fingerprint 未变化；在单事务中把 authority 切到 handoff、创建 new Clone holder/cutover generation/shared exclusion rows，并把整个单-route 旧任务置 `cutover_paused`。事务外不发 Telegram mutation。
3. 新任务以 cutover boundary 初始化 source stream，通过 difference 只补 boundary 后增量。
4. 旧任务只 reconcile 既有 Gateway/unknown，不再为 boundary 后事件创建 Action；旧 executor 在 Gateway 前必须检查 shared exclusion 和 authority `gateway_admission_side`。
5. authority 先给旧侧关闭新 mutation，待旧 executing 收口并固化 unknown exclusion 后才切到新侧；任何时刻不得两侧同时 admission。
6. 在 authority 仍为 handoff 且 admission side=new 时验证新 source stream live 和首个 canary typed fact；随后单事务停用 old holder、把 mode 切为 `exclusive_clone`、归档旧任务，并独立 readback holder 唯一、side/generation 和旧 Task 状态。

### 12.3 Rollback

- 先暂停新任务并关闭其 Gateway admission，等待 executing 收口；unknown 写入 exclusion tombstone。
- 记录新任务最大 admitted source identity 和所有 Gateway/remote fact。
- 旧任务从 rollback boundary 恢复，但必须排除 exclusion 中新任务已 Gateway-started、unknown 或 succeeded 的事件；只有 preview 冻结的单 route 旧任务可恢复。
- rollback authority 先进入 handoff 并只给 old side admission；验证旧任务恢复水位后再停用 Clone holder，将 authority 切回 `shared` 的单 legacy holder。任一步 readback 不一致都保持两侧新 mutation 关闭。
- 无法建立旧 source event identity 映射时 rollback 阻断并要求人工处理，不能声称“不会反向重放”。
- rollback 不删除新任务历史事实，也不把 degraded/unknown 改写成成功。

## 13. API、权限与审计

### 13.1 API 合同

新增并接入现有 Task Center 生命周期：

- `POST /api/tasks/group-clone`
- `POST /api/tasks/group-clone/create-and-start`
- `PATCH /api/tasks/{task_id}/group-clone`
- `POST /api/tasks/group-clone/precheck`
- `GET /api/tasks/{task_id}/clone-source-events`
- `GET /api/tasks/{task_id}/clone-obligations`
- `GET /api/tasks/{task_id}/clone-bindings`
- `GET /api/tasks/{task_id}/clone-message-mappings`
- `GET /api/tasks/{task_id}/clone-reconcile-cases`
- `GET /api/tasks/{task_id}/clone-update-ingress-status`
- `GET /api/tasks/{task_id}/clone-manual-reviews`
- `POST /api/tasks/{task_id}/clone-manual-reviews/{review_id}/decision`
- `GET /api/tasks/{task_id}/clone-sequencer-head-cases`
- `POST /api/tasks/{task_id}/clone-sequencer-head-cases/{case_id}/decision`
- `POST /api/tasks/{task_id}/clone-bindings/{binding_id}/release/preview`
- `POST /api/tasks/{task_id}/clone-bindings/{binding_id}/release/apply`
- `POST /api/tasks/{legacy_task_id}/group-clone/cutover/preview`
- `POST /api/tasks/{legacy_task_id}/group-clone/cutover/apply`
- `POST /api/tasks/{clone_task_id}/group-clone/rollback/preview`
- `POST /api/tasks/{clone_task_id}/group-clone/rollback/apply`

cutover 路径中的 `legacy_task_id` 必须指存量 `group_relay`。服务端自行解析完整旧配置并证明唯一 1→1 route，前端不得传 route id 从多 route 任务中选择一条；apply payload 携带 preview token、旧 revision、route manifest hash、authority version 和新 clone 配置。rollback 路径中的 `clone_task_id` 必须指由该 cutover 创建的 `group_clone`，服务端从 cutover generation 解析旧任务，前端不得自由指定另一旧任务。

sequencer-head decision payload 只允许 `accept_visible_gap|retry_same_mutation|keep_blocked`，携带 expected case revision、reason 和 client request id。unknown-deadline case 不接受 retry；failed case 的 `retry_same_mutation` eligibility 由服务端基于 Gateway/Attempt/authoritative absence evidence 计算，前端布尔值不构成授权。

列表接口必须分页/keyset，不返回整表。所有写接口使用 tenant scope、expected revision、client request id、reason 和 AuditLog。

`clone-update-ingress-status` 只返回本任务 authorization state id、session generation、owner/lease health、task subscription/delivery lag 和 gap 摘要；不得返回其他订阅、其他群 update identity、正文或 outbound mapping 明细。

### 13.2 权限

- 查看任务/事件/统计：复用 Task Center view 权限。
- 创建、修改、启动、暂停：复用 Task Center manage 权限。
- 查看账号映射只展示平台账号 ID 和固定人设标签，不显示 Session/AuthKey/2FA/代理密码。
- manual release、binding reset、sequencer-head decision、cutover、rollback 属于高风险操作，要求 manage 权限、原因和二次确认 revision。
- 只有 tenant 内账号、群、OperationTarget、规则和任务可互相引用；跨 tenant 请求返回 404/forbidden，不泄露对象存在性。

### 13.3 审计事件

至少记录 `clone_created/config_revised/started/paused/resumed/binding_created/binding_released/manual_decided/sequencer_head_decided/random_id_collision_rederived/update_ingress_owner_changed/transport_blocked/target_authority_claimed/target_authority_released/target_authority_blocked/cutover_previewed/cutover_applied/rollback_applied/unknown_closed/target_binding_blocked`。

## 14. 前端创建与运行详情

### 14.1 创建向导

1. 选择并验证 source group 与授权模式。
2. 选择 target group，显示 source≠target、共享 mutation authority holder、其他平台 writer/open unknown 和 route cycle 检查。
3. 选择 listener account 与 sender pool，逐账号展示读取/加入/发送/Topic 权限、online readiness、update ingress/session generation 和共享 transport block。
4. 配置固定规则、Reply/Album/失败策略和拟人延迟。
5. 展示完整 Precheck；硬阻断不可绕过，warning 需明确确认。
6. 创建或创建并启动；重复点击复用 client request id。

### 14.2 详情页

首屏展示：

- Task 状态、authorization ingress/source stream state、source/target canonical identity、epoch/config revision。
- source events、eligible obligations、strict success、degraded、filtered、blocked、failed、unknown 和 lag。
- 当前 Sequencer head、`stream_order_no`、case kind/决策、阻断原因、账号全局 FloodWait/目标 SlowMode、manual review 数。
- sender binding 数、可用 slot、cooling/disabled 账号。
- 当前共享 target mutation authority holder/version/gateway side、最近 typed remote fact 和最后 successful update gap recovery。

下钻链必须可见：

```text
Source Event -> Obligation -> FOP -> Action -> Attempt
             -> Gateway/Reconcile -> Typed Remote Fact -> Target Mapping
```

完成率：

- 分母是可执行 eligible obligation，不包含明确 filtered/blocked/no-remote-required。
- 分子只包含绑定 canonical typed remote fact 的 `succeeded`。
- `degraded` 单列，不计 strict success。
- `unknown_after_send/remote_reconcile_only` 永不计成功；接受 unknown visible gap 也不改变统计类别。
- `failed_terminal + visible_gap_accepted` 仍计 failed，并在后续记录上显示 inherited visible gap；不得计 degraded success。

## 15. 失败分类与可观测性

### 15.1 失败分类

- source：`source_peer_unresolvable|source_gap|source_access_revoked|source_content_protected|source_event_invalid|source_stream_order_gap|source_dependency_unproven`
- target：`target_binding_mismatch|target_binding_unproven|target_mutation_authority_conflict|target_mutation_authority_stale|target_write_forbidden|topic_permission_missing|topic_bootstrap_unproven`
- account：`authorization_invalid|authorization_update_ingress_unavailable|authorization_session_generation_mismatch|session_revoked|account_restricted|membership_missing|transport_flood_wait`
- content：`content_filtered|entity_rebuild_invalid|unsupported_media|album_incomplete|media_source_changed`
- execution：`gateway_pre_accept_rejected|unknown_after_send|remote_fact_conflict|unknown_deadline_closed|sequencer_head_decision_required|lifecycle_authorization_unavailable`
- migration：`cutover_identity_unproven|cutover_route_scope_unsupported|cutover_manifest_changed|cutover_unknown_blocked|rollback_exclusion_unproven`

错误必须携带 `scope/source|target|account|obligation`、是否可重试、retry_at、evidence id 和用户可读说明；禁止只写自由文本。

### 15.2 健康与告警

worker/container healthy 不能证明克隆健康。任务健康至少同时观察：

- authorization update ingress 与 task source stream 是否 live、是否有未恢复 common/channel/order gap。
- eligible source event 是否产生唯一 obligation/FOP。
- Sequencer head 是否推进及阻断原因。
- Attempt/Gateway unknown 数和最老 unknown 年龄。
- typed remote fact 增长与 source-to-target lag。
- target binding/authority mismatch、账号池容量、共享 transport block 和权限变化。
- stopped/archived Task 是否仍异常持有 authority，及 initializing subscription 是否超时未完成 boundary。

## 16. QA 验收矩阵

### 16.1 数据库与并发

- UUID Task 外键、tenant 隔离和 Telegram 64-bit/string peer identity。
- 两个 create-and-start 并发申请同一 target 时只有一个领取 exclusive holder；失败方没有 subscription/Action。领取后 Telegram boundary 初始化失败走 fenced cleanup，cleanup 未完成时保持 fail closed。
- 重复/并发 New/Edit/Delete/Pin update 只生成一份正确 revision/obligation。
- 两个 Planner 并发物化时只有一条 open FOP/Action。
- 空闲账号也有可锁 slot；并发 sender 分配不撞号。
- authorization update state/ingress order、共享 target authority、sequencer、message mapping 和 Gateway mutation identity/random_id 唯一约束在 PostgreSQL 真实并发下成立。
- collector 在 event 已持久但 task fan-out 前崩溃时，pending delivery 被接管重放；common cursor 推进事务不允许缺少当时匹配 subscription 的 durable delivery。
- random_id 人工制造跨 Task、send/Topic create、旧/新 session generation 唯一冲突时，只在 before_call 前 CAS 增加 nonce；before_call 后 immutable，Task archive/retention 不释放 identity tombstone。
- worker restart/pause/resume 不增加 epoch；显式 reset 才增加。

### 16.2 Source stream

- start boundary 读取后、live subscription 前到达的消息仍通过 difference 捕获。
- 同 authorization 多任务只有一个 common update collector；subscription 初始化与 boundary 并发、pending delivery crash replay、common gap、channel gap、difference slice、too-long、session generation change 和 owner fencing 接管。
- 同一语义事件分别从 live update 和 difference 返回、constructor/可选 PTS 表达不同，只生成一个 semantic source event/stream order。
- boundary 前 update 只推进 state，不创建 obligation；boundary 后不漏不重。
- edit/delete 先于 base 到达时等待并最终按 source 顺序收口。
- gap 期间较新 event 不分配可推进 `stream_order_no`；恢复后连续编号，Sequencer 不按 observed_at/裸 PTS 排序。
- boundary 后消息位于旧 Topic 时 lazy bootstrap；回复 boundary 前父消息时立即进入 orphan policy，不永久 waiting。

### 16.3 目标隔离

- source=target、直接/间接 route cycle、exclusive Clone 下重复 holder 均阻断；shared legacy mode 的多个已登记 holder 保持可发送但不能领取 Clone exclusive authority。
- `group_relay/group_ai_chat/MessageTask/Campaign` 在 Clone 持有 exclusive authority 时尝试同目标 mutation，统一 Gateway admission 为 0；authority version 变化后的陈旧 worker 同样阻断。
- Planner 后目标配置、OperationTarget、canonical peer、账号群关系或 reply target 任一变化，Gateway 调用为 0。
- 账号转派不改变 target；不可证明的历史 Action 进入 `target_binding_unproven`。
- source/target 标题相同、username 变化或 ID 数字重叠时不靠标题/裸 ID 路由。

### 16.4 Sequencer 与依赖

- 多账号不同随机延迟下实际 Telegram Gateway 顺序仍严格单调。
- 前序 FloodWait、failed、filtered、blocked、manual review、unknown 和 remote-reconcile-only 逐项符合状态矩阵。
- Reply 不在父 remote fact 前发送；不得自动回复到最近消息。
- Topic create unknown 阻断该 Topic 后续消息。
- `failed_terminal` head 在 fail_stop 下生成 case 并阻断；accept visible gap 后推进但统计仍 failed；只有 authoritative absence 才显示并允许 same-mutation retry。
- `remote_reconcile_only` 在 continue policy 下自动记录 unknown visible gap，在 fail_stop 下等待人工接受；两者都不允许 retry、不计成功。

### 16.5 内容与媒体

- UTF-16 entity 重建覆盖 emoji、组合字符、URL/@mention 替换和 caption。
- quote fallback 再次清洗，不泄漏被过滤父内容。
- album quiet/refetch/fingerprint、乱序、缺片、迟到 part、单批限制和 partial degraded。
- source media 删除、类型变化、file reference 过期、跨账号 cache 和 protected content。
- Poll 只重建题面；unsupported service/paid message 不伪成功。

### 16.6 生命周期动作

- Edit before send 合并 revision；Edit after success 生成 edit typed fact。
- Delete before Gateway 无远端 mutation；Delete during unknown 等待 reconcile；Delete after success 有 delete fact。
- Pin/unpin 权限缺失、unknown 和目标 mapping 不存在。
- binding 过期/slot 已重分配后 Edit 仍使用 message mapping 的原 sender authorization；Delete/Pin/Topic 使用 config revision 冻结的 target control authorization。任一失效时不换号并进入 Sequencer head case。
- sender pool 账号即使具备管理员权限也不得代替 frozen target control；control 变更只能新 config revision，既有 lifecycle obligation 不动态重解释。
- 同 authorization 在另一任务收到 FloodWait 后，本任务 Gateway 也等待；SlowMode 只阻断对应 target scope。
- Edit/Delete/Pin/Topic 分别覆盖 response 丢失后的 desired-state confirmed、pre-accept rejected 和 inconclusive；一般 history miss 不构成重试证据。

### 16.7 Reconcile

- raw adapter 对 Send 和 Topic create 确实注入独立 mutation identity 中冻结的 random_id；RPC response 丢失后由 Attempt 冻结的同 authorization/session generation ingress 通过 `updateMessageID/getDifference` 恢复 target message/topic id。
- sender update ingress gap、lease 接管、worker crash 和旧 session generation 不可恢复分别验证；不得创建第二 common collector。
- 普通 history 不返回 random_id，系统不据 history miss 重试。
- Gateway journal success/safely-not-executed/inconclusive 三分支。
- unknown deadline 后只读 reconcile，永不创建第二 remote mutation。
- late fact 修正历史统计但不触发重发。

### 16.8 API/UI/权限与迁移

- create idempotency、PATCH expected revision、跨 tenant、分页和脱敏。
- `starting/start_cleanup_pending/start_failed` 状态、相同 client request id readback、pause 保留 holder、archive/delete 置 authority vacant 的 CAS 与审计。
- manual review CAS、权限、reason 和 Sequencer 恢复。
- 多 source/target legacy task preview 返回 `cutover_route_scope_unsupported` 且无 apply token；单 route cutover 校验 manifest/revision/authority/open-action fingerprint。
- legacy authority backfill 可为同 target 多个可证明 writer 建 shared holders；canonical target 不可证明时形成阻断清单，绝不猜测或绕过 authority。
- cutover preview/apply、旧 unknown、shared exclusion、authority gateway side 移交和 rollback 去重。
- sequencer-head failed/unknown decision CAS、权限、reason、服务端 retry eligibility 和 UI visible gap 展示。
- UI strict success/degraded/unknown 分列，Action success 不得单独增加完成率。

## 17. Release Gate 与生产验收

### 17.1 开发前 Resync Gate

开发开始前必须同步：

- `docs/01-product/tg-ops-platform-prd.md`：任务类型、API、完成口径、权限和详情。
- `docs/00-index/project-dataflow-index.md`：本 PRD 第 3.1 节完整主链、共享 authorization update ingress、平台 target authority、source stream、cutover 和 typed fact。
- `docs/00-index/project-structure-index.md`：新增 models/services/router/frontend/test 入口。

若开发开始后本 PRD 再变化，标记 `resync`，dev/qa 必须重新读取新 revision。

### 17.2 发布门禁

- Alembic 在 blank PostgreSQL 和 current production-shaped schema 上 upgrade/downgrade 验证。
- authority backfill preview 固化现存 writer/route/target manifest；apply 后逐项 readback holder 数与不可证明清单。不可证明项未处理前不得启用强制 Gateway admission或发布 Clone，但也不得给这些 writer 增加无 authority 旁路。
- PostgreSQL 并发、唯一索引、claim/CAS、lease/fencing 和 unknown recovery 测试通过。
- 所有现存 Telegram group writer 已接入共享 target mutation authority；全仓搜索与定向测试证明不存在可绕过的旧 Gateway 入口。
- raw MTProto adapter 的 Send/Topic-create frozen random_id、跨 session 永久唯一 tombstone、Update ingress/getDifference 和 session generation 隔离集成测试通过；高层 helper 不得承接 Clone send/Topic create。
- 后端定向测试在 60 秒硬超时内通过；前端 typecheck/build 通过。
- `group_relay legacy_v1` 回归通过；其行为不被新 contract 动态重解释。
- 发布必须走 `master -> release -> GitHub Actions Deploy Production`，核对 deployed SHA、迁移 head 和 runtime readiness。

### 17.3 E4 生产验收

生产完成不能由 CI、部署成功、容器健康、Action 数或 UI toast 证明。Canary 至少证明：

1. 一个授权 source 和一个受控 target 的真实 New、Reply、Edit、Delete、Pin、旧 Topic lazy bootstrap、Album 各产生正确 typed remote fact，并能区分 sender/control execution role。
2. source event、obligation、Action、Attempt、Gateway evidence、remote fact 和目标消息逐项可关联。
3. 断线 canary 从发送/target-control authorization 的持久 ingress 分别恢复消息与 Topic create 的 `updateMessageID`，且无双发/双 Topic，并可关联 frozen mutation identity/random_id/session generation。
4. target binding mismatch 和另一平台 writer 同目标抢写 canary 都在共享 authority/Gateway 前阻断，Telegram 调用为 0。
5. 旧 Topic 内新消息与 boundary 前父消息 Reply 按 frozen policy 得到可解释结果，不永久阻塞。
6. pause/resume、update owner 接管或 worker restart 后无 epoch 变化、无漏发、无历史重放。

只有以上 E4 证据满足，才能写 `production_fixed/production_accepted`。

## 18. Product Handoff

### 18.1 实施入口顺序

1. 平台共享数据模型与迁移：authorization update ingress/events/outbound mapping、Gateway mutation identity/random-id tombstone、authorization transport state、Telegram group mutation authority。
2. 把 `group_relay/group_ai_chat/MessageTask/Campaign` 等现存群写入口全部接入共享 authority admission，先消除旁路。
3. Clone 领域模型：source stream/events、owner projection、binding、album、topic、obligation、mapping、sequencer-head review/exclusion。
4. raw MTProto frozen-random-id Gateway adapter、共享 update collector 与 getDifference reconcile；复用 Action/Attempt/Gateway journal/typed fact 主链。
5. Task schema、create/precheck/lifecycle、source stream/gap recovery、materializer、binding manager 和 Sequencer。
6. New/Edit/Delete/Pin、Reply/Topic lazy bootstrap/Album/media adapters。
7. API/detail projections、manual review、sequencer-head decision、单-route cutover/rollback。
8. 前端向导、详情与告警。
9. PostgreSQL/Telegram 集成 QA、Release Gate 和 E4 canary。

### 18.2 当前实现差距审计（2026-08-30）

当前代码已具备本地可验证的文本 NewMessage 与消息生命周期主链：严格 create/precheck、starting boundary、durable delivery 消费、source event、sender slot/binding、obligation/FOP/Action、冻结 mutation identity/random_id、Gateway 前 route/execution/authorization/transport/authority/Sequencer 复核、Attempt/typed Remote Fact、outbound mapping 与 message mapping；Edit/Delete/Pin、显式 Topic 生命周期、旧 Topic 权威 lazy fetch/create，以及旧父消息权威读取后的 quote fallback 已接入同一主链。API 已提供 source event、obligation、binding、message mapping、reconcile case、update-ingress status 和 Sequencer case 的只读导航；前端已能创建 `group_clone`。

本轮差距修复后，以下能力已有本地代码与定向测试证据，但仍不等于发布或 Telegram 生产验收：

- 共享 Telethon collector 已接入 listener worker，包含 authorization 单 owner/lease/fencing、`updates.getDifference/getChannelDifference`、durable update journal 和 `updateMessageID` 恢复；Topic create 的 random-id mapping 会按 mutation identity 精确关联 `group_clone_mutation` Action。
- Edit/Delete/Pin/Topic 与 Reply lazy bootstrap 均走同一 Action/Attempt/typed-fact 主链；Edit/Delete/Pin/Unpin/Topic Edit/Delete 的 RPC 丢响应后执行冻结期望状态 readback，只有精确匹配才收口成功，不匹配保持 unknown 且不重放。unknown deadline 会转 `remote_reconcile_only` 并创建唯一 Sequencer head case。
- obligation 成功词汇已统一为 `succeeded`；manual review 与 Sequencer decision 均有 manage 权限、revision CAS、请求幂等、原因和审计。详情页已提供 source event、obligation、binding、mapping、ingress lease、manual review、Sequencer case 下钻与受控决策。
- 已识别的群写入口（Task Center 群/目标发送、删除、频道评论/反应、MessageTask、OperationTask、人工即时发送）均在 Gateway 前接入 `TelegramGroupMutationAuthority`；handoff 还必须匹配 exact active-side holder，不能仅凭 `writer_kind` 旁路。一次性 shared writer 在远端调用后释放，调用中断则保留 holder 以 fail-closed。
- 单 route cutover preview/apply 和 pre-mutation rollback 已实现 revision、route manifest、authority version、open-action fingerprint 与 client request CAS；旧任务 unsafe/unknown 或 Clone 已有 Gateway-started mutation 时阻断。该实现只证明安全的“尚未产生 Clone 远端副作用”回滚子集。
- Album quiet/max deadline 已有持久 manifest，并在缺少 fresh-refetch/media adapter 时按冻结策略明确 `filtered` 或 `waiting_manual_review`，不会永久卡住后续文本事件，也不会把部分媒体伪装为成功。

以下仍是 Release Gate 硬阻塞，不得改写成 `implemented_local`、`qa_pass`、已上线或生产完成：

- Album 原子发送、Poll、单媒体下载、tenant 隔离加密缓存、上传和 source fingerprint fresh-refetch adapter 尚未实现；当前只具备显式失败/人工态闭环。
- 完整 cutover exclusion、Clone 已产生远端 mutation 后的去重 rollback、handoff canary 后 finalize 为 `exclusive_clone`、binding release/reset 仍未实现；不得用当前 pre-mutation rollback 代替完整合同。
- 前端尚未提供 legacy task 的 clone-config cutover 表单和受控 sender 换绑；当前只提供 Clone 详情证据、人工处置及符合条件的 rollback。
- 尚无 blank/current PostgreSQL、并发 CAS、真实 Telegram collector/desired-state readback、媒体、割接和 E4 canary 证据；当前证据仅为 SQLite 定向自动化测试与前端生产构建。

### 18.3 禁止实现方式

- 禁止复用 `recent_context_messages` 扫描代替 source event journal。
- 禁止把 Clone Executor 写成绕过 Action/Attempt/typed fact 的第二套发送引擎。
- 禁止从普通目标群 history 查询 random_id 并据 miss 重发。
- 禁止 Clone 使用无法注入 frozen random_id 的高层 `send_message` helper，或为同 authorization 创建 task-local common update collector。
- 禁止任何平台群写 writer 绕过共享 target mutation authority；Clone 私有 owner 不能替代平台 authority。
- 禁止用 `planned_at` 代替实际 Gateway 顺序门禁。
- 禁止 worker 重启递增 epoch。
- 禁止号池不足时公共账号兜底、自动换目标或文本 fallback。
- 禁止把 protected、partial、filtered、unknown、Action success 计为 strict success。
- 禁止 sender/control lifecycle role 因当前 binding 或管理员变化而换用其他账号；禁止把 accepted failed/unknown visible gap 改写为 success/degraded。

## 19. Product Design Complete 自检

以下勾选表示“设计合同已覆盖”，不表示相应代码、发布或生产验收已完成；实时实施状态只以 18.2 为准。

- [x] 用户原始需求：群 1 对 1、发言人稳定账号映射、内容克隆。
- [x] Telegram 身份、内容保护和媒体真实能力边界。
- [x] 新任务类型与 legacy 双轨边界。
- [x] Task/type_config、创建、修改、生命周期和 Precheck。
- [x] 共享 authorization update ingress、source stream、outbound mapping、gap、幂等、乱序和重启恢复。
- [x] sender binding、sender/control lifecycle authority、账号池并发和 authorization 全局 transport。
- [x] target binding、平台共享 mutation authority、防串台与 Gateway 前二次校验。
- [x] obligation/FOP/Action/Attempt/Gateway/reconcile/typed fact 单一主链。
- [x] actual Sequencer 门禁、source order 和 failed/permanent-unknown head 决策闭环。
- [x] New/Edit/Delete/Pin、Reply、旧 Topic lazy bootstrap、Poll、Album 和媒体矩阵。
- [x] raw MTProto random_id、碰撞、updateMessageID、session generation 和 unknown deadline 关闭。
- [x] API、UI、权限、审计、详情完成率、visible gap 和告警。
- [x] 单 route cutover、rollback、shared exclusion 和 authority side handoff。
- [x] QA、Release Gate、E4 与失败边界。

结论：设计合同仍为 `design_status=complete`，且项目真相源已完成 resync。当前实现仅为 `partial_local_validation`；18.2 所列 Release Gate 硬阻塞全部闭合前，不得声称实现完成、QA 通过、已发布或生产已恢复。
