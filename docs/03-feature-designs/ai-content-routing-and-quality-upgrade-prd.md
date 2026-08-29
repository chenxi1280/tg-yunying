# AI 活群上下文路由、成人方向与多 Provider 内容质量升级 PRD

## 0. 文档状态与真相源边界

| 项 | 结论 |
| --- | --- |
| Intake ID | `intake-2026-08-18-ai-content-routing-quality-001` |
| 问题级别 | L2 / P1，影响生产内容质量，必须走 Release Gate |
| 文档版本 | v1.2 business-outcome remediation |
| 设计状态 | `product_design_complete / pending_user_acceptance` |
| 实现状态 | `implementation_in_progress / local_qa_partial`；仅关闭态基础能力已实现，生产开关与业务效果仍未验收 |
| 适用范围 | Phase 1：`group_ai_chat`；Phase 2：`channel_comment` 复用公共能力后独立验收 |
| 上位合同 | `hourly-random-pacing-and-ai-humanization-prd.md` 的节奏、MessageBrief、voice、质量闸和 E4 边界继续有效 |
| 本文优先级 | 本文补正上位合同中 AI 内容的 task direction、context route、window mode、短 Prompt、多 Provider 与评测细则 |
| 生产结论 | `shadow_generation_pass / reviewer_comparison_unproven / production_fixed=unproven` |

v1.2 在 v1.1 上补齐拟人发送与达量的共同合同：使用真实任务字段和单调上下文 revision，修正 WindowPlanSlot pre-Gateway replacement，引入逐 slot 生成/替代与 typed shortfall，并明确 current v2 禁止固定“签到”或其他静态内容补量；仍不代表代码已实现或线上已修复。

本文只定义产品与技术合同，不授权修改线上 Provider、任务配置、数据库或发送 Telegram。任何实现都必须沿用 current 履约链，不得用独立脚本或旁路发送器替代：

```text
stable obligation
  -> assignment / immutable intent
  -> GenerationJob
  -> accepted candidate + message memory
  -> ready Action
  -> Dispatcher / Gateway
  -> ExecutionAttempt
  -> typed remote message fact
```

`GenerationJob` 仍是内容生成 owner；新增 window plan 只协调内容，不拥有数量、不结算履约、不直接创建远端副作用。

---

## 1. 原始需求与成功定义

### 1.1 用户需求

1. AI 活群和 AI 评论不能继续输出明显 AI 腔、模板句和语境错位内容。
2. 活群目标是整个窗口自然，不是每一条都成人化；普通上下文必须继续普通话题。
3. 任务方向和真实上下文明确指向成人视觉、成人用品或成年性服务时，允许相应成人表达。
4. 成年性服务“老师”语境不能只剩“多少钱、在哪里”；在上下文支持性感、身体或性暗示时，需要出现“好润”“水多不？”这类短、粗粝的感官互动。
5. “老师”单词本身、课程、夜课和普通群聊不能触发成人方向。
6. MiMo、DeepSeek、MiniMax 等 Provider 应能按用途同时配置和排序；脚本可调用不等于生产已有该能力。
7. 先形成方案和 PRD，确认后再开发、灰度和上线。

### 1.2 产品成功定义

- 主题由任务授权和真实上下文共同决定，账号面具只改变表达方式。
- 同一窗口可同时包含 general、成人询问和成人感官互动，但不得按固定成人比例污染上下文。
- 成人感官 mode 能稳定生成可直接发送的短句，不把“润/湿”错误地修饰到丝袜、衣物等对象上。
- general 路由不出现成人服务、交易或感官强转；年龄不明、未成年人或高风险内容必须 silence/quality_wait。
- Prompt 按 mode 分流，单个 Realizer 不再承载所有场景的长规则。
- Provider 选择、切换和失败均有显式 purpose、priority、revision、attempt 与错误码；不静默降级。
- 候选通过确定性硬闸和独立在线 reviewer 后才能成为 ready Action；每个 quantity slot 独立重试/替代，耗尽预算后只为该 owner 结算一次 typed `quality_shortfall`。
- 发布验收以真实 Telegram typed remote fact 和盲抽内容质量共同完成；健康、Action success 或 shadow pass 不能替代。

### 1.3 非目标

- 不把成人内容开关做成全租户默认放开。
- 不根据账号面具、账号昵称或单个弱词推断成人主题。
- 不在 Provider 返回后用模板、字符串替换、emoji、随机短句或固定“签到”改写正文或冲抵 quantity owner。
- 不把“好润”“水多不？”写成所有成人消息的固定模板。
- 不把 transport fallback 当作内容质量修复；有效但低质的候选不能换模型重试到“评审通过”。
- 不在 Phase 1 同时重写频道评论 reply authority、AI 数量 owner 或 Dispatcher/Gateway。
- 不用本次 8-case shadow 代替 120+ 分层样本评测、canary 或生产 E4。

---

## 2. 当前证据与根因

### 2.1 真实生产形态 shadow 结果

本轮脚本从真实成功 `group_ai_chat` Action 获取上下文，按 `account_id` 读取 active `AiAccountVoiceProfile`，使用线上 Provider 凭证生成；全程不写数据库、不发 Telegram、不打印源上下文或生产基线。

可复现证据入口为 `backend/scripts/evaluate_ai_group_mixed_shadow.py`；下表来自相同真实 8-case Action/voice 窗口的分阶段运行，不代表生产发送链已接入该脚本。

