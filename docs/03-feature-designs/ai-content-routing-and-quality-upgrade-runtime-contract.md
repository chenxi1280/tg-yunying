# AI 内容升级运行、Provider 与数据合同

> 规范性附录。与 `ai-content-routing-and-quality-upgrade-prd.md` v1.2 共同生效；冲突时以主 PRD 的业务目标和本附录更具体的运行合同为准。

## 5. 多 Provider purpose route

### 5.1 目标

取代“全系统只能一个 active Provider”的隐式选择，支持同租户按用途同时使用多个 Provider：

- MiMo 可用于 context route 或特定 Realizer；
- DeepSeek/MiniMax 可作为独立在线 reviewer 或离线 evaluator；
- 每个 purpose 有显式顺序、模型、启停和 revision；
- 禁用某条业务 route 不等于删除 Provider 凭证。

### 5.2 Purpose 枚举

| purpose | 是否在线发送链 | 说明 |
| --- | --- | --- |
| `group_context_route` | 是 | route/reason/evidence 结构化分类 |
| `group_realize_general` | 是 | general Realizer |
| `group_realize_adult_visual` | 是 | 成人视觉 Realizer |
| `group_realize_adult_product` | 是 | 成人用品 Realizer |
| `group_realize_adult_service_inquiry` | 是 | 成人服务询问 |
| `group_realize_adult_service_sensory` | 是 | 成人服务感官短句 |
| `group_semantic_review` | 是 | 在线放行 reviewer |
| `comment_context_route`、`comment_realize_general`、`comment_semantic_review` | Phase 2 | 仅在 Phase 2 purpose enum、样本与 reply authority 通过审批后才可创建；不接受 wildcard |
| `offline_pairwise_eval` | 否 | shadow/离线 A/B，不参与发送 |

### 5.3 路由选择合同

路由配置采用 header/items，不原地修改 active revision：

- `TenantAiProviderRouteSet`：`tenant_id`、`purpose`、递增 `revision`、`status=draft|approved|active|retired`、content hash、`approved_by/approved_at`；同一 tenant+purpose 只有一个 active set。
- `TenantAiProviderRouteItem`：`route_set_id`、`priority`、`provider_id`、`model`、`enabled`、timeout、rate/concurrency policy；`(route_set_id, priority)` 唯一。

GenerationJob 冻结 `route_set_id/revision/hash`。任何 reorder、模型或策略修改都创建新 draft revision，审批并 CAS 激活；回滚只重新绑定上一不可变 revision。

v2 task 只能读取 frozen route set；旧 `ai_model/ai_semantic_reviewer_model` 和 tenant `model/static fallback` flags 仅服务尚未迁移的 legacy task。一个 obligation 只能由一种 selection contract 接管，禁止 route-set 失败后回到 legacy 模型或固定“签到”补量。

选择顺序：

1. 读取 GenerationJob 已冻结的 route-set revision/hash；
2. 按 priority 找第一条 enabled 且 health/admission 可用的绑定；
3. Provider 调用在数据库事务外执行；
4. 每次 attempt 追加 purpose/provider/model/priority/outcome/duration/tokens/error code；
5. 仅 typed transport unavailable 可尝试下一 priority；
6. 有效响应但 schema/质量失败保持原 Provider 和内容失败语义，不切换下一 Provider；
7. 所有 route 不可用时回到持久状态 `pending`，写 `generation_stage=waiting_provider` 与 `next_retry_at`；超过冻结 attempt/latency budget 或安全发送时间后，只为当前 quantity owner 结算 typed shortfall，禁止 mock、静态句或生成模型自审。

可切换的 transport code 包括经过 Provider adapter 归一的 timeout、connection error、429/rate limit 和明确 5xx；敏感拒绝、schema invalid、route mismatch、低质或 reviewer fail 不属于 transport fallback。

### 5.4 429 与准入

