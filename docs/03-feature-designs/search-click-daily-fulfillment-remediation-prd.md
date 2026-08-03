# 纯搜索点击每日履约专项 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产履约修复 |
| 设计状态 | `product_design_complete` |
| 合同版本 | `search_click_contract_v3`，2026-08-04 单目标、连续执行、旧 Task 删除重建 |
| 适用任务 | `task_type=search_click` 且 `search_execution_mode=click_only` |
| 唯一完成事实 | `target_click_observed` |
| 当前实现真相源 | 本文件、`task-fulfillment-classified-recovery-prd.md`、`task-fulfillment-contract-closure-prd.md`；主 PRD/主数据流索引已同步产品合同，项目结构索引待代码完成后更新 |
| 非完成证明 | Action 创建、assignment 落库、按钮可见、代理连通、membership、join/admission、worker 心跳均不等于完成 |

本文完全取代本文件 2026-07-26 至 2026-08-03 的中央共享 Dispatcher、跨 Window 预扣、二次容量确认、`search_join` 双目标、验证码 AI/VLM 和点击后入群设计。旧内容仅存在于 Git 历史，不得作为实现或 QA 依据。

`search-click-boost-prd.md` 中的关键词/排序研究可保留为只读协议参考；其 `search_join_group`、membership/admission、创建前容量证明、点击后加入和旧完成口径均为 `historical_do_not_implement`。

## 2. 产品目标与边界

### 2.1 目标

1. 每个任务日在 deadline 前完成配置的 `daily_click_target_count` 个真实目标点击。
2. 搜索点击使用独立 planner、solver、assignment、worker、代理/OCR 资源与熔断，不被 AI、评论、点赞、浏览队列阻塞，也不阻塞它们。
3. solver 已原子落库的 assignment 立即执行到业务 deadline，不跨 Window 预扣，不等待第二次容量分配。
4. 点击只认明确的 `target_click_observed`；任何未知结果都先对账，禁止换账号、换代理或换 assignment 重点。
5. 验证码只按 RapidOCR → ddddOCR 顺序处理，不调用 AI/VLM。

### 2.2 非目标

- 不执行搜索结果入群、关注、群管准入或成员数量目标。
- 不把候选组合数、Action 数、Attempt 数或 assignment 数解释为可确认点击数。
- 不建立搜索与普通 Telegram 任务的共享 active claim、公平 cursor 或预扣池。
- 不用直连、隐式换代理、模拟点击、默认成功或 Action success 代替远端事实。
- 不因单个账号/代理失败下调日点击目标；只有真实点击才减少 remaining。

## 3. 创建与任务日合同

### 3.1 创建字段

创建接口只接受：

```text
task_type=search_click
search_execution_mode=click_only
keywords[]
approved_target_ref
account_group_id
daily_click_target_count
timezone
start_at/end_at
```

创建阶段只校验权限、字段、公开目标引用和任务结构；不要求运行期代理、验证码或账号容量证明。结构合法即允许创建，运行期事实不足显示 blocker，不伪装为创建失败。

一个 Search Task 必须且只能保存一个 `approved_target_ref`。缺失目标、目标数组或多个目标返回 `422 search_task_requires_exactly_one_target`；多目标需求由运营创建多个独立 Task，它们在 search lane 并行执行。`keywords[]` 可为同一目标提供多条已批准搜索入口，但不能指向不同目标。

`approved_target_refs[]`、`join_target_group_after_click`、`daily_admission_target_count`、`membership_target`、`search_join_group` 和 `search_join_membership` 不属于当前 schema；当前创建路由携带这些字段返回结构化 `422 current_search_schema_violation`。旧混合创建路由本身返回 `410 legacy_search_join_contract_removed`。

### 3.2 任务日与义务

每个任务日建立不可变 `task_day_ledger_id`，冻结 timezone、period、deadline、lifecycle epoch 和单一目标；建立 `daily_click_target_count` 条稳定点击义务：

```text
SearchClickObligation(id, task_id, task_day_ledger_id, approved_target_ref, state)
```