| 阶段 | 真实结果 | 结论 |
| --- | --- | --- |
| 任务方向分类 | 同一任务在 MiMo 上出现 `0.9 / 0.8 / 空方向` 漂移；真实配置多次明确包含“楼凤/楼凤阁” | runtime LLM 不应成为成人方向授权真相源 |
| 长混合 Prompt | 6 general + 2 adult_service，但成人两条均为 inquiry：“郑州哪里？”、“上门的话怎么收费？” | 窗口缺少 inquiry/sensory 多样性 |
| 增加 sensory mode 后 | 生成“丝袜什么颜色的”或“丝袜看着好润” | mode 生效，但长 Prompt 忽略局部对象约束 |
| sensory 短 Prompt 隔离 | 同一真实 8-case 窗口连续两次输出“水多不？”；另一成人 case 为“多少钱一次” | 人工批准表达和 deterministic gate 连续两次通过 |
| 窗口质量 | 两次均为 6 general + 2 adult_service，0 duplicate、0 gate failure、0 general forced-adult | context-first 与窗口多样性通过小样本 shadow |
| 独立评审 | reviewer 曾返回 HTTP 429；最终两次显式 generation-only | 相对生产 baseline 的 reviewer 结论仍为 `unproven` |

### 2.2 当前代码已经具备的能力

| 当前模块 | 已有能力 | 本次复用方式 |
| --- | --- | --- |
| `ai_generation_pipeline.py` | 单阶段/两阶段分流、逐 slot 生成、一次定向重生成、`quality_wait`、结构指纹 | 在现有两阶段分支内升级，不建旁路 |
| `two_stage_generation.py` | Brief Planner、单 slot Voice Realizer、在线 semantic reviewer、证据与置信度 | 前置 route/window plan，并按 mode 选择短 Prompt |
| `message_brief.py` | MessageBrief v1、fact anchor、voice contract v3、结构塌缩与 schema 校验 | 升级为 MessageBrief v2，增加 route/mode/policy snapshot |
| `ai_group_prompt.py` | general 安全清洗和旧单阶段固定 JSON contract | 保留 legacy；拆出全局安全清洗与 route-scoped facts |
| `GenerationJob` | context/assignment/intent/policy version、candidate hash、evaluator evidence | 增加内容策略、window、route、mode、Prompt 与 Provider route 冻结字段 |
| `ai_quality_evaluation.py` | position-swap、context-cluster bootstrap 95% CI | 作为离线 Release Gate 基础 |
| `voice_contract_v3` | 句长、提问、emoji、语气词、强势度、幽默、热度 | 继续只控制 surface voice |

### 2.3 当前架构缺口

1. `ai_group_prompt.FORBIDDEN` 和 `message_brief.fact_id_map()` 会删除价格、位置、预约、服务和“楼凤”等语义；任务即使允许 adult_service，Brief Planner 也看不到事实。
2. MessageBrief v1 没有 `task_direction_snapshot`、`context_route`、`content_mode`、`route_evidence_ids` 或 `prompt_contract_version`。
3. 一个长 Realizer Prompt 同时承载 general、成人视觉、成人用品、成人服务和声线规则；模型会抓住“丝袜”等显眼词而忽略“不要错误修饰”的局部约束。
4. 批次结构只检查 speech act/长度/标点，没有在 adult_service 内平衡 inquiry/sensory。
5. 当前 `AiProvider` 有 `uq_ai_provider_single_active`，没有 tenant+purpose+priority 路由；禁用的 MiMo 只能脚本直调，不能作为正式生产 route。
6. 在线 reviewer 是单候选 direct gate；离线 pairwise 工具存在，但没有冻结真实分层评测集、人工 ground truth 与持续监控闭环。
7. 当前 UI 只配置生成模型、两阶段开关和 reviewer 模型，不显示成人方向授权证据、route/mode 结果或 Provider purpose 顺序。
8. 旧 Prompt 中“所有被提到的人都是成年人”只是生成指令，不能证明真实对象成年；若把它当年龄证据，会绕过任务授权和上下文安全边界。
9. 当前 `system_prompt_override` 与 `slang_prompt_template_id` 解析出的模板正文可注入大段自由文本；v2 若继续允许其替换系统合同，会绕过 route、安全、schema 和短 Prompt 隔离。

### 2.4 根因结论

问题不是“只要换模型”或“只要缩短一句 Prompt”。根因是四个边界混在一起：

```text
任务是否允许成人方向
  × 当前上下文是否支持该方向
  × 同一窗口该用什么互动模式
  × 该模式如何用账号声线实现
```

生产升级必须先确定性冻结前三项，再让 LLM 只完成它适合的语义路由、表达和主观审核。

---

## 3. 产品核心决策

### 3.1 任务方向授权不是 runtime LLM 决定

任务创建/编辑时生成“内容方向预览”，来源包括：

- `topic_directions`；
- `teacher_targets`；
- 任务规则集和人工选择的 `allowed_content_routes`；
- 当前 approved `AiContentPolicyVersion`。

系统可用模型提供建议，但保存时必须形成确定性、可审计快照：

```json
{
  "task_config_revision": 12,
  "content_policy_version": "ai_content_policy_v2",
  "allowed_routes": ["general", "adult_service"],
  "adult_subject_attestation_id": "attestation-uuid",
  "adult_subject_class": "adult_service_provider",
  "evidence_codes": ["operator_verified_adult_service_scope"],
  "evidence_hash": "sha256",
  "approved_by": "operator"
}
```

规则：

