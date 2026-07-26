# 账号面具初始化与失败恢复可靠性修补 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L2 / P0 生产可靠性修补 |
| 设计状态 | `complete`（2026-07-26 第二轮整体修补评审） |
| 适用范围 | 普通运营账号的账号面具初始化、重建、恢复，以及 AI 活跃群对账号面具的准入 |
| 产品术语 | 产品统一称“账号面具”；`voice_profile`、`AiAccountVoiceProfile` 为兼容技术名 |
| 生产相关 | 是 |
| 发布闸门 | 必须 |
| 完成条件 | 自动初始化失败可持久恢复，单账号缺面具不再阻断其他账号或整个任务，生产连续验证达到本 PRD 的 E4 标准 |

本文是账号面具初始化、生成失败恢复和 AI 活跃群面具准入的专项真相源。总 PRD、全账号日覆盖 PRD 与本文冲突时，以本文对“初始化状态、失败补偿、任务影响范围和恢复唤醒”的定义为准。

## 2. 背景与生产事实

2026-07-26 生产复查确认：

- 在线普通账号共 799 个，其中 798 个存在可用 active 面具，只有 `account_id=814` 没有任何 `ai_account_voice_profiles` 记录。
- 账号 814 于 2026-07-23 19:45 登录成功，自动初始化面具时 AI 返回非法 JSON，审计原始错误为：

```text
Expecting property name enclosed in double quotes: line 1 column 96 (char 95)
```

- 当前流程捕获异常后只写“账号面具初始化失败”审计，不创建持久任务、不设置下一次重试时间，也不产生可被 Recovery 消费的状态。
- `all_accounts_daily` 后期只剩少量未完成账号时，天津、石家庄、郑州楼凤都反复选中 814；面具过滤后本轮候选变为 0，任务被写成“账号面具缺失，等待账号面具初始化后继续执行”。
- 任务累计字段中的数千次 `voice_profile_missing_count` 是 Planner 多轮重复观察同一账号的次数，不是缺失账号数量。

因此本事故不是“大面积面具数据丢失”，而是：

```text
单账号生成失败
  -> 失败状态不可恢复
  -> 覆盖账本持续把该账号作为最后未完成候选
  -> Planner 把单账号缺口放大成任务级中断
  -> 累计计数进一步造成大面积缺失的错误观感
```

## 3. 用户问题与产品目标

### 3.1 要解决的问题

1. 新账号登录成功后，面具初始化不能依赖一次同步 AI 调用的偶然成功。
2. AI 非法 JSON、内容校验失败、供应商超时或配置错误必须形成可恢复、可观察的持久事实。
3. 单个账号缺面具只能阻塞该账号使用，不能停止其他账号的自然聊天、每日覆盖或硬小时履约。
4. 面具补齐后，相关任务和覆盖账本必须自动回流，不依赖运营手工暂停、恢复或重置任务。
5. 页面和诊断必须展示“缺失账号数”和“失败观察次数”的不同含义，不能把同一账号的重复扫描累计成大面积故障。

### 3.2 产品目标

- 把账号面具初始化从登录请求内的一次性副作用改为持久化、幂等、可恢复的后台履约。
- 为每个需要面具的账号维护明确状态、尝试次数、下一次重试、原始错误和人工处置入口。
- 保证已有可用面具在重建失败时继续有效；新版本成功前不得覆盖或停用旧 active 版本。
- 让 Planner 按账号隔离面具阻塞，并继续扫描、规划其他可用账号。
- 面具恢复成功后自动刷新缓存、覆盖账本、任务调度和异常聚合。
- 建立可以证明“数据修复、代码修复、任务恢复”三个层次的生产验收证据。

### 3.3 非目标

- 不允许使用 mock、模板面具或空摘要让账号伪装为 ready。
- 不允许缺面具账号绕过质量门直接生成或发送 AI 活群正文。
- 不把 AI 供应商故障包装成 Telegram 发送失败。
- 不因为修补初始化链路而改变接码专用、搜索降权专用或其他隔离账号的用途边界。
- 不在数据库迁移过程中同步批量调用外部 AI。
- 不用清空覆盖账本、缩小覆盖分母或伪造成功解决缺面具问题。

## 4. 核心定义

### 4.1 可用账号面具

账号只有同时满足以下条件才拥有可用面具：

```text
AiAccountVoiceProfile 存在
status = active
quality_status = active
version > 0
short_prompt_summary 非空且通过服务端结构校验
账号用途允许 account_mask_init
```

Redis 命中不构成可用事实。缓存缺失时必须从数据库恢复；缓存存在但数据库版本已停用、被 supersede 或校验失败时不得继续使用。

#### 4.1.2 轻量独立面具（单账号恢复）

账号 814 这类单账号恢复可以使用**轻量独立面具**：它是该账号自己的、通过真实 AI 生成和服务端校验的简化人格，不是复用另一账号面具，也不是空摘要、模板或跳过面具门禁。