义务 UUID 是执行身份，不分配 completion/click ordinal。`remaining = target_click_count - confirmed_target_click_count`；明确 pre-accept 失败把同一义务释放为 open 并可重新 assignment，Gateway-started/unknown 保持原义务占位，confirmed 终结原义务。不得通过新建替代义务绕过 unknown。

### 3.3 新建切换与旧 Task 删除

所有旧 search/search_join Task 不迁移、不拆分、不 quarantine。运营先按当前 schema 为每个目标直接创建一个 `prepared` 新 Task；真实 prepared canary 只验证 remote fact 链，不做容量计算。activation manifest CAS route epoch 后，新 Task 从 0 运行、旧 Task同时失去 Gateway 权限；随后删除 operation 只为 Gateway-started/unknown/confirmed 远端副作用写最小防重 tombstone，再物理删除旧 Task 和全部可重建 runtime/config。同日新 Task 从 0 计算，旧成功不抵扣新目标，并要求显式确认可能重复完成同日业务目标。

## 4. 独立搜索执行架构

```text
Search Planner
  -> SearchClickObligation
  -> Continuous Search Solver（资源空闲即求解）
  -> SearchClickAssignment（原子冻结执行身份）
  -> Search Worker（立即直接执行）
  -> Telegram Gateway / @jisou
  -> 唯一 target_click_observed remote fact 先提交
  -> Attempt、Action、assignment、义务分别单行 CAS 收敛
```

以下资源与普通互动完全隔离：进程池、队列、heartbeat、active capacity、OCR worker、代理路由、熔断和指标。数据库公共 Task/Action/Attempt 表可以复用，但不得复用普通 Dispatcher 的 claim capacity；搜索不建立 Window、Reservation、预扣或任务份额。

## 5. Solver 与 assignment

### 5.1 候选事实

solver 只枚举真实存在且当前新鲜的：

```text
account authorization
keyword + approved target path
proxy subscription parse
proxy node egress
active account-proxy binding
protocol sample version
open click obligation
business deadline
```

禁止把账号、关键词、代理和授权的笛卡尔积当作容量。solver 由义务/事实变化、assignment 终态和 search worker 空闲持续唤醒；每轮先从每个 running Search Task 至多选择一条开放义务，再按 `opened_at,task_id,obligation_id` 填满实际空闲槽位，不要求一个 Task 排空后再处理下一个。

### 5.2 assignment 原子落库

assignment 必须冻结：

```text
assignment_id + obligation_id
account_id + authorization_id
keyword_id + approved_target_ref
proxy_node_id + proxy_binding_version
protocol_sample_version + solver_input_hash + assignment_version
execution_policy_version + obligation_deadline_at
```

同一 obligation 最多一个 `open|executing|gateway_unknown` assignment。assignment 与所引用事实版本在一个短事务内原子落库；事务冲突只回读现有 assignment。

assignment 行本身就是持久待执行工作。search worker 先用 partial index 有界读取候选 ID，再逐条执行带 `state + version + owner fencing` 条件的单行 `UPDATE ... RETURNING` CAS 领取，不使用 `FOR UPDATE/SKIP LOCKED` 或跨 assignment 锁；进程内通知/队列只缩短延迟。通知丢失、worker 重启或进程崩溃后，其他 worker 使用数据库时间和旧 owner/version CAS 接管同一 assignment，不建立替代 assignment。

### 5.3 直接执行，不预扣

- assignment 落库后立即进入独立 search worker，执行权持续到 `obligation_deadline_at`。
- 只按真实空闲 search worker 槽位建立 assignment；不计算 Window、速率、静默权重、预扣、TaskAllocation/Reservation 或二次 capacity confirm。
- Gateway 前若账号资格、binding 或 policy version 已失效且尚未远端开始，终结原 assignment，义务保持 open 进入后续求解；禁止在原 assignment 上换绑。
- 已进入 Gateway 或结果 unknown 时保留原 identity 并对账，禁止重分配。

## 6. 账号与失败语义

solver 在每轮 eligible snapshot 中对账号做持久随机排序，保存 `selection_seed + candidate_snapshot_hash + random_order`；同一轮重试复用顺序，避免每次重新随机造成饥饿或重复。

