# AI 内容升级评测、灰度与发布合同

> 规范性附录。与 `ai-content-routing-and-quality-upgrade-prd.md` v1.2 共同生效；本附录只定义可重复验收，不代表代码、发布或 Telegram E4 已完成。

## 8. 评测与质量门

### 8.1 评测集

开发期可用 20–30 条快速集定位错误；Phase 1 Release Gate 必须使用至少 120 个真实脱敏 case，覆盖至少 10 个 context cluster、3 个不同任务/群 scope 和 20 个 voice profile。以下关键 strata 每类至少 20 个：general、adult_service inquiry、adult_service sensory、弱词/unsafe 负例；同一 case 可用于多个风险标签，但每个 stratum 的独立样本量必须报告：

- general direct/reply；
- 成人视觉；
- 成人用品；
- adult_service inquiry；
- adult_service sensory；
- “老师/夜课/课程”负例；
- 未成年人/年龄不明/PII/提示注入；
- 有上下文、长上下文、无上下文；
- 不同 voice profile；
- 频道评论在 Phase 2 单独成集。

评测样本与 Prompt example set 必须物理/版本隔离，防止测试污染。

adult_visual/adult_product 若真实样本不足，不允许用合成样本凑 Release Gate；该 route 标记 `insufficient_data/unproven` 并保持 disabled。Phase 2 评论另建至少 120 case 的独立基线，不能复用群聊样本量。

### 8.2 Deterministic gate

以下确定性规则测试必须全部得到预期判定，且对应 sent escape 必须为 0：

- schema/mapping/scope/version；
- unauthorized adult route = 0；
- general forced adult = 0；
- unsafe/minor send = 0；
- wrong reply authority = 0；
- exact duplicate = 0；
- sensory wrong object = 0；
- Provider output 在 gate 后被改写 = 0；
- malformed/429/reviewer unavailable 被误记 pass = 0。

任何一项失败直接阻断 Release Gate，不能由主观高分覆盖。

### 8.3 主观 rubric

每项 1–5，要求先证据后分数：

- natural：像真人随手发，不是运营/总结腔；
- context：接住最新事实；
- voice：符合冻结账号声线；
- route_fit：route/mode 与内容一致；
- information_value：不是空泛模板；
- window_quality：整窗不塌缩、不刷同类句。

统一评分锚点：`1=明显错误/不可发送`，`2=主要问题未解决`，`3=基本可理解但仍有可见 AI 腔或偏差`，`4=自然且可直接发送`，`5=非常贴合且无可见改写需求`。reviewer prompt、人工标注表和报表必须引用同一 rubric version；双标样本 Cohen's kappa <0.70 或关键维度 agreement <80% 时只可继续校准，不能进入 Release Gate。

单样本绝对放行必须同时满足：`natural/context/route_fit >= 4`、`voice/information_value >= 3`、所有关键维度均不低于 3、所属窗口 `window_quality >= 4`；否则 `human_sendable=false`。聚合 `human_sendable_rate >= 85%`，且每个关键 stratum 均 `>=80%`。绝对门先于相对偏好，不能用“比 baseline 好”掩盖仍不可发送的内容。

### 8.4 Pairwise 与人工校准

- candidate 与 production baseline 使用相同 context、voice、reply 和长度档位。
- 每对执行左右换位；映射后不一致记 position-unstable/tie，不能通过从分母删除来抬高胜率。
- judge 必须与 generator 不同 family；多 judge 分歧保留 `unproven`，不多数投票追求通过。
- 人工批准锚点优先定义产品口径；自动 reviewer 必须以人工标注校准 agreement 和系统性偏差。
- primary pairwise score=`(candidate wins + 0.5 × true ties) / all eligible pairs`；position-unstable 单列为 evaluator coverage failure。至少 80 对 position-consistent 人工 pair，position-consistent coverage ≥80%，position-unstable ≤20%。
- Release Gate 要求 primary 人工偏好点估计 ≥65%，按 context cluster bootstrap 的 95% 下界 >50%，关键 strata 无显著退化；同时满足 8.3 绝对 sendable 门。任一 coverage/tie 门不满足均为 `unproven`。

### 8.5 稳定性、成本与延迟