- `general` 永远存在。
- “楼凤”等明确成年性服务主题可在预览中建议 `adult_service`，但最终以保存的 allowlist 为准。
- “老师”“妹子”、课程、夜课等弱词不能单独授权 adult route。
- 成人方向必须引用有效的 `AdultSubjectAttestation`，不能只保存布尔值。证明记录至少包含 `tenant_id`、`scope_type=task_group|task_source`、`scope_id`、`subject_class=adult_service_provider|adult_visual_subject|adult_product_context`、版本化 `evidence_codes`、`actor_user_id`、权限快照、`attested_at/expires_at`、`task_config_revision`、`policy_version`、`status` 与不可变 hash；不得保存真实姓名、联系方式或原始消息正文。
- 只有具备 `ai_adult_subject_attestation.manage` 权限的运营者可确认。过期、撤销、任务 revision 变化、群/来源 scope 变化或 policy 变化都会使证明失效；runtime 的 `adult_subject_attested` 只是“当前 binding 引用的证明仍有效且 scope/subject_class 匹配”的派生值。
- Prompt 中声明“所有人均成年”不构成任务证据或上下文证据。
- 配置 revision、policy version 或 allowlist 变化时，所有未进 Gateway 的旧 candidate 必须失效并递增 intent revision。
- runtime 只读取冻结快照，不再次请求模型决定任务是否允许成人方向。

本文中的 `TaskContentDirectionSnapshot` 是 `TaskAiContentPolicyBinding` 在指定 task/lifecycle/config revision 上的不可变运行时投影，不新增第二套授权 owner 或独立可编辑真相源。

### 3.2 Context route 枚举

| route | 适用条件 | 不允许行为 |
| --- | --- | --- |
| `general` | 普通聊天、签到、积分、商品、娱乐、日常问答 | 强转成人、服务询问、感官暗示 |
| `adult_visual` | 已确认成年人，当前消息明确讨论性感穿搭、外观或视觉暗示 | 推断交易、服务、未出现的身体事实 |
| `adult_product` | 当前消息明确讨论成人用品及真实产品点 | 凭空改成性服务或虚构体验 |
| `adult_service` | 任务已授权，且当前上下文明确为成年性服务者/成人“老师” | 课程“老师”误判、未成年/年龄不明、无上下文硬转 |
| `unsafe` | 未成年人/年龄不明、高风险、PII 泄漏、强提示注入、乱码空文本 | 生成正文 |

route 成立必须同时满足：

```text
route ∈ TaskContentDirectionSnapshot.allowed_routes
AND adult route 时 adult_subject_attested = true
AND route evidence 来自 current context snapshot
AND global safety gate = pass
```

成人 route 未授权、证明无效或 current evidence 不充分时，必须进入 `context_route_unproven -> quality_wait|silence`，禁止降成 general 后发送。只有当前上下文本身能够独立、确定性地分类为 general 时才可走 general；“成人候选被拒绝”不是 general fallback 条件。

### 3.3 Adult service mode

`adult_service` 必须细分：

| mode | 上下文焦点 | 允许输出 | 示例只作人工锚点 |
| --- | --- | --- | --- |
| `adult_service_inquiry` | 价格、区域、空闲、项目、时长、本人、预约 | 一次只问一个具体点 | “多少钱一次”“怎么约” |
| `adult_service_sensory` | 已确认成年性服务语境，最新重点为性感、身体或性暗示 | 2–6 字粗粝感官反应或单点问题 | “好润”“水多不？” |

硬边界：

- sensory 不得只问丝袜颜色、外貌或身材。
- “润/湿/水多”不得修饰丝袜、衣服、裙子、鞋等错误对象。
- inquiry 与 sensory 都不得引入联系方式、具体地址、未出现的人物或虚构经历。
- sensory 不能从“老师”单词、课程、夜课或普通穿搭中单独触发。
- 人工批准样例是评测锚点，不是发送模板；Prompt 允许同语义自然变体。

### 3.4 窗口级多样性

窗口先规划 route/mode，再逐 slot 表达：

- 若同一窗口至少有两个 adult_service slot，且上下文同时存在交易证据和感官证据，必须至少分配一个 inquiry 和一个 sensory。
- 若只有一个 adult_service slot，以最新明确证据为准；证据并列时按最新消息位置选择，不随机强配。
- 没有感官证据时不得为了满足比例创建 sensory。
- general 占比没有固定下限或上限，服从真实上下文。
- 无真人 active thread 时，只有任务配置里已批准的成人事实锚点才能创建最多一个成人 seed；`adult_service_sensory` 永远不能无上下文 seed。
- window plan 只协调内容，不改变账号、数量、due time 或 reply authority。

### 3.5 账号面具边界

账号面具只控制：

- 长度档位；
- 提问/陈述倾向；
- 标点、语气词、emoji；
- 强势度、幽默、热度；
- 禁用表达。

账号面具不得：

- 授权 adult route；
- 把 general 改成 adult；
- 引入价格、地点、服务、人物关系或经历；
- 在生成后重写 Provider 正文。

### 3.6 频道评论范围

Phase 2 复用以下公共能力：policy version、全局安全清洗、route-scoped facts、mode Prompt registry、Provider route、质量门和离线评测。

频道评论仍独立遵守：

- source post 与 reply target authority；
- same tenant/Task/source/plan revision；
- own-history remote fact；
- 评论长度、引用和敏感策略。

没有独立真实 comment shadow、120+ 分层评测和 canary 前，频道评论只能标记 `design_ready / production_unproven`。

---

## 4. 目标数据流

```mermaid
flowchart LR
    A["Stable obligation / immutable intent"] --> B["GenerationJob claim"]
    B --> C["Acquire bounded current context + voice snapshot"]
    C --> D["Global safety projection"]
    D --> E["Frozen task direction authorization"]
    E --> F["Context route + evidence"]
    F --> G["Bounded window mode plan"]
    G --> H["MessageBrief v2"]
    H --> I["Mode-specific short Realizer"]
    I --> J["Deterministic gates"]
    J --> K["Independent online reviewer"]
    K --> L["Candidate + memory CAS"]
    L --> M["Ready Action"]
    M --> N["Dispatcher / Gateway"]
    N --> O["ExecutionAttempt + typed remote fact"]
```