- Telegram/Session 权威确认 `session_invalid|session_revoked|session_unauthorized|need_relogin|cannot_send|write_forbidden|account_restricted|account_banned`：立即在该 Search Task 日放弃该账号并从本轮候选池删除，同日不自动复活；不冻结全局账号。其他 Task 仍独立物化状态，但若使用同一已失效授权槽位，会分别引用同一版本化权威事实并放弃；另一有效授权槽不受连带影响。目标 `peer_invalid|target_deleted|target_dissolved` 终结该 Task 目标。
- 网络 timeout、代理 reset、Telegram unknown：不把账号标记终态，不通过“失败 -1”减少点击目标；对原 assignment 收口后才能再次候选。
- 点击前明确失败：义务仍 open，后续可新建 assignment。
- 点击可能已发生：义务进入 `unknown_hold`，只做远端对账。
- 只有 `target_click_observed` 才使日剩余量减 1；失败数、候选数和尝试数只作诊断。

## 7. Transport 与代理事实链

必须分别保存：

```text
subscription_parse_fact
proxy_node_egress_fact
account_authorization_fact
account_proxy_binding_fact
gateway_transport_fact
target_click_observed
```

每层包含 `observed_at/fact_version/stale_after/policy_version`。16/16 节点 egress 成功只证明节点出口，不证明账号 active binding、Telegram Gateway 成功或目标点击。

- assignment 固化代理节点和 binding version，Gateway 前复核。
- binding 失效且未进 Gateway 时释放原 assignment；不得静默直连或换代理继续。
- timeout、reset、代理鉴权失败分码记录，不污染其他节点。
- Gateway unknown 保留原 click 义务的 `unknown_hold`，不换路重发。

## 8. 极搜会话与结果匹配

每个 assignment 唯一建立持久 `SearchProtocolSession`：

```text
assignment_created -> keyword_sent -> hot_list_page -> group_category
  -> verification_required -> result_page -> target_found
  -> click_started -> click_unknown | completed | failed
```

session 必须保存 `phase_version/request_identity/viewer_cursor/page_fingerprint/keyword_id/approved_target_ref/next_page_identity/challenge_fingerprint/protocol_sample_version/owner fencing`。每次远端发送或页面转换后先 CAS 并提交当前 phase，再进入下一步；重启只从已提交 phase 继续。`keyword_sent` 或 `click_started` 后结果 unknown 时保留原 request，不重发关键词、不重复点击。

1. 发送冻结关键词，保存 request identity、会话 cursor 和 protocol sample version。
2. 只接受当前 request 之后的可信 `@jisou` 回复；旧缓存消息不得匹配。
3. `hot_list_page` 只点击当前版本化 `BotProtocolSample` 为该 page fingerprint 明确批准的“群聊/群组” selector，再进入 `group_category/result_page`；selector 缺失或页面不匹配写 typed failure，禁止 OCR/AI 猜按钮。固定 70 页只是旧兼容字段，不是结束条件。
4. 目标匹配使用标准化公开 username/批准 target ref 精确匹配。展示名只能辅助展示，不得单独确认点击目标。
5. 只有实际调用批准目标按钮，并取得同 request identity 的明确 click outcome，才写 `target_click_observed`。
6. `no_next_page`、目标未找到、按钮不存在均为当前 Attempt 失败，任务保持欠量。
7. 点击完成后立即结束该义务，不创建 membership/admission/can-send 子 Action。

## 9. 双 OCR 验证码合同

### 9.1 固定顺序

```text
new challenge fingerprint
  -> RapidOCR(A)
  -> A 无输出/非法：ddddOCR(B)
  -> A 提交被权威拒绝且 fingerprint 未变：B
  -> 远端产生新 fingerprint：新 challenge 从 A 重开
```

- 禁止 AI/VLM、模型投票、hedge、两票共识和人工猜测热路径。
- A 与 B 对同一 fingerprint 给出相同答案时，A 已被拒绝后不得重复点击同一答案。
- B 无安全答案或被拒绝后，若远端已产生新 fingerprint，从 A 重开；若 fingerprint 未变，只允许协议样本明确批准的 refresh callback。
- 没有独立 refresh 动作写 `refresh_not_supported`；`/cancel`、`/start`、重发关键词、未知按钮和刷新本地页面都不是 refresh。
- refresh callback unknown 保持原 fingerprint `unknown_hold`，不得再次点击。