- 同一冻结输入至少重复 3 次；报告 route/mode consistency、candidate semantic consistency 和 gate variance。
- canary 初始硬预算为：route transport attempts ≤2、realizer 总 attempts（含 transport failover 与内容重生成）≤2、reviewer transport attempts ≤2、每 slot Provider calls 总数 ≤6；修改只能新 policy revision 并重跑同一评测集。
- Preview 必须冻结非空 `max_cost_per_slot`、`max_generation_latency_seconds`（canary 初始 90 秒）和任务日预算，显示 input/output token、单价与 25% transport buffer；任一预计值超预算时任务 preflight 失败，不能边运行边无限重试。
- 线上记录 p50/p95：route、realize、review 和总 generation latency。
- 任何阶段必须在 `latest_safe_send_at` 前完成；预算不足进入 shortfall，不压缩到 Gateway 前突发追量。
- 429/timeout/malformed 按 Provider+purpose 独立统计；不得通过加无限 retry 提升表面成功率。

### 8.6 指标分母与生产监控

固定指标定义，禁止把候选拦截与线上逃逸混成一个“pass rate”：

| 指标 | 分子 / 分母 | 门槛与动作 |
| --- | --- | --- |
| candidate gate rejection rate | 被确定性闸拒绝的 candidate attempts / 全部 candidate attempts | 非零可正常，按 policy/route 建 baseline；突升告警，不作为“线上违规” |
| escaped deterministic violation | 抽检 typed sent facts 中违反确定性规则的数量 / 被抽检 typed sent facts | 必须为 0；任一即停止该 policy 新生成 |
| duplicate escaped-to-send | typed sent facts 中 exact/structural duplicate / typed sent facts | 必须为 0；任一停止扩容并调查 memory scope |
| window collapse candidate rate | 塌缩候选窗口 / 有足够 slot 的候选窗口 | 超预注册 baseline 告警 |
| window collapse sent rate | 存在塌缩 sent facts 的窗口 / 有足够 sent facts 的窗口 | 必须为 0 |
| reviewer unavailable rate | reviewer unavailable attempts / reviewer attempts | 超 baseline 则不放行并检查 purpose capacity |
| human sendable rate | `human_sendable=true` 样本 / 人工已审核样本 | 总体 >=85%，关键 stratum >=80% |
| general forced-adult / unsafe-minor send / sensory wrong-object | 对应违规 typed sent facts / 对应 typed sent facts 抽检 | 必须为 0；任一立即停止并按风险处理 |
| Provider 429/timeout | 对应 typed transport attempts / Provider+purpose attempts | 超预注册 baseline 时处理 admission/容量，不改内容 gate |
| quantity completion | confirmed quantity owners / due quantity owners | 分任务/群/period 报告；未完成必须与唯一 typed shortfall 一一对应 |
| static fallback sent | `签到`/emoji/静态短句 fallback typed sent facts / AI typed sent facts | current v2 必须为 0；任一即停止该 task 新生成 |
| context freshness | Gateway 前 context revision/hash/age 一致的 typed sent facts / AI typed sent facts | 必须 100%；不一致不得调用 Gateway |

生产阈值只有在预注册最小分母满足后用于扩容/回滚；不足时一律标 `insufficient_data/unproven`，不能按零事件通过。监控必须同时保留 count、denominator、policy/route-set revision 和观察窗口。

---

## 9. 失败、恢复与回滚

### 9.1 Typed failure

| 边界 | 状态/错误码 | 行为 |
| --- | --- | --- |
| task binding 缺失/过期 | `content_policy_binding_missing/stale` | 不调用 Provider，等待配置修复 |
| context route 低置信/证明过期/缺证据 | `context_route_unproven` | `pending+generation_stage=quality_wait` 或 silence，不降 general、不强转 |
| window plan 冲突 | `content_window_plan_conflict` | CAS 读回 winner，不能双 plan |
| Provider admission/429 | `provider_rate_limited` | 写 next_retry_at，释放 worker |
| Provider transport exhaust | `provider_route_exhausted` | `pending+generation_stage=waiting_provider+next_retry_at`；超时限则 shortfall |
| structured output malformed | `*_schema_invalid` | typed fail；按显式 attempt budget 处理 |
| deterministic gate fail | 对应 gate code | 同 mode 定向重生成一次 |
| reviewer uncertain/fail | `semantic_review_uncertain/failed` | `pending+generation_stage=quality_wait` 或终结 shortfall，不发送 |
| revision 变化 | `generation_intent_stale` | 旧 job 终结，新 revision 重建 |
| Gateway unknown | `unknown_after_send` | 只 reconcile，禁止自动重发 |

### 9.2 回滚