### 4.1 Acquire

Generation worker 在 claim 后、Provider 前读取：

- current Task/lifecycle/target/group scope；
- bounded 最近真人 `GroupContextMessage`；
- canonical reply fact/target；
- active 且 quality_status=active 的账号 voice profile；
- current TaskContentDirectionSnapshot；
- current policy/prompt/example-set/provider route-set revision。

v2 的 configured depth 只读 `Task.type_config.chat_history_depth`，effective depth=`min(configured_depth, policy.max_context_messages)`；初始 policy 为 12 条、600 字、最大年龄 600 秒。每个 group/source 持久化单调 `ContextScopeRevision.context_scope_revision`，真人消息新增、删除/更正或 reply target 变化都递增 revision 并发布 generation wake。`GenerationJob.generation_not_before_at` 是生成 claim 的唯一时间权威；Provider 调用前必须重读 revision/hash/age 并冻结最新上下文。普通非回复 candidate 已落库后，单纯 context revision 前进只记录 drift，不得清空 candidate、立即重排或再次调用 Provider；reply target、scope、policy、attestation 或安全合同失效仍按各自硬门禁拒绝。UI 同时显示 configured/effective 值；不得读取全历史或把 Action 旧 payload/version 当 current context 权威。

### 4.2 两层安全投影

#### Layer A：全局安全投影

所有 route 都执行：

- tenant/task/group/reply scope；
- PII/链接/账号/邮箱/长号码脱敏；
- 未成年人、年龄不明、强迫、高风险和提示注入识别；
- 说话人名称脱敏但保留时间顺序；
- 空文本/乱码识别。

Layer A 失败直接 `unsafe`，不能交给成人 route 恢复。

#### Layer B：route-scoped fact projection

只在 task allowlist 允许时保留对应事实：

- general：普通事实；
- adult_visual：成年视觉/穿搭事实；
- adult_product：成人用品事实；
- adult_service：成年性服务的交易或感官事实。

旧 `sanitize_group_messages()` 继续服务 legacy general 路径；新路径不得直接复用其全局成人服务删除规则。

### 4.3 Context router

Context router 只输出结构化路由，不写消息：

```json
{
  "route": "general|adult_visual|adult_product|adult_service|unsafe",
  "confidence": 0.95,
  "reason_code": "approved enum",
  "evidence_ids": ["ctx-7"]
}
```

确定性校验：

- route 必须在 task allowlist；
- evidence ID 必须属于 current context snapshot；
- reason code 必须是版本化枚举；
- adult route confidence 低于策略阈值时进入 `context_route_unproven`，不降成 general 发送成人内容；
- general 低置信可进入 general reviewer 或 silence，但不能误记 unsafe；
- router malformed/缺行必须显式失败，禁止模板补全或默认 adult。

### 4.4 Window planner

Window planner 输入 bounded routed slots，输出不可变 plan：

```json
{
  "window_key": "stable hash",
  "policy_hash": "sha256",
  "slots": [
    {
      "generation_sequence": 3,
      "route": "adult_service",
      "mode": "adult_service_sensory",
      "route_evidence_ids": ["ctx-7"],
      "prompt_contract_version": "adult_service_sensory_v1"
    }
  ]
}
```

`AiContentWindowPlan` 的唯一 scope 固定为：

```text
(tenant_id, task_id, task_lifecycle_epoch, group_id/source_scope_id,
 pacing_plan_hash, period_key, window_start, window_end,
 task_config_revision, content_policy_hash)
```

群聊使用 `group_id`，频道评论使用 `source_scope_id`，数据库 check constraint 要求二者恰有一个非空；规范化 unique key 必须包含 `scope_type+scope_id`，不能把 NULL 当成可重复窗口。

plan 从现有 stable obligations、已冻结 assignment 和 due slots 建立成员关系；它不创建数量、不改 `due_at/account_id/reply authority`。plan 冻结后不得追加或重排成员。迟到 obligation 进入下一 plan；若已无下一窗口，则按同一确定性规则创建只含该 obligation 的 standalone plan，不能修改 frozen plan。

每个成员落为独立 `AiContentWindowPlanSlot`：

- Job 与 Action 的发送前状态必须原子对齐；`Job.pending + Action.pending + payload.generating` 会被领取谓词永久排除，不是合法等待态。
- 历史/跨事务中断自愈按安全事实而非 `generation_stage` 名称判断：只处理 exact Job binding、Job/Action/payload 均无 owner/token、Provider 未开始的 pending 行，将同一 Action payload CAS 回 `pending`；任何 Provider/Gateway 边界、unknown 或 owner 漂移均零写。

```json
{
  "plan_id": "uuid",
  "slot_ordinal": 3,
  "slot_revision": 1,
  "obligation_type": "group_ai_chat",
  "obligation_id": "uuid",
  "generation_sequence": 3,
  "account_id": "uuid",
  "due_at": "2026-08-18T10:23:00Z",
  "context_snapshot_version": 9,
  "context_snapshot_hash": "sha256",
  "route": "adult_service",
  "mode": "adult_service_sensory",
  "route_evidence_hash": "sha256",
  "prompt_contract_version": "adult_service_sensory_v1",
  "state": "frozen",
  "claimed_by_job_id": null,
  "lease_epoch": 0,
  "lease_expires_at": null,
  "version": 1
}
```

