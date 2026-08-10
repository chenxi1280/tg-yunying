# Findings & Decisions

## Requirements

- 识别要完成AI活群失败风暴与channel_view Planner饥饿整体修复还必须补齐的事项。
- 核对设计是否已经足以直接开发、迁移、发布与完成线上验证。
- 不用健康检查、测试或停止新增失败替代真实远端事实与自然日完成证据。

## Research Findings

- 上一轮专项PRD的Product Design Complete结论已被后续实现就绪复核推翻；current必须以AI+浏览两专项同步后的fresh independent zero-P0/P1 review为准。
- 隔离设计分支基于`9a1405aa`；2026-08-10最新已fetch`origin/master=6db995cb2cc5c94b805b6647219cbd060269a59a`，含独立material-cache role。设计分支只承载文档，开发从开工时最新master另建干净worktree。
- 主 `release` 工作树与多个并行工作树存在，必须继续隔离，不能覆盖用户改动。
- 设计完成状态不代表代码、迁移、发布或生产 E4 已开始。
- `9a1405aa..origin/master` 的唯一提交不改 AI 义务模型，但拆出了独立 `material-cache` worker，并同步 compose/check-web/worker-role 测试；这暴露出专项交接尚未明确 wake/projector/reconciler 由哪个生产 worker role 承担、如何注册 heartbeat 与部署健康检查。
- 专项已定义 typed state/CAS、迁移守恒和 E4，但开发交接当前主要是模块清单；还需核对可分阶段落地的依赖 DAG、每阶段 writer fence，以及 worker topology 是否足以避免“一次性大爆炸”切换。
- `due_at/deadline_at` 写明冻结 task-day 边界，但对象字段/迁移验收尚未显式冻结 `business_date/timezone_revision`；需对照现有 `TaskDayLedger` 判断是否已有唯一真相源，避免重复字段或跨日误收口。
- 现有 `TaskDayLedger` 已有 `timezone_snapshot/timezone_revision/obligation_local_date/period_start_at/deadline_at`，因此不应在新 obligation 重建第二套业务日；实现只需强制 FK 与 ledger 边界一致并补跨日测试。上一条疑点已排除为新模型字段缺口。
- 当前 `recovery` role 已统一执行 claim recovery、unknown deadline closure 和其他等待恢复，适合承接 DB-only wake/deadline/Gateway reconcile；`ai-generation` 适合承接 Provider generation reconcile。专项尚未明确这一 ownership、drain 顺序、heartbeat metadata 和 compose/role 回归。
- §8 只定义四层语义与失败聚合字段，没有冻结具体 API 路由、游标、排序、快照一致性或 legacy 字段兼容；若前后端并行开发，这会造成接口漂移，属于开发前应补的 P1 交接合同。
- 真实 Task Center 路由入口是 `backend/app/api/routers/task_center.py`，schema 位于 `backend/app/schemas/task_center.py`；专项交接应使用这些路径而不是泛称 task-center API。
- 现有 `FulfillmentFactProjectionState` 有 pending/failed/next_retry_at，但初步搜索只看到发送事务内 `project_remote_fact()` 与 unknown closure 写状态，尚未看到一个明确的 retry drain；新设计又要求 remote fact 后异步投影 typed obligation/target/coverage，因此 projector consumer ownership 是必须闭合的候选 P0。
- 物理删除不能把所有新表一概加入删除顺序：obligation/allocation/intent/variation/wake/claim/route/link 等可重建 task-day runtime 必须显式逆 FK 删除；FleetPolicy、immutable inventory membership、retired enrollment、ContractTombstone、global activation manifest 与 remote fact 必须保留且不级联，否则 legacy 永久 fence、fleet hash/count 和远端守恒审计会断裂。
- 共享 venv 验证当前 Alembic head 仍是 `0144_avatar_material_sources`；实际实现仍须在合并前重新取 head，且把 schema DDL 与生产规模 takeover 数据写分开。
- 当前已有 `GET /api/tasks/{task_id}/daily-fulfillment`（date+page）和 `GET /api/tasks/{task_id}/actions`（page/sort/filter），`TaskDetailOut` 已很重。新四层读模型应复用 daily-fulfillment 摘要，但将 obligations/waits/attempt history 作为独立分页端点，不能继续把全量行塞入 Task detail。
- 前端 `DailyFulfillment` 仍是旧 coverage 口径，`TaskCenterStats`/多个字段仍使用 `Record<string, any>`；专项明确“不透传 any”，因此 API schema 与 TS 类型需要先冻结再并行开发。
- 现有 generic `retry_task()` 对 group AI 的 failed/unknown Action 会直接清空 result、重置 retry_count 并改回 pending；`reset_task()` 会清空 stats/未完成 plan、设置 force bootstrap；`stop_task()` 只把 pending Action 标 skipped。若新 route 激活后不加 task-type-specific contract guard，这些入口会绕过 stable obligation、Gateway unknown 和 variation 预算，属于 P0。
- 专项只禁止新增 UI 的“全部重试/清空 unknown/重置任务”，尚未冻结既有 `PATCH/start/pause/resume/stop/retry/reset/delete` 的 typed obligation 语义。pause/stop 还会推进 `task_lifecycle_epoch`，必须与 route epoch、FOP/obligation/Action 的 epoch 和 pre-Gateway 收口原子协调。
- 需要新增控制面合同：generic retry/reset 对 active `ai_message_obligation_v1` 返回 409；合法恢复只能按 obligation blocker/wake 或受审计的 contract-error CAS；pause/resume/stop/delete/config update 各自定义 pre-Gateway、Gateway hold、route 和任务日 ledger 行为。
- 仓库已有 task-level `TaskContractActivationManifest/TaskContractRoute` 与 `gateway_task_allowed()`；专项又设计 task-day `AiGroupMessageContractRoute`，但未规定两者的组合关系。必须明确前者是 release-train/task-set fence，后者是 AI message task-day writer fence，Planner/Generation/Dispatcher 必须同时通过，不能成为两个可择一的真相源。
- 现有 `TaskContractRoute.expected_lifecycle_epoch` 只在 activation 投影 Task 状态时校验，`gateway_task_allowed()` 当前没有校验 task epoch。新 task-day route 不能假设 generic fence 已覆盖生命周期；需把统一 `require_ai_group_message_route()` 作为 Planner、Generation、Dispatcher/Gateway 和 recovery 的共同入口并测试 epoch 漂移。
- active task 的配置变更也缺少 typed 语义：timezone 只能影响下一 TaskDayLedger；数量目标走 planned target revision；账号范围走动态 coverage；content/prompt/policy 变化使 pre-Gateway intent/variation 失效并发 wake，Gateway hold 不改写。不能继续只清 unfinished plan。
- `TaskGroupDailyTarget` 当前没有 `target_operation_target_id` 或 row `version`，虽然 obligation identity 和 ordinal allocator 都按 target operation scope 设计；开发必须 additive 增加 target FK、`next_quantity_ordinal/version`，对新 route 建 `(task_day_ledger_id,target_operation_target_id)` 唯一，旧 tenant/task/group/date unique 仅保 legacy 兼容。
- 当前 daily target 的 due/confirmed 窗口仍用 `target_date` naive midnight 与 Action/memory 查询；新合同必须改成读取 `TaskDayLedger.period_start_at/deadline_at/timezone_snapshot` 和 typed remote facts，否则多时区/跨日会在正确义务模型下继续算错数量。
- `task_day_ledger_id` 当前在 TaskGroupDailyTarget 可空；迁移期可以保留 nullable，但 `ai_message_obligation_v1` route 的新建/接管必须要求非空 ledger+target FK，缺失即 contract_migration_blocked。
- aggregate content plan 的 `scope_total_units` 还没有公式。为保持既有 Cycle 产品语义，应冻结为 `min(resolved_messages_per_round_for_current_mode, remaining_planned_target_units)`；`reply_min_required=min(reply_min_per_round,scope_total_units)`，技术批次最多 20 只消费 assignment，不重算 Cycle。
- 主 PRD 当前执行器段仍写“任务日不生成 due-by-now”，与专项的 `natural_full_day due_by_now` 直接冲突。即使顶部 supersede 已声明，开发容易误读；应原位更正或显式标成 historical_do_not_implement。
- `messages_per_round=1` 的现有特殊语义会退化成 desired participant count；新 aggregate plan 不能照字面把 scope_total 固定为 1，必须复用现有 resolved turn-count 函数的结果，而不是原始配置值。
- generation epoch 公式引用 `external_message_memory_revision` 和 Provider config/health epoch，但当前模型搜索未发现这两个稳定单调 revision；现有 Provider 主要是 mutable `health_status`，message memory 也无 scope revision。必须由 durable wake clock 提供对应 scope revision，并在 memory reservation/status、Provider config/key/health 写事务同步递增，禁止用 `updated_at/max(id)` 猜版本。
- `group_context` 可由 listener watermark/context snapshot 派生，Task 有 `config_revision`、profile 有 lineage/version；其余 basis 每个都要列出唯一 owner/写入点，否则“basis 未变不重置 3+3”无法验证。
- 新流程横跨 target/allocation/obligation/FOP/coverage/claim/job/variation/memory/Action/Attempt/fact 多行 CAS，PRD 只说“同一事务”但没规定规范更新顺序。应冻结 canonical transaction order，并让并发 PostgreSQL 测试覆盖 Planner vs wake、Generation vs config update、Dispatcher vs pause/stop、fact projector vs deadline。
- §10.3 要求 takeover manifest 守恒后才能激活，但 §11 当前顺序是先 route active、再 takeover apply/readback，存在新 writer 在未接管缺口上发送的窗口。正确顺序必须是 task-local quiescence + route preparing/fenced → 归类在途 → manifest apply/readback 守恒 → 最终 active CAS → resume。
- `gateway_prepared` 仍有“网络调用已经发出、结果 journal 尚未提交即崩溃”的窗口。必须在任何外部调用前提交同 request identity 的 durable `call_issued/ambiguous` 证据并把义务置 unknown hold；prepared 且无 call-issued 才能证明未调用，call-issued 后即使实际调用前崩溃也保守 hold，只有同 request typed safely-not-executed 可释放。
- 现有 remote fact unique 是 `(remote_mutation_key_hash,gateway_request_hash,fact_kind)`；canonical fact必须继续按request/mutation identity append，不能用obligation partial unique拒绝第二个真实副作用。另建 `AiGroupMessageQuantityFactBinding`，只对 `bound_obligation_id WHERE state=bound` partial unique；第二fact保留为typed unbound conflict。
- 当前发布基线持续漂移；独立 reviewer 观察到设计分支已落后 origin/master 2 个提交，其中新增提交触达 Gateway/worker/deploy。dev 不得直接在该设计分支实现，必须从合并时最新 master 建干净工作树重新 resync 路径与 migration head。
- 再核对时 origin/master 已前进到 `5bea2555`，设计分支落后 3 个提交。新增提交本身分别处理 material-cache Telethon 生命周期与账号资料 readback，不改变 AI send 语义，但确实修改共享 `gateway.py`、worker/compose/ops 文档；实现时必须以最新 master 合并，不能从本设计 worktree 直接编码。
- `AiProvider/TenantAiSetting` 当前无 config/health revision；人工 provider 更新、健康检查与生成时 quota-exhausted 都是不同写入口。wake clock producer 必须覆盖这三处，并以 scope/version 去重，不能只改一个 API 写点。
- group context 插入和 voice-profile activation 都有明确同事务写点，可在这些服务内推进 wake clock；这证明 durable event 方案可实现，但必须形成 producer inventory 和漏接测试。
- 独立 reviewer 最终判定 `blocked`，确认 9 个 P0：管理命令旁路、三层 route/epoch、daily-target 身份与 due、projection drain、Gateway call-issued、takeover 顺序与分类、wake ownership、全局锁序与成功事实唯一；另有 assignment/API/Task 状态/provider reconcile/Release Gate 等 P1。
- `contract_migration_blocked` 与 `paused_contract_incompatible` 不应伪造为当前不存在的 `Task.status`；保持 `Task.status=paused`，原因写入 task-day route/runtime blocker，避免全栈枚举漂移。
- 历史 generated-ready 但没有合法 variation、request identity 或 current intent 的 Action 不能“原位绑定后继续发”；只保留为 terminal legacy audit，由同一 stable obligation 依 current contract 重新物化。
- 主 PRD 的 current 热领取合同明确禁止 `FOR UPDATE/SKIP LOCKED` 和跨表显式锁；wake/projector 必须复用 partial-index keyset + 逐行 version/lease CAS，专项已据此改写，规范“变更顺序”而非另造锁体系。
- call-issued 提交与实际外调之间若发生 pause/stop，不能让 recovery 重放，也不能假装没发；Tx B 冻结不可接管 invocation owner，原 owner只允许一次 `invoke_committed`，其余路径保守 hold/reconcile。
- preparing apply期间可能收到既有hold的远端结果；A类identity/conservation只允许manifest内同request的守恒delta，其他漂移blocked；B类liveness/dedupe允许推进但逐行重评估，不重排ordinal。
- TaskGroupDailyTarget 现有 `uq_task_group_daily_target` 是全表唯一；current 合同必须先 drop，再用 legacy `target_operation_target_id IS NULL`（有意兼容 ledger 已填的旧行）与 current `ledger+target both nonnull` 两个 partial unique 替代，且 takeover 新建 current row而不改 legacy row。
- pause/stop 的 command-id 幂等不足以防 direct/generic API 用新 id重复推进生命周期；正确门禁是 Task 状态 CAS 幂等，只有真正非paused→paused或非stopped→stopped的 winner推进epoch并创建 Adoption。
- 历史刷新值`origin/master=4bfdb946`已过期；当前已知值为`6db995cb`，仍须在dev开工时重新fetch并resync共享Gateway/worker/deploy/dataflow，不能在本设计分支编码。
- 生产channel_view首断点已闭合定位：due约1370/190但只物化31/25个future Action，最晚23:57，0 Attempt/0 ViewRemoteFact；future-tail整体平移越deadline，Task级180秒间隔又把理论上限压到约480/日。
- 浏览来源与容量是独立边界：一个Task fresh source=0却无typed状态；约869/871 lifetime identity小于单消息1000，必须物化可达集合并报告structural shortfall。E4不得以obligation行数充当required。