- 准入采用层级桶：先预留 credential identity 的全局 rate/concurrency bucket，再预留 tenant+purpose child bucket；两层都通过才能调用。现有 `provider_admission_key` 的凭证级 identity 语义保持不变。
- 429 写 `provider_rate_limited`、`retry_after/next_retry_at`，释放 worker，不 sleep 占槽。
- Provider 返回 429 时更新 credential 全局 cooldown 和本次 route attempt；tenant+purpose 配额只更新 child bucket。任一桶状态不确定都 fail closed，不绕到无额度 route。
- 429 不消耗内容重生成预算；同一 retry 保持 request hash 和 frozen route-set revision。
- reviewer 429 时 candidate 不得直接 ready；可按 typed transport contract 尝试同一 frozen route set 的 next priority，否则回 `pending+generation_stage=quality_wait`。
- 连续 429、timeout 和 malformed 必须分 Provider/purpose 监控，不能归为通用“AI 不可用”。

### 5.5 迁移兼容

迁移必须按以下单向顺序执行，禁止在 single-active 约束仍存在时宣称多 route canary：

1. **A / additive**：新增 `credential_enabled`、route-set/items 与 attempt 表；按显式凭证可用性回填，但保持 `is_active` 和 single-active 约束不变。当前 active/default Provider 回填为各 purpose 的 legacy route set，业务仍只读旧 selector。
2. **B / shadow selection**：只读 shadow 可解析新 route set；历史 disabled 凭证仅在具备显式 shadow 授权时可做不发送的 health/generation 测试，不得进入生产发送链。
3. **C / legacy selector cutover**：将所有 legacy 生成、配置 API、health UI 和后台查询从 `is_active=true` 迁到“tenant default provider ID + credential_enabled”；完成调用点清单、进程 capability 和有效配置 readback。`is_active` 变成只读兼容镜像，不再承担选择语义。
4. **D / maintenance migration**：只有 C 的所有读写者完成切换并排空旧进程后，才移除 `uq_ai_provider_single_active`；先保持每个 purpose 只有回填的一条 route，证明移除约束没有改变任何业务选择，再允许 preview 多 item。
5. **E / one-task canary**：只对一个显式 task flag 启用 route-set read path，冻结 revision/hash，并完成独立读回；旧配置仍可回滚。
6. **F / expand**：每批按 Release Gate 扩容；新任务保存后才以 approved active route-set revision 为权威。

迁移不得自动启用历史 disabled 凭证；任何 credential enable、route-set approve/activate 都需要独立权限、actor/reference、old/new hash 与审计记录。

---

## 6. 数据模型、版本与幂等

### 6.1 新增/扩展模型

| 模型 | 关键字段 | 约束 |
| --- | --- | --- |
| `AiContentPolicyVersion` | tenant、version、route rules、prompt registry、gate config、example-set、policy hash、status、approved_by/at | approved 后不可变；新版本替代，不原地修改 |
| `AdultSubjectAttestation` | tenant、scope type/id、subject class、evidence codes、actor/permission snapshot、attested/expires、task/policy revision、status/hash | 不存姓名/原文；有效性由权限、期限和 scope/revision 共同决定 |
| `TaskAiContentPolicyBinding` | task/lifecycle/config revision、policy id、allowed routes、attestation ids、evidence hash、style overlay id、approved_by | task revision 唯一；成人 route 必须引用匹配的有效证明 |
| `AiContentWindowPlan` | 完整 window scope、pacing/policy hash、period/window、state/version、plan hash | 完整 scope 唯一；plan 冻结成员，不是数量/due/account owner |
| `ContextScopeRevision` | tenant、scope type/id、monotonic revision、last human message/reply change、updated at | 真人上下文或 reply authority 变化在同一事务递增并发布 wake；Action payload 不是权威 |
| `AiContentWindowPlanSlot` | plan/ordinal/revision、obligation、account/due、context revision/hash、route/mode、state/version、claimed job、lease epoch/expiry | current predicate 覆盖 frozen/claimed/candidate_ready/gateway_bound；pre-Gateway 可失效替换 |
| `TenantAiProviderRouteSet` / `Item` | tenant/purpose/revision/status/hash/approval；provider/model/priority/enabled/policy | 每 purpose 一个 active set；item priority 唯一；active revision 不原地修改 |
| `GenerationJob` 扩展 | binding hash、window slot id/hash、route、mode、route evidence hash、prompt/example/voice/route-set revision、request hash、generation stage/version、next retry | candidate ready 前全部冻结；不扩张 durable state enum |
| `AiProviderAttempt` | GenerationJob、purpose、route-set revision、provider/model/priority、attempt index、request hash、typed outcome、latency/token/cost | 独立 append-only owner；不复用 campaign `AiUsageLedger`，不保存完整 Prompt/上下文 |
| `FulfillmentShortfallFact` | quantity owner、task/lifecycle/period、kind、reason、requested/settled quantity、evidence hash、settled at | 每 owner+period 最多一个最终 shortfall；kind 是原因而非第二事实，append-only |