`(plan_id, slot_ordinal, slot_revision)` 永久唯一；每个成员位/obligation 最多一个 current slot，current predicate=`frozen|claimed|candidate_ready|gateway_bound`。状态为 `frozen -> claimed -> candidate_ready -> gateway_bound -> settled`。Provider 调用前的 `claimed` slot 可因 context revision 前进 CAS 到 `invalidated` 并创建递增 revision 的替代 slot；`candidate_ready` 不再因普通 context drift 失效或重生成，`gateway_bound` 后只能 reconcile。scope、policy、attestation、安全或 reply authority 硬合同失效仍可在 Gateway 前拒绝。claim 持有 job、lease epoch/expiry，超时只允许同 revision reclaim。candidate persist 进入 `candidate_ready`，不能提前写 consumed；窗口其他 slot 不重算，Window planner 不调用 Telegram、不写 Action。

GenerationJob 在 Gateway 前因质量/Provider 容量等待超过预算或 deadline 形成 typed shortfall 并终结为 `failed|cancelled` 时，必须在同一事务把该 Job 持有的 `claimed|candidate_ready` slot CAS 为 `invalidated`、清空 `claimed_by_job_id/lease`，再允许同一 obligation 的替代 Job 建立 current slot。历史版本遗留的精确形态 `slot.current + claimed_by_job.state IN (failed,cancelled)` 由 ai-generation reconcile 按有界批次和行锁自动失效；`gateway_bound`、open/unknown Job 或归属漂移一律不进入自愈。数据库 current-obligation 唯一约束始终保留为最终并发闸门，禁止通过放宽唯一性、删除旧事实或把唯一冲突降级成假成功恢复吞吐。

### 4.5 MessageBrief v2

在 v1 基础上新增：

```json
{
  "brief_contract_version": "message_brief_v2",
  "task_direction_snapshot_hash": "sha256",
  "content_policy_hash": "sha256",
  "window_plan_hash": "sha256",
  "context_route": "adult_service",
  "content_mode": "adult_service_inquiry",
  "route_evidence_ids": ["ctx-7"],
  "claims": [{
    "category": "availability_question",
    "speech_act": "question",
    "evidence_ids": ["ctx-7"]
  }],
  "forbidden_claim_categories": ["price_assertion", "location_assertion"],
  "prompt_contract_version": "adult_service_inquiry_v1",
  "example_set_version": "adult_human_anchors_v1"
}
```

原有 slot、anchor、stance、length、punctuation、reply 和 voice version 保留。每个 `claims[]` 项必须直接绑定 category、speech act 与 current evidence IDs，禁止用平行数组猜测对应关系；任何 hash/version 不一致均为 schema/scope 硬失败。

`adult_service_inquiry` 只允许 `price_question`、`region_question`、`availability_question`、`service_question`、`duration_question`、`identity_question`、`booking_question`，且必须是问题形式并锚定同类别 evidence。对应断言、精确金额/地址/联系方式、人物关系或无证据细节一律禁止。v1 的 substring marker 保留在 legacy 路径；v2 不全局放宽 sanitizer/marker，而是按 route、speech act、claim category 和 exact evidence ID 判定。

### 4.6 Mode-specific Prompt registry

Prompt 按 purpose/mode 版本化，不再给每个 Realizer 注入所有场景规则：

| Prompt contract | 输入 | 核心输出约束 |
| --- | --- | --- |
| `general_v2` | 一个 brief、1–2 个普通事实、slim voice | 自然短反应/具体追问，禁止成人强转 |
| `adult_visual_v1` | 明确成年视觉事实、slim voice | 短、直、感官；不得虚构服务/交易 |
| `adult_product_v1` | 一个真实产品点、slim voice | 只接一个产品事实 |
| `adult_service_inquiry_v1` | 一个交易事实、slim voice | 只问价格/区域/空闲/项目/时长/本人/预约之一 |
| `adult_service_sensory_v1` | 一个感官证据、slim voice | 2–6 字感官反应或问题；可生成“好润/水多不？”等自然变体，禁止错误对象和模板复读 |

Realizer 每次只处理一个 slot。Prompt 输入不包含其他 mode 规则、完整面具文本或完整历史；Provider 必须返回固定 JSON schema。

现有 `system_prompt_override` 与 `slang_prompt_template_id` 解析出的模板正文在 v2 中不得直接进入 system contract。它们只能选择已批准、不可变、版本化的结构化 `style_overlay`：`length_band`、版本化 `particles_allowlist`、`punctuation_profile`、`emoji_rate_band`、`forbidden_expressions`；拒绝任何自由指令字段。初始 policy 固定 `max_style_overlay_chars=200`，后续只能通过新 policy version 和同一评测集修改。`system_prompt_override` 永不复制进 overlay。route 规则、安全规则、输出 schema、事实锚点和 Provider purpose 均不可覆盖。legacy 任务迁移时必须同时展示“配置值”和“实际生效值”，不能静默截断后宣称已兼容。

### 4.7 生成与定向重试

- 每个 slot 独立生成；一个 slot 无合法时间、Provider 或候选时不得取消同批其他合法 slot。初次失败后最多按 rejection code 定向重生成一次。
- 同一重试必须冻结 task binding、context revision/hash、route、mode、brief、voice、prompt、example set 和 provider route-set revision，并满足 `generation_not_before_at <= now < latest_safe_send_at`。
- rejection feedback 只传失败码和短说明，不回灌旧完整候选。
- Provider 调用前的 route/mode/context/policy 变化不是重试，必须终结旧 GenerationJob、递增 intent revision 并新建 current job；普通非回复 candidate 已持久化后的 context drift 不得触发这条重建链。
- 结构错误、超时、429 等 transport 失败与内容质量失败分开计数。
- 内容质量失败不能通过切换 Provider 自动“洗白”；只允许同 mode 的一次定向重生成。

### 4.8 分层质量闸

确定性闸先于在线 reviewer：