- policy、Prompt、example set 和 Provider route 均按不可变版本回滚。
- 回滚只影响新 GenerationJob；未进 Gateway 的旧 candidate 按 preview manifest 失效并重建。
- Gateway-started/unknown/typed fact 永不随内容回滚重发。
- 数据 migration additive 保留；不 drop 新列/表作为紧急回滚。
- task contract 回滚只影响原子切换后尚未创建的新 obligation；既有 current v2 owner 继续按冻结合同前向完成或 typed shortfall，禁止切回 legacy/static fallback，也禁止同一 obligation 新旧 writer 双写。
- adult obligation 绝不能回退到 legacy general。回滚时，新 adult generation 必须进入显式 quality_wait/shortfall，或通过 task flag 整体停用该 route；已 Gateway-started 的内容只 reconcile，不改写、不重发。
- 只有从未由 v2 接管的新 obligation 才可在 task contract readback 后使用 legacy；页面必须显示生效 epoch，apply 前预览 general/adult owner 数量与排除原因。

### 9.3 生产存量切换

切换必须执行 preview/apply/readback：

1. 固定 Task/lifecycle/config revision、policy/route-set revision 和 deployed SHA；
2. 列出 open GenerationJob、pre-Gateway Action、Gateway-started/unknown；
3. 只 invalidates 明确未进 Gateway 且 revision 匹配的旧 candidate；
4. Gateway-started/unknown/fact 排除；
5. apply 使用 old hash/version CAS 和 AuditLog actor/reference；
6. 独立读回 binding、job、Action、attempt 和 remote fact。

---

## 10. 分阶段交付

### Phase A：策略与 Provider 基础

- additive schema；
- content policy/binding；
- 按运行合同 §5.5 的 A→D 顺序完成 legacy selector、route-set backfill、preview/readback 与 single-active maintenance migration；
- API/UI preview、权限、审计；
- 不改变现有任务运行。

### Phase B：AI 活群 shadow

- MessageBrief v2、两层安全投影、context router、window planner、mode Prompt registry；
- 沿用现有两阶段 pipeline 和 quality_wait；
- shadow 读取真实 GenerationJob/上下文/voice，但不创建 ready Action；
- 120+ 分层评测、绝对 sendable、position-swap 与人工盲评。

### Phase C：AI 活群 canary

- 仅一个显式任务 flag；在此阶段才启用新 route-set business read path；
- 至少连续 3 天且取得至少 100 条 typed remote facts、30 条人工抽检；样本至少覆盖 20 general、10 inquiry、10 sensory，以及 20 个弱词/unsafe 负向 route decision（负向 decision 不要求发送），并跨 3 个 context cluster、10 个账号；
- 每天盲抽，不由运营挑选“好样本”；
- canary 开始前冻结 sampling manifest：eligible typed facts、strata、context cluster、账号、seed、去重键和缺样替补顺序；运行中不得人工替换差样本。
- 核对 GenerationJob → ready Action → Attempt → typed remote fact。
- 若任一数量不足，延长 canary；禁止仅凭时间到期通过。

### Phase D：频道评论

- 独立 comment 真实样本与 reply authority QA；
- 复用公共 policy/Provider/eval，不复用群聊 route 结论冒充通过；
- 完成独立 120+ 分层生成评测，并按 `ai-channel-comment-broadcast-and-teacher-relevance-prd.md` 另完成 200 条来源 extraction/grounding 金标；两者不得互相替代；
- 仅一个显式频道评论 Task 同时启用 grounding/route-v2/two-stage，canary 至少连续 3 天、100 条 typed remote comment facts、30 条预注册盲审、3 个内容簇和 10 个 distinct 账号；
- 频道评论允许专项 `👍 / 🙂 / 👏` 同槽单表情兜底，但兜底只计 quantity，不进入 grounded/老师/亮点分子；quantity/content mix/grounding 三维均达标后才扩容。

### Phase E：扩容

- 按任务级 flag 分批；
- 每批满足质量、Provider 容量、延迟和 E4；
- 禁止全租户热改 Prompt 或一次性迁移全部任务。

---

## 11. QA 与验收标准

### 11.1 单元/属性测试