### 6.2 状态机

为兼容现有 partial unique open-job index、claim 和 reclaim 查询，本项目不新增 durable `GenerationJob.state`：

```text
pending -> generating -> ready | failed | unknown | cancelled
```

`pending/generating/unknown` 的 open predicate 必须与现有实现保持完全一致；migration、claim、reclaim 与 comment generation job 的 predicate 必须做 schema 级回归。内部进度新增 `generation_stage=routing|planning|realizing|reviewing|waiting_provider|quality_wait`、`stage_version` 与 `next_retry_at`：

- claim 后 durable state 为 `generating`，lease 结束前记录当前 stage；
- Provider/reviewer 暂不可用且仍可重试时，释放 lease并 CAS 回 `pending`，保留 `generation_stage` 和 `next_retry_at`；
- 超过 `latest_safe_send_at` 或内容预算耗尽时终结 `failed`，写 typed `quality_shortfall`；
- `unknown` 只表示 GenerationJob 持久化/owner 归属不确定，仍属于 open predicate；reconcile 证明可重试后才 CAS 回 `pending`，证明已终结后才转 `failed|cancelled`。它不复用为 Gateway `unknown_after_send`；后者只属于 Action/ExecutionAttempt reconcile。

`AiContentWindowPlan` 为 `draft -> frozen -> settled|invalidated`；slot 为 `frozen -> claimed -> candidate_ready -> gateway_bound -> settled`。`claimed|candidate_ready` 可在 Gateway 前转 `invalidated` 并建立下一 slot revision；`gateway_bound` 只能随 Action/Attempt reconcile。lease 超时按相同 revision/epoch reclaim，不能同时产生第二个 candidate。

### 6.3 Invalidation

以下变化使未进 Gateway candidate 失效：

- Task lifecycle/config revision；
- target/group/reply scope；
- assignment/intent revision；
- context snapshot version/hash（只失效对应未进 Gateway slot/job）；
- content policy/binding；
- window plan；
- route/mode/evidence；
- voice profile snapshot；
- prompt/example set；
- Provider route-set revision。

失效流程必须：CAS 将 `claimed|candidate_ready` slot 置 invalidated、终结旧 job、释放该 job 的 memory reservation、递增 intent/slot revision 并创建新 current job。旧 candidate/audit 保留；已 `gateway_bound` 的 slot 排除 replacement。

### 6.4 并发与事务

- `(obligation_type, obligation_id)` 继续只允许一个 open GenerationJob。
- 同一完整 window scope 只允许一个 frozen plan；同一 obligation 只允许一个 current slot，partial unique predicate 必须包含 `frozen|claimed|candidate_ready|gateway_bound`，冲突方读回 winner。
- settled quantity owner、Gateway unknown 或 typed remote fact 永久占用 obligation identity；即使 slot 已 settled 不再属于 current predicate，也不得重新进入 window plan。
- DB 事务只做 snapshot/CAS/persist；任何 Provider 或 Telegram 调用都在事务外。
- worker claim 只读取 `GenerationJob.generation_not_before_at <= now < latest_safe_send_at`；reclaim 必须校验 slot/job lease epoch、job version、intent revision、context revision 和 request hash。
- Provider response 迟到时若 revision 已变化，写 stale attempt，不得覆盖 current candidate。
- message memory reservation 精确幂等键为 `(generation_job_id, candidate_hash)`，不建立全局 candidate-hash 唯一约束；重复判定继续沿用现有 tenant/account/group/window scope。失败/unknown 候选按现有保护窗口参与防重。

### 6.5 质量与数量结算