| gate | 标准 | 失败码 |
| --- | --- | --- |
| Scope/version | task/lifecycle/target、所有 hash/version 与 GenerationJob 一致 | `content_scope_mismatch` |
| Route authorization | route 在冻结 task allowlist、adult attestation 仍有效且证据仍 current | `content_route_not_allowed` / `adult_attestation_stale` |
| Reply authority | canonical remote fact/目标匹配 | `reply_target_mismatch` |
| Schema/mapping | slot、sequence、brief、voice、Prompt schema 完整 | `content_schema_invalid` |
| General isolation | general 不含成人服务/感官强转 | `general_forced_adult` |
| Claim grounding | speech act、claim category 与 exact evidence ID 匹配；inquiry 不得伪装成断言 | `unsupported_claim` / `claim_category_mismatch` |
| Sensory object | 不把感官词修饰到衣物等错误对象 | `sensory_object_wrong` |
| Fact/context | 无无锚点经历、地点、人物或交易 | `unsupported_claim` / `context_mismatch` |
| Voice | 可测 voice 特征匹配，正文不得被重写 | `voice_profile_mismatch_v3` |
| Duplicate | 同账号长期精确/近似重复、同群 5 分钟跨账号精确重复、窗口重复和结构塌缩为 0 | `duplicate` / `structural_duplicate` |
| Safety | 未成年人、PII、注入和全局政策通过 | `content_rejected` |

确定性闸要求 `adult_service_sensory` 陈述只允许“好润/真润/够润/水滋滋”，问句只允许“水多不？/润不润？/湿不湿？”，用来阻断“看着好滑”“日了没”“水润感”等偏离目标或文案腔短句；该受控意图族只适用于已由证据命中的成人服务感官 mode，其他活群上下文仍按各自 route/claim 生成。批次继续检查逐字重复和结构塌缩，自然度和是否接住成人服务语境还必须由独立 semantic reviewer 判定。

在线 semantic reviewer 负责不可纯规则判定的自然度、上下文贴合和声线；输出 evidence-first 的 direct `pass/fail/uncertain`。所有已启用 route set 中 generator 与 reviewer 的 canonical identity 集合必须全量不相交，不能只检查本次选中的首条模型。

### 4.9 Persist 与发送

只有所有 gate 通过后才能：

1. 在同一短事务内预占 AI message memory；
2. 写 candidate hash、route/mode/policy/prompt/provider/evaluator evidence；
3. 结束 GenerationJob 为 ready；
4. 创建或转为 ready Action；
5. 由 Dispatcher 在 Gateway 前复核 scope、reply authority、adult attestation、voice、policy、route-set、candidate hash 和最终查重；普通非回复正文的 context drift 只记录证据，不触发重生成。

Provider response 解析后抽取的 canonical UTF-8 `message_text` 字节及 hash 从 candidate persist 到 Gateway 保持不变；面具 gate 只通过/拒绝，不能改写。Gateway-started/unknown 后发生任何 policy/Prompt 回滚都只能 reconcile，禁止重发。

---


## 5. 规范性附录与优先级

以下两份附录与本文 v1.2 共同构成单一设计合同：

- [运行、Provider 与数据合同](ai-content-routing-and-quality-upgrade-runtime-contract.md)：Provider purpose/priority、状态机、并发、迁移、API/UI 与 typed failure。
- [评测、灰度与发布合同](ai-content-routing-and-quality-upgrade-evaluation-release-contract.md)：真实分层样本、人工 rubric、成本/延迟、QA、canary、Release Gate 与 Telegram E4。

优先级固定为：本文业务目标与 route/mode 语义 > 运行附录的实现合同 > 评测附录的验收合同。附录不得改变 stable quantity owner、业务 `due_at`、Gateway unknown 或 typed remote fact 的上位语义。

## 6. 2026-08-19 实现快照