### 9.2 状态与审计

每次识别保存 `fingerprint/engine/input_hash/output/validation/rejection_fact/next_state/policy_version`。OCR worker 必须有独立 deadline 和内存隔离；进程崩溃显式失败，不降级到 AI。

## 10. 远端事实、unknown 与幂等

### 10.1 唯一成功提交

`target_click_observed` 最终提交采用事实先行，不做跨表事务：

1. 以 `remote_mutation_key + gateway_request_identity + target identity` 唯一追加 click remote fact；
2. 事实提交即为点击完成真相；任何 worker 发现同 obligation/mutation 已有 confirmed/unknown fact 都不得重复点击；
3. projector 以 `fact_id + expected_version` 分别单行 CAS 终结 ExecutionAttempt、Action、SearchClickObligation 和 assignment/lease；
4. 任一投影失败只重放缺失 projection，不回滚事实、不新建 assignment、不再次点击；
5. Task stats 在事实之后异步投影，不得反向创造完成事实。

### 10.2 unknown 收口

- 明确 pre-accept rejection：可证明未点击，原义务重开。
- 明确正向 click 事实：提交成功。
- timeout、连接断开、callback unknown、页面状态证据不完整：保持 unknown，不自动重试。
- 换账号看不到、当前按钮仍存在或页面 fingerprint 未变化，都不能单独证明点击未发生。
- `deadline_at` 到达仍无权威结论时，追加 `unknown_deadline_closed`，义务转 `remote_reconcile_only`、任务日转 `closed_with_unknown_shortfall` 并释放执行槽；保留 Gateway journal/tombstone 永久防重。运营不能强制成功、清空 unknown 或换号重试；迟到权威事实只修正历史投影，不触发新点击。

## 11. API、前端与可观测性

### 11.1 API

- `POST /api/tasks/search-click`：创建单目标 click-only Task；`approved_target_ref` 必填且只能一个。
- `GET /api/tasks/{task_id}/search-fulfillment?task_day_ledger_id=...`：返回目标、confirmed、open、executing、unknown、blocked 和 deadline。
- `GET /api/search-click-assignments/{id}`：返回冻结 binding、transport facts、OCR state、Attempt 和 remote fact。
- 旧混合创建路由返回 `410 legacy_search_join_contract_removed`，不做兼容写入。

### 11.2 前端必须展示

```text
daily target / target_click_observed / remaining
open / assigned / executing / unknown_hold / blocked
subscription parsed / egress / authorization / binding / gateway / click
OCR A/B 当前 fingerprint 与安全终态
最近 blocker、next decision、deadline risk
```

不得展示“预计可确认点击数”。assignment 和代理健康只能标为运行事实，不能显示已完成。

### 11.3 指标

```text
search_obligation_open_total
search_assignment_created_total
search_assignment_to_attempt_latency
search_transport_fact_total{stage,result}
search_ocr_total{engine,result}
search_gateway_unknown_total
search_target_click_observed_total
search_deadline_shortfall
```

## 12. 权限、安全与并发

- 创建/编辑/暂停/删除继续使用 Task 权限；代理明文凭据和 Session 不进入 API、日志或事实 payload。
- 同一账号可在不同任务执行互不冲突的 Telegram RPC；不设账号全局单 in-flight 锁。
- 相同 callback/request identity、同一 assignment 和同一 click 义务由唯一键和单行 CAS 幂等收敛，不建立账号级或跨表锁。
- Telegram FloodWait、账号授权、协议版本、代理绑定和 unknown 防重仍是硬边界。
- 暂停/停止立即停止新 assignment；删除遵守异步 fencing/最小远端 tombstone/物理删除合同，已 Gateway-started/unknown 只对账不新增写入。

## 13. QA 验收

### 13.1 合同与旧 Task 新建切换/删除

- 新任务只产生 `search_click/click_only` 义务，membership/admission Action 为 0。
- 缺失目标、目标数组和多目标均返回 422；每个新 Task 的目标字段恰好一个，多个独立 Task 可同时执行。
- 运营先直接创建单目标 `prepared` 新 Task；route epoch 切换后新 Task 从 0 运行、旧 Task同时失去 Gateway 权限，再为所有 legacy search/search_join Task 写最小 tombstone并物理删除，不产生迁移、quarantine 或 shadow 数据。
- 同日新 Task 从 0 计算且要求显式确认；旧成功不抵扣新目标，旧 mutation identity 不重放。
- 旧混合路由返回 410；不存在 silent compatibility writer。