- 轻量化只减少 Provider 输出字段和文案负担；运行时仍须满足本节全部可用条件。
- 单账号恢复的最小 JSON 行固定为 `id,mask,aud,frame,tags,habits,ban,summary`：`tags` 为 2–4 项，`habits` 与 `ban` 各为 3–5 项，`summary` 为具体可执行的短摘要；其余历史可选画像字段可为空。
- `id` 必须等于待恢复账号；`mask/aud/frame/summary` 必须体现该账号自身的成年男性日常社交身份和差异点。不得复制其他账号的字段、版本或缓存值。
- 轻量面具不得写入色情、性交易、寻欢、夜场、楼凤、外围、招嫖等敏感交易标签；不得虚构可核验的真实个人、管理员或指定对象身份。
- “减轻说法”指使用日常社交身份措辞、短句、单行 JSON、最小字段集，并在 Provider 支持时请求 JSON object 输出；不降低 `active + quality_active + version + short_prompt_summary` 的准入门槛，也不把生成失败伪装为 ready。

此契约优先用于 `recovery`、`manual_single` 和单账号 `login_auto` item；批量生成仍可按同一最小 Schema 输出多行 JSONL。

#### 4.1.1 账号用途判定真相源

`account_mask_init` 是否允许由 `account.account_identity` + `account_pool.pool_purpose` 双字段一致性决定，二者均为现有字段，不新增用途枚举：

| `account_usage` | 判定条件 | 是否允许 `account_mask_init` |
| --- | --- | --- |
| `normal` | `account_identity=normal` 且（`pool_id` 为空或对应 `pool_purpose=normal` 的启用池） | 是 |
| `code_receiver` | `account_identity=code_receiver` 且绑定 `pool_purpose=code_receiver` 启用池 | **否**（接码专用，禁止） |
| `rank_deboost` | `account_identity=rank_deboost` 且绑定 `pool_purpose=rank_deboost` 启用池 | **否**（搜索降权专用，禁止） |
| `mismatch` | 上述条件不成立（字段不一致、租户不一致或池禁用） | **否**，并写 `account_purpose_mismatch` 审计 |

判定由 `app.services.account_usage_policy.account_usage(account, pool)` 单一函数完成；`assert_account_action_allowed(account, pool, "account_mask_init")` 在非 normal 时抛 `account_action_not_allowed:<usage>:account_mask_init` 或 `account_purpose_mismatch`。