- 已完成本地 additive schema 与 migrations `0155`/`0156`，覆盖 policy/证明/binding/context、window/slot、purpose route、attempt、typed shortfall 和 source capacity。
- 已实现关闭态的 MessageBrief v2、mode 确定性闸、Provider 显式顺序与 typed transport fallback；普通内容不得被成人强转，成人服务 sensory 可接受有证据的“好润/水多不？”等短句，但弱词不能授权。多 route task 先按当前 evidence 做确定性分类；唯一命中才冻结 route，成人证据模糊或多类冲突进入 `context_route_unproven`。
- 已把 Provider 凭证可用性与 legacy `is_active` 分离并补配置 UI；当前仍保留 single-active，未开启 route-set production business read。
- 已把 task revision/target scope/policy version、window slot、purpose route snapshots 与 Provider attempt 接到显式 v2 GenerationJob；job 首次冻结 router/realizer/reviewer 全 purpose route，后续 stage/retry 不再读取 active revision。source aggregate capacity 已接入 AI/浏览/评论/点赞 Planner 的 feature-flag 路径，保留业务 due，仅推进 `release_not_before_at` 并把 plan hash/ordinal 写回真实 owner；deficit 或 immutable scope 冲突显式阻断。
- 正式任务 schema 与创建/编辑页已开放显式 `ai_content_route_v2_enabled`、`ai_content_policy_version_id`、`ai_content_allowed_routes`、`ai_content_attestation_ids`。启用时必须同时开启两阶段生成；保存事务会校验 active policy、全部 purpose route-set 和成人 scope attestation，并冻结当前 task revision binding。任一依赖缺失均在保存前失败，禁止形成“保存成功、worker 才失败”的半配置状态；`ai_content_context_route` 仍不公开，route 必须按 current evidence 逐上下文判定。
- `ContextScopeRevision` 已进入 GenerationJob 消费链；2026-08-24 紧急补正后，只允许 Provider 调用前因 revision/hash 前进而重绑上下文。普通非回复 candidate 已持久化后只记录 drift，禁止清除 candidate/window、立即重排和重复消耗 Token；Gateway-bound 后仍不允许改写。
- `quality_wait` 已改为 pending GenerationJob/Action 并按任务 retry delay 重试，不再当普通生成失败释放数量义务；`latest_safe_send_at` 从账号 pacing reservation、履约 projection 或显式 deadline 冻结。下一次重试越过截止时写唯一 `FulfillmentShortfallFact`，Provider capacity 与 quality 分别结算 typed shortfall，并将 FOP、评论义务或 AI content-mix/quantity owner 同事务转为 terminal shortfall，禁止截止后再次重规划补发。
- 单一成人 allowed route 不再等同于当前内容已命中成人语境；没有 current markers、多个成人 route 同时命中或弱词无法唯一分类时均返回 `context_route_unproven`。
- 2026-08-19 生产数据 shadow 复核补正：MiMo 首轮虽通过旧 reviewer，但出现“顶/好顶”语义复读及“这水光/看着好滑”等未达到成人服务感官口径的候选；另一混合场景直接 `candidate_rows_missing`，均不得算质量通过。current v2 因此把“软软的/水灵灵的/好心动/挺好看的/这状态真不错”设为成人 route 确定性失败，成人服务 sensory 必须命中批准的润/水多/水滋滋/湿问句意图；同一冻结上下文同时具备服务询问与感官证据时，按 stable slot ordinal 在 inquiry/sensory 间交替，避免同批所有账号塌成同一 speech act。Realizer 与 reviewer 共用该简短口径，但 reviewer 仍必须是独立 Provider。
- 2026-08-19 已部署 `319a769b` 后，用线上真实任务上下文、20 个 active voice profile 与线上 MiMo 凭据执行零写入 shadow：sensory 产出两次“水多不？”及“嫩不嫩 水多不？”，同时暴露 reaction 问句漂移、“日了没”意图漂移和 inquiry 寒暄/长度不匹配。`3e1febda` 上再次用真实面具直连 MiMo，4 个冻结 brief 得到“水润感/水多不？/价格多少？/有什么项目”，其中“水润感”被旧闸误放行、服务问句因漏问号被结构闸拒绝。Realizer 因此把 sensory 收口到批准短句集合，并要求全部 service inquiry 以问号结尾；所有失败仍由 contract/deterministic gate 拒绝，不得直接发送。
- 尚未完成 shadow runner 的 120+ 人工样本验收、canary、生产发布和 Telegram E4；当前本地测试不能证明线上内容质量或任务量达标。

当前状态为 `product_design_complete / implementation_in_progress / local_qa_partial / production_fixed=unproven`。

## 7. 2026-08-24 生产低质重复 P1 Product Resync

### 7.1 连续窗口证据与根因

生产 SHA `cd91429356d623197a03e51152e67db7da194f31` 已包含本文既有实现 commits `91272da0`、`319a769b`、`3e1febda`、`eeb4cd5f`，质量代码没有在合并中丢失。但 7 个运行 `group_ai_chat` 的 `ai_two_stage_enabled` 与 `ai_content_route_v2_enabled` 均等效为 false，active policy/binding/window plan/provider attempt/reviewer evidence 均为 0。

每任务最近连续 100 条成功 typed remote fact、合计 700 条的脱敏只读诊断发现：19 条精确重复逸出、272 条存在确定性近似重复、168 对跨任务近似重复、约三分之一为“签到”模板族、95.6% 集中在短句、99% 集中为陈述式开头；700 条 reviewer evidence 均缺失。`remote_message_id` 证明发送成立，不证明正文质量；远端正文与 candidate 的受控一致性仍 `unproven`。

第一坏边界是生产任务未激活 V2，而不是单一 Provider。legacy static fallback 与旧质量闸允许模板、结构塌缩和签到变体进入 ready Action；模型切换只能改善运输选择，不能替代 policy、MessageBrief、voice、去重、独立 reviewer 和 quality_wait。

### 7.2 单任务 canary 激活硬合同

- `ai_content_route_v2_enabled=true` 必须同时满足 `ai_two_stage_enabled=true`；保存事务在任何 policy/binding 写入前拒绝半配置。
- active policy、task binding、allowed routes、全部 purpose route snapshots、MessageBrief v2、voice contract v3 与独立 reviewer canonical identity 必须在同一 task revision 冻结。
- V2 runtime 必须把 tenant legacy static fallback 视为 false；不得进入 due-catch-up check-in pipeline，不得产生精确“签到”、签到变体、Stage 1 正文、emoji fallback 或其他静态补量。
- reviewer 不可用、429、route unavailable、fail 或 uncertain 均保持同一 GenerationJob/Action 为 `quality_wait`，达到 latest-safe 后写 typed shortfall；绝不转 ready Action。
- generator 与 reviewer 的全部 canonical provider/model identity 集合必须不相交；只看首候选不同不够。
- 只允许显式选中的一个 Task revision 作为 canary；实现和发布不得自动批改或同时打开现有 7 个生产任务。

### 7.3 开发与 QA Handoff

| 层 | 最小实现/验证 |
| --- | --- |
| activation | `route_v2=true/two_stage=false` 保存失败且零 binding；完整配置继续冻结 policy/binding |
| runtime | V2/two-stage 下 tenant static flag 不进入普通 fallback或 due-catch-up pipeline |
| MessageBrief/voice | 每个 job 有 v2 brief、context/policy/prompt/example 与 voice snapshot |
| reviewer | canonical identity 独立；不可用/429/fail/uncertain -> quality_wait，无 ready Action |
| deterministic gates | 精确/近义/结构重复、相同开头/speech act/length collapse、签到/emoji/Stage 1 均拒绝 |
| 兼容 | flag-off legacy 行为不在本提交中迁移；频道评论按 Phase 2 独立验收 |