### 13.2 并发与执行

- 多个搜索任务并发求解和执行，不串行排空。
- assignment 落库后立即直接执行；资源空闲立即补下一条，不出现 Window、速率、静默权重、预扣或二次 Reservation。
- assignment 通知丢失、worker 重启、lease 过期接管后仍从数据库持久 phase 继续；`hot_list_page/group_category/result_page/verification_required/click_started` 均有 CAS，不能重复关键词或目标点击。
- binding 失效且未进 Gateway 可重分配；Gateway unknown 不换绑、不重试。
- 普通互动队列堵塞不影响搜索，搜索代理/OCR 故障不影响普通互动。
- claim/query 使用闭合专项规定的 partial index 与当前空闲槽有界 batch；生产高水位 2 倍下满足 `claim_query_p95<=100ms`、单行 CAS `p99<=250ms`、Seq Scan=0、deadlock=0。

### 13.3 OCR 与协议

- A invalid 才进 B；A rejected 且同 fingerprint 才允许 B 提交。
- 新 fingerprint 必须从 A 重开；相同答案不重复点击。
- refresh approved/unsupported/unknown 三类均覆盖；AI/VLM 调用数为 0。
- no_next_page、目标未找到、按钮不存在均不减少 remaining。

### 13.4 E4 生产验收

canary 直接使用一个真实 `prepared` 新 Task执行，不计算吞吐、容量、required rate、预计完成量或 P95/P99；只要求形成：

```text
Task
-> SearchClickObligation
-> SearchClickAssignment
-> Action
-> ExecutionAttempt
-> target_click_observed
```

完整任务日验收 `confirmed == daily_click_target_count`、`remaining=0`、单一 target identity、无重复义务完成、无 unresolved unknown、无 membership/admission 子 Action。若未达标，按 obligation/transport/OCR/protocol/Gateway/remote fact 分层报告 blocker；worker、部署、健康检查或 Action 增长不能声明 `production_fixed`。

## 14. 发布与回滚

发布顺序：部署 inactive-by-default 新 search writer/worker → 运营按当前确认配置直接创建全部单目标 `prepared` 新 Task → allowlist 一个真实新 Task直接执行并取得完整 remote fact 链 → 审批 activation manifest → CAS 唯一 route epoch，让全部 prepared 新 Task从 0运行并使全部旧 search Task失去 Gateway 权限 → 异步写最小 tombstone并物理删除旧 Task/runtime → 验证旧运行行为为 0。

canary 未取得 `target_click_observed` 时不得切 route 或删除旧 Task。canary 不是容量证明、shadow 或双写：同一 Task/mutation 只有一个 writer。route 尚未切换时可删除 prepared Task；route 一旦切换就永久禁止回到旧 search Task/旧 writer，无论新 assignment 是否已进入 Gateway。故障只允许停止受影响新 Task、前向修复和对账；不存在 legacy 迁移、兼容 writer、quarantine 或 shadow。发布仍走 `master -> release -> Deploy Production`。

## 15. 开发交接

开发必须同时更新：

1. search 创建 schema 与旧路由 410；
2. 单目标 task-day click obligation、assignment 唯一键与持久 `SearchProtocolSession` phase CAS；
3. 独立 planner/solver/worker/heartbeat/metrics；
4. transport typed facts 与 binding version 守卫；
5. RapidOCR → ddddOCR 状态机；
6. `target_click_observed` 唯一事实先行、幂等 projector 和 unknown deadline 运营终态；
7. prepared 新 Task、route activation manifest、全部旧 search Task 删除 operation、最小远端 tombstone和同日从 0 确认；
8. 自动化 QA；主 PRD/主数据流索引已同步产品合同，代码完成后按真实入口更新项目结构索引。

本文件 `product_design_complete` 只表示产品设计可交接，不表示代码、旧 Task 删除、QA、发布或生产已完成。