- candidate、Provider attempt、quality wait 都不算完成；数量只认 quantity owner 对应 typed remote fact。
- current v2 永久忽略 legacy static fallback，禁止 Stage 1、emoji、随机短句或固定“签到”创建 ready Action。
- 同批 slot 独立推进；一个 slot 失败不回滚已 ready 的其他 slot，也不得用其他 slot 的成功冲抵该 owner。
- deadline 前允许使用预先通过容量准入的 replacement headroom；deadline 后按 owner CAS 写一个最终 shortfall fact，其 kind 只能是 `quality|provider_capacity|context_stale` 之一。
- `Task.stats` 只投影 shortfall fact，不得作为结算真相源或重复累加缺口。

### 6.6 数据保留与隐私

- 日志、指标和普通详情只保存 hash、枚举、版本、长度、token、latency 和错误码。
- 完整上下文、Prompt、账号面具正文和成人候选不进入普通日志。
- 普通 eval artifact 不保存原始真人消息；只保存脱敏后的 context/candidate、必要 feature、source reference hash 与 retention deadline。
- 脱敏评测样本默认保留 30 天；人工标签、聚合分数和不可逆 feature/hash 保留 180 天。到期由可读回的删除任务清除，删除失败必须告警；法务保留必须有独立 reason、审批、期限和审计，不能靠关闭删除任务实现。
- 为人工复核临时读取受限原文时，原文必须加密、tenant scoped、逐次权限校验并写 AuditLog，默认不进入 eval artifact；质量样本导出默认禁用，授权导出也必须脱敏、限时并审计。
- 成人候选正文不得进入普通应用日志、错误栈、指标 label 或 Provider attempt detail。
- 人工批准 example set 必须脱敏、版本化、可审计和可回滚。

---

## 7. API、前端与权限

### 7.1 任务创建/编辑

AI 活群高级设置增加：

- “上下文路由 v2”任务级开关，默认 off；
- 当前 content policy version；
- `allowed_content_routes` 多选；
- 成人方向证据预览、成年对象声明与显式确认；
- legacy `system_prompt_override/slang_prompt_template_id` 的配置值、实际生效 `style_overlay` 与不兼容原因；
- Provider route group/read-only purpose 摘要；
- generation/reviewer canonical identity 校验；
- shadow preview，不发送。

保存前必须展示：

```text
general: enabled
adult_visual: disabled
adult_product: disabled
adult_service: enabled (explicit_adult_service_topic)
weak-only terms ignored: 老师 / 妹子
```

若成人 route/成年对象未确认、Provider purpose 缺失、generator/reviewer 相同、自由 Prompt 越权或 policy revision 过期，保存失败并返回明确字段错误。

### 7.2 API 合同、CAS 与审计

所有 API 都从认证上下文取得 `tenant_id/actor_user_id`，禁止客户端指定其他 tenant。修改请求必须带 `request_id`、`idempotency_key`、`audit_reference` 和下表 expected revision；响应回传生效 revision/hash。相同 idempotency key + 相同 request hash 返回原结果，不同 request hash 返回 `409 IDEMPOTENCY_KEY_REUSED`。

| API | 请求核心字段 | 成功响应 | 权限 |
| --- | --- | --- | --- |
| `GET /api/ai-content-policies` | status/version filter | 脱敏 policy 摘要、version/hash/status | `ai_content_policy.view` |
| `POST /api/ai-content-policies` | base version、route/gate/prompt/example/style/context config、expected latest revision | immutable draft version/hash 与 validation report | `ai_content_policy.manage` |
| `POST /api/ai-content-policies/{version}/approve` | expected status/hash、audit reference | immutable approved version/hash | `ai_content_policy.approve` |
| `POST /api/tasks/{task_id}/ai-content-policy/preview` | expected task config revision、policy version、allowed routes、attestation ids、style overlay id | derived route suggestions、弱词忽略项、effective context/style、validation errors、preview hash | `ai_content_policy.view` + task edit |
| `POST /api/tasks/{task_id}/adult-subject-attestations` | expected task config revision、scope type/id、subject class、evidence codes、expires at、explicit confirmation | attestation id/status/hash/expiry；不回传或保存原始消息 | `ai_adult_subject_attestation.manage` + scope edit |
| `POST /api/tasks/{task_id}/adult-subject-attestations/{id}/revoke` | expected attestation status/hash、reason | revoked status/hash 与受影响 binding preview | `ai_adult_subject_attestation.manage` + scope edit |
| `PUT /api/tasks/{task_id}/ai-content-policy-binding` | preview hash、expected task config revision、expected binding version、policy version、allowed routes、attestation ids、style overlay id | binding id/version/hash 与完整 effective config readback | `ai_content_policy.manage` + task edit |
| `GET /api/ai-provider-route-sets?purpose=` | purpose/status | route-set revision/hash、items、active pointer、health 摘要 | `ai_provider_routes.view` |
| `POST /api/ai-provider-route-sets/preview` | purpose、base revision、draft items | canonical identities、quota/health validation、diff、draft hash | `ai_provider_routes.manage` |
| `POST /api/ai-provider-route-sets/apply` | purpose、draft hash/items、expected active revision、approval reference | 新 active route-set revision/hash、previous revision、readback | `ai_provider_routes.approve` |
| `POST /api/tasks/{task_id}/ai-content-shadow-preview` | binding/route-set revision、脱敏 sample refs 或受控 live scope、sample limit | routes/gates/redacted candidates 与所有 hash/revision；`sent=false` | `ai_content_quality_review` + task view |
| `GET /api/tasks/{task_id}/ai-content-quality-summary` | period/policy revision | 带分母、样本量和 `insufficient_data` 的聚合指标 | `ai_content_quality_review` |
| `GET /api/ai-content-quality-samples/{sample_id}` | reason、request id、raw=false | 脱敏 sample；`raw=true` 仅临时读取受限原文且逐次审计 | `ai_content_quality_review`；raw 另需 `ai_content_quality_raw_review` |