Release Gate 仍执行不少于 120 条分层离线评测；单任务生产 canary 至少连续 3 天、100 条 typed remote fact、30 条盲审、3 个上下文簇与 10 个 voice 账号。每条发送事实必须可追到 task/config revision、ledger/slot、GenerationJob、policy/brief/voice/provider/reviewer snapshot、Action、Attempt/Gateway、typed remote ID 与受控正文 hash。切换 provider、route readback、发送数量和五类任务履约均不能替代质量验收。

Product Design Complete 自检：用户截图症状、连续窗口、设计激活缺口、配置/UI、后端 activation/runtime、生成与 reviewer、并发/冻结、quality_wait、静态旁路、兼容边界、QA/canary/E4 均已覆盖。当前只授权本地实现与测试，不授权生产任务开关、重生成、补发、配置 apply 或发布；`production_fixed=unproven`。

### 7.4 受保护 policy/bootstrap 与单任务 canary 配置合同

新增 `ai_group_v2_canary_policy_v1` 代码清单，只固化本文已经批准的合同：
`message_brief_v2`、`voice_contract_v3`、mode-specific Prompt contract、
`adult_human_anchors_v1`、独立 semantic reviewer、一次定向内容重试、
`quality_wait`、禁止 Stage 1/emoji/签到/static fallback、`max_style_overlay_chars=200`
和初始 `max_generation_latency_seconds=90`。清单不得包含自由 Prompt、生产 Task、
Provider、成人证明、账号面具正文、价格预算或 sampling 选择。

正式入口固定为 `preview -> fingerprint -> apply -> readback`：

1. preview 可以在选择不完整时运行，必须返回 `missing_user_choices`，至少区分
   `task_id`、`allowed_routes`、成人 `attestation_ids`、每个 required purpose 的有序
   Provider/model/timeout/rate/concurrency items、`max_cost_per_slot`、任务日 AI 预算、
   sampling manifest hash、requester/approver/approval reference；批准设计不能替用户
   决定这些值。
2. preview 冻结 deployed SHA、Task tenant/type/status/lifecycle/config revision/config hash、
   current policy/hash、每个 purpose active revision/hash、Provider health/credential/price、
   canonical identity 全集、task scope 的 active voice profile coverage、adult attestation、
   open GenerationJob/Action/ExecutionAttempt/Gateway/unknown 和现有 V2 task/binding 集合。
3. apply 要求 requester 与 approver 不同、选择完整、fingerprint 精确一致；在单事务中
   row-lock 并重算全部事实，CAS 创建不可变 policy version 和 required purpose route
   revisions，再只对一个显式 group task 增加一个 config revision 并创建唯一 binding。
   任一步或 AuditLog 写入失败必须整体回滚。
4. reviewer 的全部 canonical provider/model identities 必须与 context router 及全部启用
   Realizer identity 全集不相交。健康检查只证明可调用；任一未健康/未启用/未计价候选、
   purpose 缺项或交叉均阻断 apply，不能靠 fallback 顺序绕过。
5. 首版 apply 只接受没有 open GenerationJob、没有 pending/claiming/executing/retryable
   Action、没有 Gateway-started/unknown 的静默 Task revision。它不批量取消存量 Action，
   不改 success/typed fact，也不实现隐式存量迁移；需要存量切换时另走 §9.3 的精确
   candidate invalidation manifest。
6. 全部 scoped 账号必须已有 active、quality-active、可解析的 voice profile；工具只报告
   缺口，不生成、伪造或批改面具。成人 route 只接受与 next task revision、next policy
   version、scope 完全一致且未过期的正式证明。
7. readback 分别返回 persisted policy、route revisions、task config revision/binding hash 和
   AuditLog identity；只证明配置持久化，不证明 shadow、正文质量、数量履约或 Telegram E4。

入口不得自动选择“最合适”的生产任务，不得一次打开 7 个任务，不得修改 tenant legacy
static fallback，也不得通过 general route 消化被拒绝或不确定的成人上下文。后续生产 apply
仍需发布 SHA 一致、无并发 release，并与账号专项生产窗口串行协调。

### 7.5 Token 放大紧急补正与生成后查重

- 生产 `context_superseded_requeue` 证明“生成完成 -> 新真人消息 -> 清空 candidate -> 立即重排 -> 再次调用 Provider”会在活跃群形成正反馈放大。普通非回复 ready candidate 从本补正开始采用 frozen-candidate 语义：保留正文、消息记忆、candidate hash、原 due 和 quantity owner，只写 `context_drift_observed` 及新上下文计数。
- 生成前的 listener freshness、最新上下文刷新和 Prompt 重建继续执行；本补正不允许发送无 scope、无 reply authority、内容政策失败、账号不可用或重复的正文。
- 生成结果通过现有内容政策后、进入 ready Action 前，新增同租户同群 5 分钟跨账号精确去重；归一化指纹相同即写 `duplicate_message`，不进入 Gateway。同账号跨群 10 天精确/相似/语义/模板壳句规则保持不变。
- 同群并发精确去重的 reservation key 以 `tenant + group + fingerprint + 5m bucket` 为原子边界；不同群不同账号仍可独立发送相同自然短语。
- 本补正不修改任务目标、pacing slot、obligation、失败补量或 Telegram unknown 合同；发布验收必须同时观察新 `context_superseded_requeue` 为 0、drift 证据增长、duplicate gate、Token/E4、小时目标和发送分布。