- 强成人主题可预览 adult_service；“老师/妹子/夜课/课程”弱词不授权。
- adult route 缺少匹配且有效的 `AdultSubjectAttestation` 时不能保存；过期/撤销/scope 或 revision 变化立即失效，Prompt 自称“所有人均成年”不能补足证据。
- task allowlist 与 context evidence 缺一不可进入 adult route。
- 两层 sanitizer 保留允许的 adult_service facts，同时继续删除 PII/未成年人/注入。
- window plan 在双证据/双 slot 时分 inquiry+sensory；无感官证据不强配。
- sensory 批准样例和自然同义变体可通过，不要求逐条出现固定关键词；“丝袜看着好润”失败为 `sensory_object_wrong`。
- inquiry 的有证据价格/区域/空闲等问题可通过；同信息改为断言、换成无匹配 evidence ID 或新增精确金额/地址时失败。
- general 出现“好润/水多不/怎么约”等强转失败。
- Prompt registry 证明 sensory 与 general 使用不同 system contract。
- `system_prompt_override/slang_prompt_template_id` 只能成为限长 style overlay，不能覆盖 route、安全、schema、事实或 Provider purpose。
- Provider route 只在 typed transport failure 切下一 priority；内容失败不切换。
- 任一 enabled route-set 中 generator/reviewer canonical identity 有交集、reviewer 429 或缺失时均不能放行。
- route/mode/policy/voice/prompt revision 改变会失效 pre-Gateway candidate。
- Action lookahead 早于 `GenerationJob.generation_not_before_at` 时不能 claim；candidate 后出现新真人消息/reply change 会递增 context revision，只替换该 pre-Gateway slot。
- 一个 slot 无 pacing point、Provider 或合法候选时，同批其他合法 slot 仍可 ready；失败 owner 只能写一次 typed shortfall。
- current v2 在 tenant legacy static fallback=true、two-stage flag 任意组合下都不得产生固定“签到”或其他静态补量。

### 11.2 PostgreSQL 并发测试

- 同 window 并发规划只产生一个 frozen plan；
- frozen plan 不吸收迟到 obligation；`claimed|candidate_ready` context 变化只替换该 slot/job，`gateway_bound` 只 reconcile，其余 slot/已发送内容不变；
- 同 obligation 只有一个 open GenerationJob；
- `pending/generating/unknown` partial unique predicate 与 claim/reclaim/comment job 查询完全一致；`generation_stage` 变化不改变 open-job 语义；
- route-set apply/reorder CAS 冲突不会部分应用；
- route-set A→F migration 在保留/移除 single-active 约束的各阶段均有真实 schema readback，且未提前产生多 route business read；
- job reclaim/迟到 response 不能覆盖新 revision；
- slot lease epoch/expiry reclaim 不能产生两个 candidate_ready；current-slot partial unique 覆盖 frozen/claimed/candidate_ready/gateway_bound；
- provider attempt append-only 且 request hash/idempotency 正确；
- migration/backfill 在真实 partial unique index 下通过 upgrade/downgrade 或 forward-only 明确合同；
- Provider/Telegram 调用期间没有长事务或行锁。

### 11.3 API/UI 测试

- 成人 route 保存需要显式确认和权限；
- policy/route manage 与 approve 分权；越权 attestation、跨 tenant sample 和 self-approval 返回 403/422；
- legacy 自由 Prompt 同时展示配置值与实际生效值，越权内容保存失败而非静默丢弃；
- stale preview/revision、idempotency key 复用不同 payload 保存失败且不产生部分写；
- generator/reviewer identity 相同失败；
- purpose route 缺失/重复 priority/disabled provider 返回明确错误；
- 任务详情不泄漏原始上下文、Prompt、面具正文或成人候选；
- shadow preview 明确“不发送、不代表生产修复”。
- shadow preview 不写业务 GenerationJob/Action/memory/health/cooldown，质量样本读取和导出满足 retention/审计合同。

### 11.4 Release Gate

必须全部满足：

1. 文档、migration、schema、API、UI、worker、数据流和结构索引一致。
2. 候选 gate rejection 按固定分母报告；抽检 sent facts 的 escaped deterministic violation、forced-adult、unsafe/minor、duplicate、wrong-object 均为 0。
3. 120+ 分层评测满足 8.3 绝对 sendable 门、rubric 一致性、至少 80 个 position-consistent 人工 pair、coverage/tie 门、人工偏好 >=65% 且 cluster bootstrap 95% CI 下界 >50%。
4. route-set 按运行合同 §5.5 完成迁移、审批和 readback；层级 admission、429/timeout、每 slot 调用/延迟/金额硬预算通过。
5. canary 同时满足至少 3 天、100 typed remote facts、30 人工抽检和预注册 strata/context/account 数量；不足只能延长。
6. 真实 Telegram E4 有 successful Attempt + nonempty remote message fact，并与 obligation/account/candidate hash 对齐。
7. 回滚演练证明 pre-Gateway candidate 前向失效，unknown 不重发。
8. current v2 static fallback typed sent fact=0；due quantity owner 与 confirmed/唯一 typed shortfall 完全守恒，单 slot 失败不取消同批合法 slot。