迁移与老账号回填规则（[§11 step 6](#11-数据迁移与上线) 引用同一口径）：

- 有可用 active 面具且 `account_usage=normal`：投影为 `active`。
- 无可用面具且 `account_usage=normal`：创建 `recovery` 生成项。
- `account_usage ∈ {code_receiver, rank_deboost}`：标记 `not_required`，不创建生成项；接码专用和搜索降权专用账号继续禁止面具初始化。
- `account_usage=mismatch`：不创建生成项，单独列出交由账号池一致性修复；不得通过批量面具补齐绕过 mismatch。
- 运营显式 `disabled`：保持 disabled。

Reconcile 不重新判定已被运营移动到专用池的账号；账号池变更由 `sync_account_usage` 写审计并触发覆盖行重算。

### 4.2 两套独立状态

面具资产与生成履约是两套真相源，禁止用一个字段混合表达。

| `profile_status` | 含义 | 是否允许 AI 活群使用 |
| --- | --- | --- |
| `not_required` | 账号用途不允许或不需要账号面具 | 否，且不得进入普通运营任务 |
| `missing` | 需要面具但没有可用 active 版本 | 否 |
| `active` | 存在通过校验的当前 active 版本 | 是 |
| `disabled` | 运营显式停用 | 否，不得被自动初始化重新启用 |
| `unusable` | 存在记录但质量状态、结构或版本不满足可用条件 | 否 |

| `generation_item_status` | 含义 | 是否终结 |
| --- | --- | --- |
| `queued` | 已持久入队，等待 worker | 否 |
| `generating` | worker 已领取，正在调用真实 AI | 否 |
| `validating` | AI 已返回，正在做结构、身份和差异度校验 | 否 |
| `retry_wait` | 本次失败，等待 `next_retry_at` | 否 |
| `persist_unknown` | AI 已返回但版本落库结果未知，等待按版本事实核对 | 否 |
| `manual_required` | 已达到自动重试边界或需要运营修正配置 | 是 |
| `succeeded` | 新版本已原子提交并完成后置回流 | 是 |
| `skipped` | 触发时已存在满足目标的版本，或账号已经不需要生成 | 是 |
| `cancelled` | 账号被禁用、删除或用途变更后受控终止 | 是 |

页面并列展示 `profile_status + generation_item_status`。已有 active 面具重建中可以同时显示 `active + generating`；重建失败不能把资产状态改为 missing。

## 5. 持久化模型

新增持久化生成任务与账号项，名称可在开发设计中按现有 ORM 规范收敛，但必须保留以下业务语义。

### 5.1 生成任务

`AiAccountVoiceProfileGenerationJob` 至少包含：

- `id`、`tenant_id`；
- `source`：`login_auto`、`task_precheck`、`daily_reconcile`、`manual_single`、`manual_batch`、`recovery`；
- `status`：`queued/running/partial/succeeded/failed/cancelled`；
- `requested_by`、`reason`；
- `total_count/succeeded_count/retry_wait_count/failed_count/skipped_count`；
- `created_at/started_at/finished_at`。

job 的 `status` 和计数从 item 当前状态聚合产生，不允许由另一路径独立修改：全部 queued 为 `queued`，存在 generating / validating / retry_wait / persist_unknown 为 `running`，全部 `succeeded/skipped` 为 `succeeded`，成功与 `manual_required/cancelled` 混合为 `partial`，全部 cancelled 为 `cancelled`，其余全部终结且没有成功为 `failed`。job 与 item 状态、计数更新必须处于同一事务；聚合可重算，不能只相信可能漂移的累计字段。

### 5.2 账号生成项

`AiAccountVoiceProfileGenerationItem` 至少包含：

- `job_id`、`tenant_id`、`account_id`；
- `status`，取值只能来自 4.2 的 `generation_item_status`；
- `idempotency_key`、`expected_profile_version`、`base_profile_version`、`result_profile_version`；
- `attempt_count`、`next_retry_at`；
- `error_code`、`error_detail`、`provider_request_id`；
- `lease_owner`、`lease_expires_at`；
- `source`、`created_at/updated_at/finished_at`。

同一租户、同一账号同时只能有一个未终结生成项。重复触发必须返回或合并到既有 active item，不能并发生成两个相同版本。

数据库必须用部分唯一约束保证：

```text
UNIQUE (tenant_id, account_id)
WHERE status IN (queued, generating, validating, retry_wait, persist_unknown)
```

人工处理已终结的 `manual_required` 时必须创建新的 item，使用新的 `idempotency_key`，并通过 `previous_item_id` 关联历史；不得清零、覆盖或删除旧 item 和 attempt。

### 5.3 生成尝试

`AiAccountVoiceProfileGenerationAttempt` 至少包含 `tenant_id/job_id/item_id/attempt_no/stage/provider/provider_request_id/started_at/finished_at/outcome/error_code/error_detail/prompt_feedback_summary`。一次真实 AI Provider 调用等于一次 attempt；生成函数内部不得再执行不可见的三轮重试。

生成任务由独立 `voice-profile` worker 消费。该角色必须有独立 heartbeat、Docker healthcheck、drain batch、并发数和 AI Provider 限流，不得复用 Planner 线程执行外部 AI。

### 5.4 错误码

至少支持：

| 错误码 | 场景 | 默认处置 |
| --- | --- | --- |
| `voice_profile_output_malformed` | JSON / JSONL 解析失败、字段结构错误 | 携带原错误重试 |
| `voice_profile_output_incomplete` | 缺必要字段或缺短摘要 | 携带缺失字段重试 |
| `voice_profile_identity_invalid` | 账号身份方向不符合已批准面具规则 | 携带校验原因重试 |
| `voice_profile_too_generic` | 内容过于泛化 | 携带校验原因重试 |
| `voice_profile_similarity_rejected` | 同批差异度不足 | 仅重试失败项 |
| `voice_profile_provider_timeout` | AI 请求超时 | 退避重试 |
| `voice_profile_provider_unavailable` | 供应商不可用 | 退避重试并告警 |
| `voice_profile_provider_config_invalid` | 未配置、鉴权失败或模型能力不符 | `manual_required` |
| `voice_profile_persist_conflict` | 版本并发或落库冲突 | 重读当前版本后幂等收口 |
| `voice_profile_account_ineligible` | 账号用途或状态不允许初始化 | `not_required` / skipped |

必须保留供应商原始错误名和安全可展示的原始 detail。不得只保存“生成失败”。

## 6. 触发与执行流程

### 6.1 登录成功

登录成功的本地数据库事务必须同时提交账号 / 授权事实和 `login_auto` 生成项：

```text
Telegram 登录成功
  -> 开启本地短事务
  -> 写账号 / Session / 授权事实
  -> 判断账号用途是否允许初始化
  -> 同事务幂等写入账号面具生成 Job / Item queued
  -> 提交本地短事务
  -> 返回登录成功
  -> 后台 worker 领取并调用 AI
```

登录接口不得在持有登录事务时等待外部 AI。AI 生成失败不能把已经提交的 Telegram 登录事实改写为失败；但账号 / 授权事实与 queued 生成项的本地事务提交失败时，登录接口必须显式返回本地持久化失败，不能只写审计后返回成功。不存在“登录成功但没有 active 面具且没有未终结生成项”的合法稳定状态。

### 6.2 任务创建、启动和预检

- 创建/启动预检必须返回目标账号总数、可用面具账号数、`queued/retry_wait/manual_required/disabled` 数及样例；该只读预检不得把未提交的生成 Job 伪装成已创建。
- 任务创建或启动后的首个 Planner tick 自动幂等入队缺面具账号，但预检不得假装已经可用。
- 对全账号日覆盖任务，缺面具账号保留在覆盖分母并写账号级 blocker。
- 如果仍有其他面具可用账号，任务可以继续规划这些账号。
- 如果所有可执行账号都缺面具，任务显示结构化阻塞，但生成任务仍由独立 worker 推进，不能靠 Planner 高频扫描触发 AI。

### 6.3 定时一致性核对

独立 reconcile 周期按显式分页扫描：

- 当前应参与普通运营任务、但没有可用面具、没有未终结生成项且没有待人工处理 `manual_required` 的账号；
- `generating` 租约过期的账号项；
- `retry_wait` 且已到期的账号项；
- 数据库 active 面具与 Redis 缓存版本不一致的账号。

reconcile 只建立或恢复持久任务，不直接在扫描事务内调用 AI。

reconcile 的核心不变量为：

```text
eligible_for_account_mask_init
AND profile_status IN (missing, unusable)
=> exactly one non-terminal generation item
   OR one terminal manual_required item with explicit operator action
```

巡检指标从数据库当前去重事实计算；Planner 的观察计数不作为覆盖率和缺失数真相源。

### 6.4 手工重建和批量补齐

- 第一方页面的单账号重建、缺失账号批量补齐和已选账号批量重建统一进入同一持久生成链；旧同步接口仅按 6.5 保留一个兼容发布周期。
- 接口返回 job 与逐账号 item，不以 HTTP 请求持续等待整个 AI 批次。
- 运营可查看进度、失败原因和下一次重试时间。
- 对已有 active 面具的账号，重建失败不影响旧版本继续使用。

### 6.5 API 契约与兼容迁移

| 接口 | 作用 | 返回 |
| --- | --- | --- |
| `POST /api/ai-account-voice-profile-generation-jobs` | 创建单账号、批量缺失补齐或批量重建 job；请求包含 `mode/account_ids/rebuild_existing/reason/idempotency_key` | HTTP 202 + job/items |
| `GET /api/ai-account-voice-profile-generation-jobs` | 分页查看生成任务，按状态、来源、账号和时间筛选 | job 摘要与状态计数 |
| `GET /api/ai-account-voice-profile-generation-jobs/{job_id}` | 查看任务与逐账号 item | 尝试、版本、错误、下一次重试和审计 |
| `POST /api/ai-account-voice-profile-generation-items/{item_id}/retry` | 对 `retry_wait/manual_required` 项人工重试；请求包含 `reason/expected_status/expected_profile_version/idempotency_key` | HTTP 202 + 更新或新建的 item 与所属 job |

现有 `POST /api/ai-account-voice-profiles/{account_id}/rebuild` 和 `POST /api/ai-account-voice-profiles/batch-rebuild` 在首个兼容发布周期保持现有同步请求、响应模型和状态码，不得在原路径静默改成 202。第一方页面在同一发布中切到新 job 创建接口；旧接口标记 deprecated，只执行一次真实 Provider attempt，不包含内部隐藏重试。确认访问日志中没有旧客户端后，下一独立版本再删除旧接口。job/items 保留逐账号 `created/skipped/failed` 最终投影供完成后展示和导出。

所有 job/item 查询和写入都必须带 tenant 条件；跨租户 ID 统一返回 404。写接口显式校验 `ai_voice_profiles.manage`。重复 `idempotency_key` 返回原结果；`expected_status` 或 `expected_profile_version` 不一致返回 409，不能并发创建多个活动生成项。

## 7. AI 生成、校验和重试

### 7.1 生成边界

- 只能调用租户批准且健康的真实 AI 供应商。
- 输出必须符合明确 JSON Schema；解析失败写 `voice_profile_output_malformed`。
- Provider 对单账号恢复必须按 [§4.1.2](#412-轻量独立面具单账号恢复) 请求一行最小 JSON，不再要求 `age/px/cx/len/tone/words/emoji` 等非准入字段；解析、字段或身份校验失败时把精确原因带入下一次 item attempt。
- 批量结果中只有失败项进入补偿，已通过项不重新生成。
- 生成、结构校验、业务校验和落库分别记录阶段，便于定位是供应商、输出、规则还是数据库问题。

### 7.2 重试策略

自动重试策略必须显式、可配置、可观测，默认：

1. 单个生成项包含首次尝试在内默认最多 4 次自动 attempt；每次 Provider 调用前先持久写 attempt，调用结束后再写 outcome。
2. 结构化输出、字段缺失、身份方向或差异度失败：后续请求携带精确失败原因，只重试失败项。
3. 超时和临时不可用：按 `1 分钟 -> 5 分钟 -> 30 分钟` 退避。
4. 供应商配置错误、鉴权失败或账号用途不允许：不高频重试，进入 `manual_required` 或 `not_required`。
5. worker 崩溃或租约过期：Recovery 回收到 `queued`，保留原尝试次数和错误。

达到自动重试边界后必须进入 `manual_required` 并产生运营异常，不得静默放弃，也不得无限热循环。

现有生成质量函数中的内部三轮循环必须移除或拆成 item attempt；禁止 item 四次重试再叠加函数内部三次重试。`prompt_feedback_summary` 只携带上一 attempt 的精确结构 / 校验错误和必要禁用样例，不携带密钥或完整敏感响应。

#### 7.2.1 Provider 限流与 AI 活群独立

面具生成 Provider 限流与 AI 活群（`group_ai_chat` 文本 draft / 图片验证码识别）**完全独立**，互不挤占：

| 维度 | 面具生成（voice-profile worker） | AI 活群（Planner / Executor） |
| --- | --- | --- |
| 限流键 | `tenant_id + provider + bucket=voice_profile` | `tenant_id + provider + bucket=ai_group` |
| 并发上限 | 独立配置（默认 2） | 沿用现有 `ai_group_chat` 配额 |
| Token 预算 | 独立配额，不与活群共享 | 沿用现有预算 |
| 触发限流时 | item 退避 `retry_wait`，不影响活群 AI | 不影响面具生成 |

实现上 `voice-profile` worker 必须使用独立限流器（独立 Redis 计数键或独立 in-process 令牌桶），不得复用活群 AI Gateway 的限流入口；自动重试和人工重试共用面具侧同一并发与 token 预算。历史补偿分页入队时，面具侧总入队速率受面具侧独立配额约束，不得因一次性批量入队耗尽活群 AI 配额导致活群 draft 失败。

### 7.3 版本原子性

- 新版本完成全部校验后，才在同一短事务中把旧 active 置为 `superseded` 并写入新 active。
- 写入前重读 `base_profile_version`；版本已变化时按幂等结果收口或重新排队，不能覆盖人工编辑的新版本。
- 数据库提交成功后刷新 Redis；缓存失败写告警，但数据库 active 版本仍是真相源。

## 8. AI 活跃群与覆盖账本联动

### 8.1 账号级隔离

Planner 读取账号时必须把面具状态作为账号级准入：

```text
有可用面具
  -> 继续在线、容量、权限和内容规划

无可用面具
  -> 当前账号不创建发送 Action
  -> 覆盖行 blocked/voice_profile_missing
  -> 关联或创建持久生成项
  -> next_eligible_at 对齐生成项 next_retry_at
  -> 推进 keyset 游标并继续扫描后续页
```

Planner 必须按显式页大小循环扫描，直到填满本轮 Action 预算或当前候选源耗尽；不能先按 `account_limit` 截断账号、再因截断集合全部缺面具而返回空。本轮候选源耗尽但硬小时仍有缺口时，允许继续从“已确认过当日覆盖、当前面具 active、其他准入通过、未超过账号日上限”的账号池补位；该补位只履行硬小时消息目标，不改变全账号覆盖分母或伪造新的覆盖完成。补位账号的 credit 口径、`account_daily_limit_exceeded` 处置和唤醒时机由 `ai-group-hard-hourly-target-prd.md` 2026-07-26 面具 supersede 段定义，本节不重复。禁止从“本轮选中的账号全部缺面具”直接推导“整个任务永久阻塞”。任务级状态必须由当前不同 blocker 的账号聚合事实计算。

**与硬小时 PRD 双向 supersede：** 本节定义面具侧的账号级隔离与补位资格判定，`ai-group-hard-hourly-target-prd.md` 2026-07-26 面具 supersede 段定义硬小时侧的 credit 口径与 `account_daily_limit_exceeded`；两份 PRD 冲突时，面具侧状态机以本节为准，硬小时侧 credit 与补位上限以硬小时 PRD 为准。

### 8.2 覆盖账本

- 缺面具账号仍在当日覆盖分母中。
- blocker 至少记录 `voice_profile_missing`、生成项状态、`next_retry_at` 和最后错误码。
- 同一账号 blocker 状态没有变化时只更新最后观察时间，不增加当前缺失账号数；状态转换计数按唯一 `coverage_row + blocker_revision` 记录。
- 面具生成成功后，系统按 `account_id` 增量刷新所有相关任务的当日覆盖行。
- 只有其他准入条件也满足时，覆盖行才从 `blocked` 回到 `ready`。
- 回到 `ready` 时必须重写 `targeted_at`，确保已推进的 keyset 游标能够再次选中该账号。
- 恢复事件必须唤醒任务 `next_run_at`、日覆盖检查和硬小时检查。

### 8.3 已有 Action

- 缺面具账号不得创建新的 AI 活群发送 Action。
- 历史 `pending/claiming` Action 没有固化面具版本时，面具补齐后不得直接发送旧文案；必须显式写 `voice_profile_replan`，释放覆盖预约和消息记忆，再用新版本重建。
- 普通编辑、重建或版本回滚后，已经固化有效面具版本且尚未进入 Gateway 的 Action 继续使用该版本，不被后台新版本改写。
- 运营显式 `disabled` 或质量状态降为 unusable 属于安全撤销：所有尚未写 `gateway_started_at` 的 open Action 必须转为 `skipped/voice_profile_disabled` 或 `skipped/voice_profile_unusable`，释放覆盖预约并清除 Redis；已经进入 Gateway 的 Action 保持真实 success/failed/unknown 结果，不能伪装成 skipped。
- 停用、恢复、回滚、人工编辑和质量状态变化都必须触发相关覆盖行重算、缓存刷新与任务唤醒。只有恢复为 active 且其他准入条件通过时才回到 ready。

#### 8.3.1 `voice_profile_replan` 状态语义（与代码现状对齐）

`voice_profile_replan` 是 **Action 终态描述**，由 `Action.status="skipped"` + `Action.result.error_code="voice_profile_replan"` 联合表达，不新增 `Action.status` 枚举值。代码入口为 `group_ai_chat._skip_profileless_action_for_replan` / `_skip_open_action_for_replan`。

| 字段 | 值 |
| --- | --- |
| `Action.status` | `skipped` |
| `Action.result.error_code` | `voice_profile_replan` |
| `Action.result.message` | `账号面具已生效，旧规划已跳过等待重新生成` |
| `Action.executed_at` | 当前时间 |
| `Action.lease_owner` | 清空 |
| `Action.payload.account_voice_profile_version` / `account_mask_version` | 保持原值（均为 0，用于判定需要 replan 的依据） |

关联资源处置：

- 覆盖预约 `reserved_action_id`：由 `_skip_open_action_for_replan` 释放，覆盖行从 `reserved` 回到 `ready`（其他准入满足时）或保持 `blocked`。
- 消息记忆 `ai_group_message_memory`：标为 `expired_before_send`，状态 `expired`，不参与后续去重；释放 `reservation_key`。
- 任务统计 `task.stats["voice_profile_replanned_open_action_count"]`：累加本次 replan 的 Action 数，作为诊断指标。
- 已进入 Gateway（`gateway_started_at` 非空）的 Action **不得** 写 `voice_profile_replan`，保持真实 success/failed/unknown。

触发时机：

1. 面具生成项 `status` 转 `succeeded` 后，reconcile / 唤醒任务按 `account_id` 扫描该账号下 `task_type=group_ai_chat` 且 `action_type=send_message`、`status ∈ {pending, claiming}`、`payload.voice_profile_version=0` 的 Action，逐条写 `voice_profile_replan`。
2. 运营显式停用 / 质量降级时，同样扫描未进入 Gateway 的 open Action，但写 `voice_profile_disabled` / `voice_profile_unusable`，不写 `voice_profile_replan`。

## 9. 页面与运营处置

### 9.1 账号面具 > 面具管理

列表增加：

- 面具资产状态；
- 初始化履约状态；
- 当前 / 基础 / 结果版本；
- 尝试次数；
- 最近错误码与错误摘要；
- 下一次重试时间；
- 来源；
- 最近成功时间。

操作包括：

- “立即重试”：仅对 `retry_wait/manual_required` 可用；
- “批量补齐缺失面具”；
- “查看生成记录”；
- “查看原始错误与审计”。

写操作继续要求 `ai_voice_profiles.manage`，并记录操作者、原因、影响账号和前后状态。

“立即重试”对未终结 `retry_wait` 只将原 item CAS 回 `queued`，保留 attempt_count；对已终结 `manual_required` 创建关联旧 item 的新 item，并重新获得自动 attempt 上限。重复点击使用 `idempotency_key` 返回同一结果；状态或面具版本已变化时页面展示 409 冲突并刷新当前事实。

### 9.2 异常与审计

必须聚合展示：

- 缺面具账号数；
- 自动恢复中账号数；
- 需要人工处理账号数；
- 最近 24 小时失败错误 Top N；
- 最老未恢复时长；
- 受影响任务和覆盖义务数。

账号级异常修复入口必须回到精确账号，不能只给出任务“重置”按钮。

### 9.3 任务中心

任务详情至少展示：

- `voice_profile_missing_account_count`：当前去重账号数；
- `voice_profile_retry_wait_account_count`；
- `voice_profile_manual_required_account_count`；
- 受影响账号样例；
- 当前生成项状态和下一次重试时间；
- 其他仍可规划账号数。

历史累计观察次数可以单列为诊断指标，但不得继续使用 `voice_profile_missing_count` 冒充当前缺失账号数。`last_error` 必须来自当前快照；阻塞恢复后清除历史文案。

指标由 reconcile / 指标聚合服务按数据库当前去重事实计算，至少包含 `profile_missing_current`、`generation_item_by_status`、`generation_queue_oldest_seconds`、`generation_attempt_total_by_error` 和 `orphan_missing_account_count`。Planner 只能写观察事件，不能增减这些当前值。

## 10. 并发、幂等与失败边界

- 登录回调、任务预检、reconcile 和运营手工重试并发触发时，同一账号只能形成一个 active 生成项。
- 多 worker 通过租约和 CAS 领取，不能同时调用 AI 生成同一账号同一版本。
- AI 已返回但本地落库结果未知时，标记 `persist_unknown` 等可恢复状态，先按版本事实核对，不能立即再次生成。
- 批量任务部分成功时状态为 `partial`，成功项保持成功，失败项独立恢复。
- 停用面具是运营显式动作；自动 reconcile 不得把 `disabled` 静默改回 active。
- 删除、禁用或用途变为不允许运营的账号，其未执行生成项终止为 skipped/cancelled，并保留历史审计。
- job、item 和 attempt 的可下钻运行明细至少保留 90 天；`manual_required`、未终结项和与生产事故关联的记录在问题关闭前不得清理。AuditLog 按平台审计保留策略长期保留。清理任务只处理已终结明细，不得改变 profile、覆盖账本或 job 最终汇总。

## 11. 数据迁移与上线

发布顺序固定为：

1. 上 schema、唯一约束、状态投影和 `voice_profile_generation_v1` feature flag，所有新链路保持关闭；migration 不调用 AI。
2. 部署独立 `voice-profile` worker 暗运行，默认单实例并发 2、drain batch 20，验证 heartbeat、租约回收和 Provider 限流。
3. 仅对测试租户 / canary 账号开启 job 创建，验证 queued-to-start、非法 JSON 重试、版本 CAS、缓存和覆盖回流。
4. 第一方页面切到新 job API；旧同步 rebuild 保留一个兼容发布周期。
5. 对生产 `account_id=814` 开启受审计 recovery，确认新 active 版本与任务回流。
6. 以分页 reconcile 扫描当前普通运营账号：
   - 有可用 active 面具：投影为 `active`；
   - 无可用面具：创建 `recovery` 生成项；
   - 显式 disabled：保持 disabled；
   - 用途不允许：标记 `not_required`。
7. 队列稳定后全量开启登录事务入队与任务预检入队；生成成功只做按账号增量唤醒，不做全量任务重置。

默认运行告警：worker heartbeat 超过 2 个周期缺失、oldest queued 超过 15 分钟、`orphan_missing_account_count > 0`、`manual_required` 新增或 Provider 配额持续拒绝。历史补偿必须分页、限并发并受 tenant + provider 预算约束，不能一次把全部存量账号同时提交给 AI。

回滚顺序为先关闭登录 / 预检 / reconcile 新入队，再让 worker drain 已领取项，最后停止领取；保留 queued、已生成版本和审计事实。不得删除任务项、把失败项伪装成成功，或让新登录继续产生无人消费的 queued 项。

#### 11.1 回滚期间新登录账号 fallback

回滚关闭登录事务入队后，新登录账号不得"登录成功但无面具且无生成项"，必须按以下顺序选择 fallback：

1. **首选**：走旧同步 rebuild 路径（`POST /api/ai-account-voice-profiles/{account_id}/rebuild`），该路径在 [§6.5](#65-api-契约与兼容迁移) 兼容发布周期内保留，仍执行一次真实 Provider attempt；旧路径与新 job API 共用同一 `account_mask_init` 用途判定，DEDICATED 账号仍拒绝。
2. **旧路径已下线时**：登录接口必须在本地事务中显式写 `account.voice_profile_status=missing` + 审计 `login_voice_profile_pending_rebuild`，并返回"登录成功但面具待补齐"的可观察状态；不得返回纯成功。
3. **回滚期间 reconcile 仍可消费**：reconcile 周期扫描到 `missing` 且无未终结生成项的账号时，自动通过旧同步路径补齐；不依赖登录入队。

兼容发布周期边界：旧同步 rebuild 路径必须与新 job API 在同一发布周期内共存（[§6.5](#65-api-契约与兼容迁移)），确保回滚期间新登录有可用 fallback；只有在确认无旧客户端调用旧接口后，才在下一独立版本删除旧路径。回滚不得同时删除旧路径又关闭新入队，否则新登录账号进入"无面具无生成项"孤儿状态。

## 12. 权限与安全

- 查看状态需要 `account_masks.view`。
- 重试、重建、停用、恢复和批量补齐需要 `ai_voice_profiles.manage`。
- 普通页面不展示供应商密钥、完整请求体或不安全原始响应。
- 错误详情保留解析位置、错误类型、provider request id 和安全摘要；敏感字段按平台日志规范处理。
- 接码专用和搜索降权专用账号继续禁止面具初始化，不能通过手工批量选择绕过。

## 13. QA 验收

### 13.1 自动初始化

- 账号 / 授权事实和 queued 生成项在同一事务提交；任一写入失败都不允许出现半提交。
- 本地事务成功后，即使 AI 超时或返回非法 JSON，登录事实仍保持成功。
- 同一账号只产生一个 active 生成项。
- 非法 JSON 写 `voice_profile_output_malformed`、原始解析错误、尝试次数和下一次重试时间。
- 一次 Provider 调用只新增一次可下钻 attempt；四次 item 自动尝试最多产生四次调用，不得叠加成十二次。
- 自动重试成功后创建 active 版本并刷新缓存。
- 配置错误进入 `manual_required`，不高频循环。

### 13.2 版本与缓存

- 已有 active 面具重建失败时，旧版本继续可用。
- 新版本只有全部校验通过后才替换旧版本。
- 并发人工编辑与后台生成不会覆盖较新的人工版本。
- Redis 丢失后可从 DB 恢复；缓存旧版本不能覆盖 DB 新版本。

### 13.3 Planner 与覆盖

- 600 个可用账号加 1 个缺面具账号时，Planner 跳过缺失账号并继续规划其他账号。
- 前一页全部缺面具、后一页存在可用账号时，Planner 继续分页并填满本轮预算。
- **所有可执行账号都缺面具时**：任务显示结构化阻塞（`last_error` 来自聚合 blocker，非空），不创建发送 Action；面具生成 worker 仍按独立节奏推进，Planner 不得高频重试 AI、不得空转热循环；任务 `next_run_at` 对齐最早 `next_retry_at`。
- 当日覆盖 ready 行耗尽但硬小时仍欠量时，只从已完成覆盖且未超账号日上限的 active 面具账号补位；不得增加覆盖完成数。
- **补位账号超过账号日上限时**：写 `skipped/account_daily_limit_exceeded`，不伪装成功，不从 `durable_debt` 排除义务；继续扫描其他未超上限账号。
- 缺面具账号覆盖行保持在分母，状态为 `blocked/voice_profile_missing`。
- 面具成功后覆盖行按其他准入事实回到 `ready`，重写 `targeted_at` 并唤醒任务。
- 未固化面具版本的旧 Action 写 `voice_profile_replan`，不得直接进入 Gateway。
- 当前缺失账号数按去重账号统计；重复 Planner tick 不增加该数。
- 显式停用 / 质量降级时，未进入 Gateway 的 open Action 被跳过并释放预约；已进入 Gateway 的结果保持真实。

### 13.3.1 登录事务与并发幂等

- **登录本地事务失败时**（DB 写不下去）：账号 / 授权事实和 queued 生成项都不落库，登录接口显式返回本地持久化失败，不留半提交；不得只写审计后返回成功。
- **同账号并发触发幂等合并**（登录回调 + 任务预检 + reconcile + 运营手工重试同时触发）：同一 `tenant_id + account_id` 只形成一个 active 生成项，重复触发返回或合并到既有 item，不并发生成两个相同版本；测试覆盖 4 路并发入口同时触发的场景。
- 登录事务提交后 AI 超时或返回非法 JSON：登录事实保持成功，生成项进入 `retry_wait` 并设 `next_retry_at`。

### 13.4 页面和权限

- 面具管理、异常与审计、任务详情显示相同的当前状态与错误码。
- 只读用户不能重试或重建。
- 批量结果逐账号展示成功、重试中、人工处理和跳过原因。
- 跨租户 job/item ID 返回 404；重复 idempotency key 返回原结果；expected state/version 冲突返回 409。
- 新 job API 与旧同步 rebuild 在兼容发布周期内各自保持固定响应模型，第一方页面不再依赖旧接口。

## 14. 生产验收与证据等级

### 14.1 面具子系统 E4

- `orphan_missing_account_count=0`：不存在“需要面具、没有可用面具、没有未终结生成项、也没有明确 manual_required”的账号。
- 真实新增账号至少一次证明登录事务入队、worker 消费、失败重试或成功落库的完整链路。
- 非法 JSON、worker 租约过期和 Provider 临时不可用均留下真实 item/attempt/next_retry 证据。
- 轻量恢复提示不得出现敏感交易措辞；严格 JSON 输出中出现该类措辞必须被拒绝并进入可观察的重试或人工处理状态。
- DB、Redis、页面、任务详情、覆盖账本和审计一致。

满足本节只能说明账号面具子系统恢复，不能宣告活群目标达标。

### 14.2 面具数据完整性 E4

- 账号 814 通过真实 AI 和统一生成链得到可用 active 面具。
- 审计包含 recovery 来源、版本、生成结果和操作者 / worker。
- 任务所需普通运营账号达到 100% active 面具覆盖；运营明确停用或用途不允许的账号必须有产品批准的排除事实。
- `retry_wait/manual_required` 只证明可观察和可处置，不等于数据完整性通过。

### 14.3 活群解除面具阻塞 E4

- 天津、石家庄、郑州楼凤不再因 814 写任务级 `voice_profile_missing`。
- 面具恢复后对应覆盖行能重新进入调度。
- 任务详情当前缺失账号数与数据库去重查询一致，不再出现“一个账号累计数千次”等误导。
- 生产连续至少 3 个完整小时没有因单账号缺面具阻断其他可用账号规划。

本节只证明“面具不再阻断活群”。完整活群 `production_fixed` 仍必须按硬小时、全账号日覆盖、目标有效性、账号容量和真实远端消息 ID 的专项 E4 验收，不能由本文替代。

### 14.4 本修补可写 `production_fixed` 的条件

只有 14.1、14.2、14.3 全部通过，才可把“账号面具初始化与失败恢复修补”写为 `production_fixed`。活群任务本身只有同时满足其余专项 E4 才能单独写 `production_fixed`。

## 15. Product Design Complete 自检

| 检查项 | 结果 |
| --- | --- |
| 原始问题与生产证据 | 已覆盖 |
| 产品目标与非目标 | 已覆盖 |
| 前端状态与运营动作 | 已覆盖 |
| 后端任务、状态机、错误码和 API 语义 | 已覆盖 |
| 数据模型与数据流 | 已覆盖 |
| Planner、覆盖账本和 Action 生命周期 | 已覆盖 |
| 并发、幂等、版本和缓存一致性 | 已覆盖 |
| 权限、安全和账号用途隔离 | 已覆盖 |
| 迁移、回滚和生产修复 | 已覆盖 |
| QA 与 E4 验收 | 已覆盖 |

第二轮自检已补齐登录事务原子性、独立状态机、单层 attempt、Planner 跨页与硬小时补位、旧 API 兼容、停用 / 质量降级 Action 处置、租户幂等、灰度回滚和分层 E4，`design_status=complete`。进入开发前仍需输出 Product Handoff；dev 按本文固定的模型名和 `voice-profile` worker role 落地迁移与代码，并将最终迁移号和代码入口同步到项目结构索引。

第三轮修补（2026-07-26）已补齐 7 个 dev 阻塞 / 一致性缺口：

| 缺口 | 修复位置 |
| --- | --- |
| `account_mask_init` 用途判定真相源 | §4.1.1（`account_identity` + `pool_purpose` 双字段，DEDICATED 集合 = `{code_receiver, rank_deboost}`） |
| Provider 限流与 AI 活群独立 | §7.2.1（独立限流键、独立并发与 token 预算） |
| `voice_profile_replan` 状态语义 | §8.3.1（`Action.status=skipped + error_code=voice_profile_replan`，与代码现状对齐） |
| 与硬小时 PRD 双向 supersede | §8.1 末段 + `ai-group-hard-hourly-target-prd.md` 2026-07-26 面具 supersede 段 |
| 工单 1 RC-A / D1 / D2 / E4 口径同步 | `docs/04-ops/tickets/2026-07-25-p0-ai-group-hard-hourly-blockers.md` RC-A 改为单账号放大 + 计数口径错误，D1 改为验证去重值，E4 引用面具 PRD §14 |
| QA 测试场景补齐 | §13.3 / §13.3.1（所有账号缺面具、补位超日上限、登录事务失败、4 路并发幂等） |
| 回滚期间新登录 fallback | §11.1（旧同步 rebuild 路径作为 fallback，与兼容发布周期边界对齐） |

## 16. 关联文档

- `docs/01-product/tg-ops-platform-prd.md`
- `docs/03-feature-designs/ai-group-all-accounts-daily-coverage-prd.md`
- `docs/03-feature-designs/ai-group-hard-hourly-target-prd.md`
- `docs/03-feature-designs/ai-group-send-continuity-and-terminal-targets-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/04-ops/tickets/2026-07-25-p0-ai-group-hard-hourly-blockers.md`