## Technical Decisions

| Decision | Rationale |
| --- | --- |
| 对最新一条 master 提交做路径级差异审查 | 防止旧基线上的设计交接漏掉新模型/迁移冲突 |
| 独立 reviewer 从“可实现与可上线”角度挑战 | 避免只复核产品语义而遗漏依赖顺序和运维入口 |
| 把新后台循环的部署 ownership 当成上线硬门 | durable wake/reconcile 只有模块没有常驻 consumer 会形成永久 waiting |
| 复用 `TaskDayLedger` 的时区/业务日真相源 | 避免新 obligation 复制 timezone/date 后发生跨日漂移 |
| typed remote fact projector 必须有 durable consumer | 只在 Dispatcher 内同步投影会把“事实已落库但业务读模型未完成”变成永久不一致 |
| 四层 UI 采用摘要加独立分页资源 | 避免继续放大 TaskDetailOut，并允许 active/wait/history 各自排序和过滤 |
| 把所有既有 Task lifecycle API 纳入 route fence | 防止 UI 隐藏了按钮但直接 API 仍可破坏义务和 unknown 防重 |
| generic TaskContractRoute 与 AI task-day route 采用 AND 组合 | 前者控制发布任务集，后者控制任务日 writer，任何一层失效都必须 fail-closed |
| 新 daily-target 读写只认 TaskDayLedger 边界和 typed remote fact | 避免继续用 Action executed_at/naive midnight 形成第二套数量真相源 |
| aggregate plan 使用 resolved logical turn count | 保留 messages_per_round 的 Cycle 语义，同时与最多 20 条技术批次解耦 |
| 所有 generation basis 都绑定数据库单调 revision owner | 防止时间戳或 Action ID 伪装成事实变化并重置生成预算 |
| 冻结跨表 transition 顺序 | 让 CAS 冲突显式收敛，避免新状态机引入 PostgreSQL 死锁环 |
| route 只有 manifest 守恒 readback 后才能 active | 消除接管中途新 writer 双写或超发窗口 |
| Gateway 外调前持久化 call-issued ambiguous hold | 关闭“外调已发出但 journal 未落库”的双发窗口 |
| canonical fact identity unique + quantity binding bound-obligation partial unique | 既保证每义务最多一个确认绑定，又不丢同义务第二个真实远端事实；冲突以typed unbound overage保留 |
| dev 从最新 master 新建干净工作树 resync | 设计分支只承载文档，已与共享 Gateway/worker/ops 文件产生漂移 |
| migration/incompatible 原因存 route/runtime，Task 仍用现有 paused | 避免为了内部合同阻断扩充公共 Task 状态并引发 API/UI 兼容成本 |
| legacy Action/Job永不原位变成current owner | 旧行只作immutable alias；合法内容新建完整current fence的Action或同request只查询reconcile wrapper，避免改历史或双发 |
| wake/projector 使用现有 keyset+单行 CAS 模式 | 与主 PRD current 并发合同一致，避免恢复退役 SKIP LOCKED/跨表锁链 |
| Tx B 后只由原 invocation owner执行一次已 committed call | 同时满足调用前 durable ambiguous hold 与 pause/崩溃后不重放 |
| Fleet inventory/enrollment 使用逻辑 task ID 留存，Task FK 仅作 nullable navigation | Task物理删除后仍能证明永久 legacy fence、原 membership/state hash 与最终守恒 |
| lifecycle pause/stop 按 Task 状态而非仅 command ID 幂等 | 关闭不同 API/不同 request ID 重复推进 epoch、制造新 adoption 的并发洞 |
| daily target 使用两套精确 partial unique且takeover新建current row | 兼容现存半迁移legacy行，同时允许同local date多ledger并保证旧行hash不变 |

## Issues Encountered

| Issue | Resolution |
| --- | --- |

## Resources

- `docs/03-feature-designs/ai-group-generation-failure-churn-remediation-prd.md`
- `docs/01-product/tg-ops-platform-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`
- `.planning/2026-08-09-ai-group-generation-failure-churn-design/`
- `docs/03-feature-designs/channel-view-planner-starvation-remediation-prd.md`