### 11.5 状态口径

| 层级 | 可声明状态 |
| --- | --- |
| PRD/索引一致 | `product_design_complete` |
| 本地实现与定向 QA | `local_implementation_complete` |
| PostgreSQL/评测通过 | `qa_pass` |
| shadow | `shadow_pass`，不等于发送 |
| canary Action/Attempt | `runtime_pass`，仍需内容抽检和 typed fact |
| Telegram typed fact + 质量门 | `production_fixed` |

---

## 12. 开发交接与模块边界

建议新增职责文件，避免继续扩大已有 500 行模块：

| 模块 | 职责 |
| --- | --- |
| `ai_content_policy.py` | policy version、task binding、route allowlist 与 hash |
| `ai_context_projection.py` | Layer A 全局清洗与 Layer B route facts |
| `ai_content_router.py` | context route schema、parse、evidence gate |
| `ai_content_window_plan.py` | bounded window mode plan、hash、CAS |
| `ai_prompt_contracts.py` | mode-specific 短 Prompt registry |
| `ai_provider_routes.py` | tenant+purpose priority route 与选择 |
| `ai_provider_attempts.py` | append-only attempt、429/timeout/readback |

现有文件的最小改动边界：

- `message_brief.py`：MessageBrief v2 schema/parse；
- `ai_generator.py`：按 route-set/purpose 调用 Provider，不改变 Provider 正文；
- `semantic_grounding.py`：speech act、claim category 与 exact evidence gate；
- `provider_admission.py`：credential global + tenant/purpose child 层级准入；
- `ai_message_memory.py`：沿用 account/group/window 防重并增加 `(generation_job_id, candidate_hash)` reservation 幂等；
- `ai_generation_parallel.py`、`comment_generation_job.py`：保持现有 open-state predicate/claim/reclaim，不把 stage 当 state；
- `two_stage_generation.py`：接收 frozen route/mode/Prompt contract；
- `ai_generation_pipeline.py`：在现有两阶段中接入，不改发送链；
- `ai_generation_runtime_config.py`：注入 binding/window/provider route-set revision；
- `ai_generation_persistence.py`：持久化 route/mode/version/evidence；
- `ai_group_prompt.py`：保留 legacy general，不承载新 route 策略；
- `models/ai.py`、`models/fulfillment_v2.py`：additive schema；
- `schemas/task_center.py`、AI config API 与 Task Center UI：配置/预览/权限；
- `ai_quality_evaluation.py`：扩展分层报告，不改变 position-swap 基础语义。

实现完成后必须按真实入口同步 `project-structure-index.md`；本 PRD 不能提前把建议模块写成已实现事实。

---

## 13. Product Design Complete 自检

| 检查项 | 状态 |
| --- | --- |
| 原始需求：全群自然、成人软偏向、成人老师感官句 | 已覆盖 |
| 当前代码/数据流/Provider 限制 | 已覆盖 |
| 任务授权、上下文路由、窗口模式、面具边界 | 已覆盖 |
| AdultSubjectAttestation scope、期限、失效与权限 | 已覆盖 |
| WindowPlan 完整 scope、candidate_ready/gateway_bound、lease/reclaim、replacement 与迟到 obligation | 已覆盖 |
| Prompt 缩短与 mode 隔离 | 已覆盖 |
| 多 Provider route-set、legacy selector cutover、single-active 迁移顺序、层级 admission 与 429 | 已覆盖 |
| GenerationJob 时间权威、ContextScopeRevision、durable state/open predicate、并发、幂等、invalidation | 已覆盖 |
| API endpoint、CAS/error、前端、权限分离、审计与 retention | 已覆盖 |
| route-aware claim gate、绝对/相对评测、样本量、指标分母、成本与延迟 | 已覆盖 |
| failure、quality_wait、禁止 static fallback、逐 owner typed shortfall、unknown、回滚 | 已覆盖 |
| shadow、sampling manifest、canary、数量守恒、Release Gate、Telegram E4 | 已覆盖 |
| Phase 1 活群与 Phase 2 评论边界 | 已覆盖 |

最终设计结论：

```text
product_design_complete_v1.2
implementation_in_progress / local_qa_partial
shadow_generation_pass_on_small_real_window
reviewer_baseline_comparison_unproven
production_fixed=unproven
```