shadow preview 只能走独立只读生成路径：不得写 `GenerationJob/WindowPlan/Action/ExecutionAttempt/message memory`，不得调用 Telegram，不得修改 route health/cooldown 或业务 usage ledger；只允许写隔离、脱敏、带 retention 的 shadow-eval attempt。响应必须显式标记 `production_effect=false` 和未证明项。

统一错误合同：`400 INVALID_REQUEST`、`403 PERMISSION_DENIED`、`404 TENANT_SCOPED_NOT_FOUND`、`409 STALE_REVISION|STALE_PREVIEW|IDEMPOTENCY_KEY_REUSED`、`422 POLICY_VALIDATION_FAILED|ATTESTATION_INVALID|ROUTE_SET_INVALID`、`429 PREVIEW_RATE_LIMITED`。`409/422` 必须返回字段级 reason 和 current revision/hash，但不得回传 secret 或受限正文。

每次 mutation 在同一事务写 AuditLog：actor/permission snapshot、tenant、resource、old/new hash、expected/actual revision、request/idempotency id、reference、outcome；apply 后由独立 GET/readback 验证 active pointer 和 hash。

### 7.3 Provider 设置

新增 purpose route 页面：

- 按租户和 purpose 展示 Provider、模型、priority、health、最近 429/timeout、route-set revision；
- 支持 preview reorder，apply 时带 old revision/CAS；
- health check 只证明 Provider 可调用，不证明内容质量；
- offline evaluator route 与 production route 分开展示；
- 禁止一个“设为默认”按钮隐式停用所有其他 Provider。

### 7.4 任务详情与诊断

展示聚合字段：

- route/mode counts 与 adult route share；
- policy/window/prompt/example/voice/provider revision；
- generation/reviewer attempt、token、latency；
- gate rejection counts；
- `quality_wait/shortfall`；
- duplicate/forced-adult/sensory-object error；
- ready Action 到 typed remote fact 的 E4 状态。

普通详情不展示完整源上下文和成人候选正文。受控质量抽检页面按权限显示脱敏样本。

### 7.5 权限

新增建议权限：

- `ai_content_policy.view`；
- `ai_content_policy.manage`；
- `ai_content_policy.approve`；
- `ai_provider_routes.view`；
- `ai_provider_routes.manage`；
- `ai_provider_routes.approve`；
- `ai_adult_subject_attestation.manage`；
- `ai_content_quality_review`；
- `ai_content_quality_raw_review`；
- `ai_content_quality_export`。

任务运营可选择已批准 policy/binding，但不能编辑全局 Prompt、example set 或 Provider secret。`manage` 与 `approve` 对 policy/route-set 分权；同一高风险 revision 的创建者不能自批。成人证明管理者只能在其 task/group/source 数据权限内操作，且不能通过修改证明绕过 policy approve。

---
