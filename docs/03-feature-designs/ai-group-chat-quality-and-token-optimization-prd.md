# AI 活群质量、Token 与任务履约全局优化 PRD

> 文档状态：Product Design Complete，2026-08-25 生产复验后进入 Phase 0 补正
> 实现状态：JIT/节奏已在生产生效；V2 bootstrap、Provider 逐次账本和旧链路负向词门禁已发布，单群灰度所需声线、Provider 价格与成人证明已完成生产读回。生产暂停验证发现旧 lifecycle epoch 的 Action/GenerationJob 不会被只领取 running/current-epoch 工作的 worker 清理，现补正安全暂停清理后再执行 V2 bootstrap；成人路由与 Telegram E4 尚未完成，状态仍为 `production_fixed=unproven`
> 适用范围：AI 活群 `group_ai_chat`；AI 评论仅定义独立二期边界
> 不在范围：降低任务目标、压缩发送时间、静态话术兜底、网络安全专项设计
> 最近修订：2026-08-25

> 本地实现 revision：`ai_group_v2_canary_policy_v2`；通用 Prompt 为 `general_v3`，成人感官 Prompt 为 `adult_service_sensory_v2`。该 revision 仅是待灰度候选，不自动替换线上 active policy。

## 1. 文档定位与真相源

本文是本轮“质量差、Token 空烧、任务不达标、发送不像真人”问题的全局整合与开发交接入口，不另造第二套运行契约。冲突时按下列顺序执行：

1. 任务量、稳定履约义务、GenerationJob 与远端事实：
   `task-fulfillment-contract-closure-prd.md`、
   `ai-group-generation-failure-churn-remediation-prd.md`。
2. 内容路由、授权、Provider 与运行状态：
   `ai-content-routing-and-quality-upgrade-prd.md`、
   `ai-content-routing-and-quality-upgrade-runtime-contract.md`。
3. 消息 Brief、人设、随机节奏：
   `hourly-random-pacing-and-ai-humanization-prd.md`。
4. 评测、灰度、发布与 E4：
   `ai-content-routing-and-quality-upgrade-evaluation-release-contract.md`。
5. 本文只补齐跨域决策、落地顺序和共同验收口径；如开发改变入口、模块或数据流，必须同步项目结构与数据流转索引。

`ai-group-topic-teacher-burst-prd.md` 中连续突发式发送已经废止，不得以“双号互动”名义恢复。

## 2. 原始需求与成功定义

### 2.1 必须解决

1. AI 活群应根据真实群上下文、人设和场景生成，既能通用聊天，也能在获得任务授权且上下文支持时偏向成人同城话题。
2. 内容应短、自然、有差异，可接话、调侃、追问或接梗；不能所有账号说同一种成人黑话，也不能退化成签到、天气、加油等模板。
3. Token 只在稳定任务量已有发送机会时消耗，停止“生成后上下文失效—丢弃—重排—再生成”的放大环。
4. 各任务原目标、小时配额、随机分布、账号错峰和 Telegram 限速保持不变；不能靠降目标或一分钟集中发送制造达标。
5. CPU、内存与数据库压力不得因质量升级回升。
6. AI 评论必须作为独立产品链路验证，不能默认继承活群提示词后直接上线。

### 2.2 完成的唯一口径

- 内容质量通过预注册离线评测和生产灰度门禁。
- 数量只由 Telegram 类型化远端成功事实计入，不以生成成功、Action 完成、健康检查或部署成功代替。
- 生产 E4 同时证明质量、任务量、拟人节奏、Token、CPU/内存及其他任务无回归。
- 在取得上述证据前只能写 `qa_pass`、`product_accepted` 或 `production_fixed=unproven`，不能写生产已修复。

## 3. 已有证据与当前结论

### 3.1 生产历史基线

历史审计显示生成后发送前废弃比例异常：

| 日期 | AI 生成量 | 发送量 | `expired_before_send` | 废弃比例 |
| --- | ---: | ---: | ---: | ---: |
| 08-24 | 44,560 | 7,861 | 32,119 | 72.1% |
| 08-23 | 47,715 | 6,927 | 38,264 | 80.2% |
| 08-22 | 26,746 | 2,201 | 23,731 | 88.7% |

这些数据用于定义问题基线，不证明单一根因，也不授权修改任务目标。

### 3.2 线上真实上下文影子脚本

- 输入：4 组 Listener 真实上下文，分别调用 MiMo 与 MiniMax，共 8 个样本。
- 结果：精确硬门禁仅 2/8 通过，融合严格门禁 0/8；双号链未通过。
- MiMo 能产出部分合适的成人感官短句，但通用场景仍有模板化。
- MiniMax-M2.5 多次返回不合规结构化 JSON。
- 影子调用合计消耗 10,729 Tokens；只读数据库、未向 Telegram 发送。

结论：脚本证明了问题和部分可行方向，没有选出默认模型，也没有完成生产验证。

## 4. 根因边界

1. **生成所有权错误**：未先冻结稳定数量所有者和发送机会就生成，导致候选反复失效。
2. **上下文选择错误**：把 `j`、`Qz5` 等低信息末条当事实锚点，模型只能猜测或说套话。
3. **路由污染**：把成人场景做成全局 Prompt，使通用聊天也强行成人化。
4. **人设丢失**：将完整 Voice Contract 压成同一个“老哥标签”，降低 Token 的同时抹平账号差异。
5. **事实未冻结**：生成内容虚构“昨天见过、刚体验、花钱、具体地点”等经历和交易事实。
6. **Provider 语义混乱**：429、超时、JSON 错误和质量不合格共用切换逻辑，容易用更多调用洗出碰巧通过的内容。
7. **质量与履约脱节**：查重、审阅、过期、任务差额没有统一状态与唯一计数所有者。

## 5. 目标业务链路

```text
稳定履约义务 / 小时 Slot
  -> 冻结 Assignment 与 immutable_intent
  -> GenerationJob 在生成窗口取上下文并生成
  -> 确定性门禁 + 独立 Reviewer
  -> accepted candidate + message memory
  -> ready Action
  -> Dispatcher / Gateway
  -> ExecutionAttempt
  -> Telegram typed remote fact
  -> 唯一履约记账
```

### 5.1 所有权约束

- Planner 只规划稳定义务、Slot、账号和确定性时间，不生成正文。
- GenerationJob 是唯一 LLM 调用所有者；不得在 Dispatcher 的数据库事务中调用模型。
- 一个稳定 Slot 同一时刻只允许一个有效 GenerationJob 和一个 accepted candidate。
- Action 只有 accepted candidate 才能 ready；Dispatcher 只发送，不改写正文、不重新调用模型。
- Gateway 返回类型化远端事实；仅该事实可完成数量记账。
- 每次失败必须落到唯一状态，禁止多个循环分别补同一差额。

## 6. JIT、任务量与拟人节奏

### 6.1 JIT 生成窗口

- 每个 Slot 持有 `generation_not_before_at`、`due_at`、`latest_safe_send_at`。
- 到达可生成窗口且账号、群、Provider admission 和发送容量均可用时，GenerationJob 才取最新上下文。
- 提前量是配置化 lead window，由当前模型延迟与队列余量决定；不是固定“发送前 1～2 分钟”的绝对承诺。
- 若尚无发送机会，状态为 `waiting_send_capacity`，不得先生成正文。
- 生成后因上下文或策略修订失效时只将该 Job 标为 `context_stale` 或 `policy_stale`；同一 Slot 由单一协调器决定是否重生，禁止 Planner 与接管循环同时补偿。

### 6.2 任务量不可被质量方案修改

- 不自动把 4,000+ 调成 1,200～1,500，也不以任何“平台合理上限”静默改目标。
- 原日目标、小时目标和 backlog 保留；容量不足记录 `capacity_shortfall`，质量无法通过记录 `quality_shortfall`，Provider 不可用记录 `provider_shortfall`。
- 短缺必须在任务状态和运营视图可见，不能发送静态兜底话术填量。
- 补偿只能落入未来合法 Slot，不能压缩到当前一分钟集中发送。

### 6.3 拟人节奏不可回归

- 沿用确定性 seed、小时内随机分布、账号独立抖动、冷却和限速契约。
- 重启、接管和重排必须复用已冻结 due time；不得把历史欠量全部改成“立即执行”。
- 同账号、同群和跨账号的间隔分别校验；质量重试不能改变原发送节奏。

## 7. 上下文与事实契约

### 7.1 信息门禁

Listener 提供有界候选窗口后，先确定性筛选：

- 去除纯标点、单字符噪声、随机码、机器人系统消息和已撤回消息。
- 优先选择最新且有明确语义、对象或事件的真实群友发言。
- 若没有有效事实锚点，显式设置 `context_mode=silence`，只能使用任务允许的通用话题，不能假装回复某人。
- 不能为了接最后一句而丢失前文；上下文截取必须保留与锚点相关的最小局部线程。

### 7.2 冻结字段

GenerationJob 必须保存：

- `context_revision`、`context_hash`、`captured_at`、`context_age_ms`；
- `anchor_message_ids`、`anchor_author_ids`、`context_mode`；
- `allowed_facts`、`forbidden_claims`、`task_topic_revision`；
- `content_route`、`route_reason`、`voice_contract_version`、`prompt_version`。

进入 Gateway 前重新读取 revision。锚点被删除、路由发生变化或超过各模式的最大上下文年龄时，不发送旧候选，进入显式 stale 状态。

### 7.3 事实约束

- 只能复述或询问 `allowed_facts` 中可支持的内容。
- 禁止虚构亲历、消费、到访、价格、位置、人物状态和第三方评价。
- “昨天见过她”“刚体验完”“前天刚去过”“花钱找气受”等只有上下文明确提供同一主体事实时才允许。
- 无事实时可表达感受或提出短问句，但不能把猜测写成事实。

## 8. 内容路由与 Prompt 架构

### 8.1 先路由，后生成

每个 Slot 先确定一个模式：

| 模式 | 触发条件 | 内容范围 |
| --- | --- | --- |
| `general_chat` | 默认或成人证据不足 | 本地生活、吃喝、夜间活动、群友原话接梗 |
| `adult_visual` | 任务授权且上下文讨论外观 | 非露骨外观感受或追问 |
| `adult_product` | 任务授权且上下文讨论成人用品 | 产品使用感、选择和简短追问 |
| `adult_service_inquiry` | 任务授权且明确询问服务对象 | 基于事实的短问句，不虚构亲历 |
| `adult_service_sensory` | 任务授权且明确存在感官语义 | 克制、短促、场景化感受或追问 |
| `unsafe_or_unknown` | 证据冲突或无法确定 | 不生成，进入 `quality_wait` |

“老师、夜课、课程、学校”等弱词单独出现不构成成人路由证据。任务必须已绑定对应话题授权和有效、scope 匹配的 `AdultSubjectAttestation`，且当前上下文有同一对象的语义证据；Prompt 自称“均为成人”不能代替该业务证明。

### 8.2 分层 Prompt

Prompt Registry 由四层组成：

1. 稳定核心：JSON Schema、事实约束、唯一输出规则。
2. 路由模块：只加载当前模式所需的词义和正负样例。
3. MessageBrief：行为、对象、事实、长度、是否回复和禁区。
4. Voice Contract：当前账号的结构化表达差异。

不得再使用全局 `ADULT_SYSTEM_PROMPT`，也不得要求所有消息“100% 行业黑话”。

### 8.3 语言与风格

- 以手机随手发送的短句为主，允许少量极短回应和少量较长句，长度分布由 Voice Contract 决定。
- 优先接话、反问、调侃、补充生活细节，禁止机械广播。
- 通用负向词库至少覆盖：签到、打卡、积分、努力加油、搬砖、今天状态不错、大家心情好。
- 负向词库必须记录 `scope`、`route`、`version`、匹配类型和启停状态；不能只靠一个字符串数组。
- `好润`、`水滋滋`、`水多不？`仅作为经人工认可的 `adult_service_sensory` 评测锚点，不能成为全局模板，也不能在事实和路由不匹配时生成。

## 9. MessageBrief 与 Voice Contract

### 9.1 MessageBrief

每次生成前构建不可变 Brief：

- `speech_act`：回复、追问、接梗、轻吐槽、生活化起话之一；
- `target_message_id` 与 `reply_authority`；
- `allowed_facts` 与 `forbidden_claims`；
- `content_route`、`topic_direction`、`length_band`；
- `must_include`、`must_avoid`、`dedupe_window`。

Brief 决定“说什么”，Voice Contract 决定“这个账号怎么说”，二者不得互相覆盖事实。

### 9.2 Voice Contract v3

面具瘦身只删除长篇传记和重复自由文本，保留下列结构：

- 长度组合与短句比例；
- 问句率、emoji 率、标点习惯；
- 口语标记、语气强度、幽默度、温度；
- 偏好的 speech acts 与禁用模式；
- `voice_contract_version` 和来源。

不能把所有账号统一压缩成“包工头老哥”。同一批次所选账号必须有可观测的风格距离，并用评测验证账号可区分度。

## 10. 查重与质量门禁

### 10.1 生成后确定性门禁

候选必须依次通过：

1. JSON Schema 与字段类型；
2. 路由授权和当前 revision；
3. 事实 grounded 检查与 unsupported experience 检查；
4. 通用场景强制成人化检查；
5. exact、normalized、semantic、structural 四级查重；
6. 同账号、同群、同任务窗口内模板坍缩检查；
7. MessageBrief 与 Voice Contract 硬约束。

Schema 错误不得提取一段文本后继续发送；记录 `malformed_output`。确定性门禁失败不得更换 Provider 洗稿。

### 10.2 独立 Reviewer

- Reviewer 与 Generator 使用不同模型家族，且接收精简 Brief、上下文事实和候选，不接收生成思维过程。
- Reviewer 输出固定维度：事实一致、上下文相关、自然度、人设一致、重复度、路由正确。
- 评分规则需对人工认可的模式锚点做校准，不能因为短句过短或成人词本身将正确样本误杀。
- Reviewer 不通过进入 `quality_wait` 或在同一 Job、同一稳定 Slot 内执行唯一一次同路由重写；重写预算耗尽后记录 `quality_shortfall`。

## 11. 双号互动契约

双号互动只用于冷清场景下两个既有稳定 Slot，不额外创造任务量：

1. A、B 分别属于两个 Slot、两个不同账号和两个数量所有者。
2. A 先生成并按原 due time 发送；B 保持 `waiting_parent_remote_fact`。
3. A 获得 Telegram typed remote fact 后，B 才解析真实 `reply_to_remote_message_id` 并生成接话。
4. B 的 due time 至少满足账号、群和拟人间隔；不得固定 2.5～5 秒连发。
5. A 失败时 B 取消链关系或回到原独立 Slot，不能伪造 reply，也不能重复补 A 的数量。
6. 其他合法 Slot 不因该链阻塞；一次链默认两条，不输出四轮对话塞进队列。

## 12. Provider 路由与 Token 预算

### 12.1 Purpose Route

至少拆分：

- `brief_builder`：规则或小模型；
- `content_generator:<route>`；
- `quality_reviewer:<route>`；
- `embedding_dedupe`。

每条路由保存 provider/model、priority、revision、config_hash 和生效时间。小样本脚本不能直接决定默认模型。

### 12.2 失败语义

| 失败 | 状态 | 是否可切 Provider |
| --- | --- | --- |
| 429 / provider admission | `waiting_provider` | 仅按已配置 transport failover |
| timeout / connect error | `provider_transport_failed` | 是，受同 Slot 预算限制 |
| malformed JSON | `malformed_output` | 否，暴露模型/Schema 问题 |
| deterministic gate failed | `quality_gate_failed` | 否 |
| reviewer rejected | `quality_wait` | 否；最多同路由一次重写 |

Provider 切换只能解决传输可用性，不能作为内容质量重试策略。

### 12.3 完整 Token 账本

每个 Slot 的完整成本为：

```text
route/brief + generator + reviewer + rewrite + transport failover = slot total
```

逐次记录 purpose、input/output/cache tokens、费用、延迟、provider/model、Job/Slot id 和结果状态。预算包含至少 25% 的延迟与重试余量，按日、任务、路由和 Provider 聚合。

既有“407→181”“1500～2500→330～390”等不同口径不能直接互比；上线前必须用相同完整账本重测。本文不承诺固定节省比例、固定延迟或零 429。

## 13. 数据、接口与运营可见性

### 13.1 最小增量字段

优先扩展既有 GenerationJob、MessageMemory、Action 和 Attempt，不新建平行事实表。需要持久化：

- Slot/Job 唯一所有者和 JIT 时间窗；
- Context、Brief、Voice、Prompt、Route、Provider 的 revision/hash；
- candidate、dedupe fingerprint、review result；
- token/cost/latency 分项；
- typed failure、shortfall 与 remote fact 关联。

所有写入使用既有幂等键；迁移先加 nullable 字段和索引，再双写观察，最后切读，不回填伪造远端事实。

### 13.2 API / UI

任务详情与诊断接口至少展示：

- 原目标、已完成远端事实、各类 shortfall；
- 等待发送容量、等待 Provider、质量等待、stale 和失败数量；
- 生成到远端成功的转换漏斗及 Token 浪费去向；
- 当前 route/prompt/voice/provider revision；
- 小时分布、账号间隔与集中发送告警。

运营修改 `topic_directions` 只是任务配置输入，不等于成人路由授权，也不能绕过版本和审计。

## 14. CPU、内存与并发约束

- 上下文查询使用有界窗口和现有索引，不扫描全群历史。
- 每轮只领取有发送机会的 free slots；沿用既有有界批量上限，不因欠量扩大到全量生成。
- LLM 网络调用不占用数据库长事务或 Dispatcher 锁。
- Prompt/Voice/Route 配置按 revision 缓存，不能按每条消息重复载入整套长文本。
- 查重索引采用有界时间窗；不得把全部历史 embedding 常驻 worker 内存。
- 不新增常驻轮询 worker；优先复用既有 GenerationJob worker 和接管协调器。
- 灰度必须比较 worker RSS、CPU、数据库查询 p95、锁等待、队列 lag；任一显著回归即停止扩量。

## 15. AI 评论二期边界

AI 评论和活群共享 Provider 账本、Voice Contract 与确定性门禁框架，但不共享 MessageBrief 和评测结论。二期必须另行补齐：

- 评论源消息 revision/hash、目标 `remote_message_id`、direct/reply authority；
- 评论对象、事实锚点、去重窗口与撤回/编辑处理；
- 评论自己的路由、Prompt、120+ 分层样本、人工基线和 Reviewer；
- 独立灰度、数量记账和 Telegram E4。

活群一期通过不能自动开启评论新链路。

## 16. 实施顺序与回滚

### Phase 0：观测与止血

- 关闭生成后因上下文失效而由多个循环立即重排的放大环，只保留单一 Slot 协调者。
- 上线完整 Token/状态账本和生成后查重观测，不改变发送正文与任务目标。
- 对尚未进入 route v2 的存量任务，Provider Attempt 允许以 `GenerationJob + provider/model` 记账而暂缺 route-set；`route_set_id` 必须显式为空、revision 固定为 0，不得伪造路由。每次真实 Provider 调用都要记录 input/output/cache、结果、延迟和费用；进入 V2 后再记录不可变 route revision。
- Prompt 负向词不是门禁。存量链路在所有模型阶段之后仍命中 `签到/打卡/积分/努力加油/搬砖/今天状态不错/大家心情好` 时，必须以 `negative_lexicon_match` 可见拒绝，不得再转成签到静态补量；原数量债保留为 shortfall，后续只由新的真实 JIT Slot 补齐。
- Planner 的真人 `usable_context_rows` 必须优先形成 reply target，再回退到我方已发送历史；不得丢弃 Listener 上下文。小时追量/日覆盖欠量只改变 Slot 释放时间，不能把配置的回复比例清零；回复仍消费原数量 Slot，并在 Gateway 前同时通过本地 scope 与远端目标存在性校验。

### Phase 1：正确所有权与 JIT

- 固化 Slot、Assignment、GenerationJob、Action 和远端事实的唯一关系。
- GenerationJob 按发送机会 JIT 生成；Dispatcher 移除所有 LLM 调用。
- 验证原任务量和小时节奏没有回归后再进入质量灰度。

### Phase 2：质量链路

- 上线信息门禁、MessageBrief、Voice Contract v3、分路由 Prompt、确定性门禁和 Reviewer。
- 先 shadow，再单群小比例 canary；Provider 路由通过完整评测后显式启用。
- canary preview 的声线覆盖必须为全量 ready；Provider 明示的闭合 `<think>...</think>` 可在解析前确定性剥离，但不得从任意自然语言或未闭合 block 中搜索、猜测或提取 JSON。
- 声线生成的 Provider HTTP 429 必须进入 `provider_rate_limited` 退避，不得因错误正文包含 `provider` 而误判为不可重试的配置错误；推理模型的声线单条输出预算为 1024 token，以容纳 reasoning 与单行 JSON，字段及质量门禁不放宽。
- 单群 bootstrap 在 paused epoch 创建配置后，resume 必须在新的 task lifecycle epoch 上重新建立同一 policy/config revision 的 binding；运行时只读取当前 epoch binding，不得把“配置已落库但 binding 仍属于暂停 epoch”误报为启用成功。
- `group_ai_chat` 暂停必须先锁定 Task 并检查 `unknown_after_send`、已开始 Gateway Attempt 和 V2 `gateway_bound` slot。无远端不确定性时复用任务重规划清理，释放旧 Action 的 coverage/content-mix 占用，并把开放 GenerationJob 与 pre-Gateway window slot 显式置为 cancelled/invalidated；同时只对当前时间覆盖的 open ledger 内、`open + frozen` 且未绑定 active/success/unknown Action、任一 Attempt 均未开始 Gateway 且没有 remote ID 的数量 slot 释放旧 `task_lifecycle_epoch` claim 和已过期 `release_not_before_at`，使其可由 resume 后的新 epoch 接管。已关闭或历史 ledger 不参与接管。存在任一不确定性时必须保留原事实、在 AuditLog 标记 `blocked_remote_ambiguity`，且 bootstrap 继续以 `task_open_work_present` 阻断。
- 暂停清理不得修改日目标、`pacing_due_at`、plan hash、slot ordinal、已确认 Telegram 事实或任务配置 revision；success、unknown、Gateway-started 及仍有 active/retryable Action 的数量 slot 必须保持 immutable。resume 由新 epoch 在原 due/plan 合同上重新规划原数量债，并按当前历史游标恢复 `release_not_before_at`，不能把清理数量记为成功或用集中补发追平。
- `paused -> running` 的 resume 必须保留既有 `pacing_anchor_at`；只有从未建立过 pacing anchor 的首次启动才能写入当前时间。不得因灰度暂停把自然日 DueSet 从恢复时刻重新起算，也不得通过一次性集中补发弥补恢复前债务。
- fact-first 旧链路写回 Task 的 `ai_provider_id` 是单 Provider 运行时绑定，V2 的 router/realizer/reviewer 改由各 purpose route-set 冻结。bootstrap preview 必须把旧 ID 纳入 fingerprint；apply 在同一配置 revision 事务中显式移除它并在审计记录原 ID，不能放宽 `GroupAIChatConfig(extra=forbid)`，也不能让旧 ID 覆盖 V2 route-set。

### Phase 3：双号与 AI 评论

- 双号互动单独灰度，确认不形成突发发送。
- AI 评论按第 15 节独立设计、实现和验收。

回滚按 revision 切回上一组 Prompt/Route/Voice 配置，并停止领取新的新版本 Job；已 accepted 的候选若 revision 不匹配则显式 stale。回滚不能启用静态话术、降低目标、压缩 due time 或伪造成功。

## 17. QA、Release Gate 与生产 E4

### 17.1 离线评测

- 至少 120 条、覆盖不同群、时段、上下文质量、成人/通用路由、silence、各 Voice 和评论边界。
- 固定人工金标、确定性 gate、分维度 Reviewer、盲测和 position swap。
- Generator 与 Reviewer 不同模型家族；先锁定 rubric，再比较 Provider。
- 必过硬门禁：Schema 错误误放行 = 0、事实幻觉误放行 = 0、通用强制成人 = 0、重复逃逸 = 0、静态兜底发送 = 0。

### 17.2 灰度门禁

- 对比旧版本的人工自然度、上下文相关度、人设区分度和路由正确率。
- 对比每个 typed remote success 的完整 Tokens/费用，而非只看单次 Prompt。
- 生成后因 Planner/队列年龄而废弃的候选必须为 0；合法的 context/policy stale 单独统计并使用预注册阈值。
- 原目标不变时，小时及日任务完成率不得下降；发送聚集、账号间隔、失败补偿不得回归。
- CPU、RSS、DB p95、Gateway 延迟和非 AI 任务吞吐不得显著回归。

### 17.3 发布与 E4

按 `master -> release -> GitHub Actions Deploy Production` 发布。部署 SHA、容器健康和 QA 只证明发布层，不证明业务完成。

生产 E4 必须从当前任务读取：

1. 稳定 Slot 到 GenerationJob、Action、Attempt、Telegram typed remote fact 的完整链路；
2. 多个真实成功消息及其上下文、route、voice、质量结果；
3. 原日/小时目标完成情况和所有 shortfall；
4. 生成—接受—发送—远端成功漏斗和完整 Token 账本；
5. 小时随机分布、账号错峰、无一分钟集中发送；
6. CPU、内存、DB、其他任务类型和 AI 评论未启用链路的无回归证据。

只有上述证据均通过，才能标记 `production_fixed`。

## 18. Product Design Complete 自检

- [x] 覆盖质量、Token、任务量、拟人节奏和成人方向的原始需求。
- [x] 明确通用/成人分路由，不使用全局成人 Prompt。
- [x] 明确上下文信息门禁、事实冻结、查重和 Reviewer。
- [x] 明确 JIT 所有权、失败状态、数量唯一记账和双号依赖。
- [x] 明确 Provider、Token、数据、API/UI、迁移、回滚和资源约束。
- [x] 明确 AI 评论独立二期边界。
- [x] 明确离线、灰度、发布和生产 E4。
- [x] 未降低目标、未恢复突发发送、未声明生产已修复。

设计结论：本文已达到产品设计完整度，Phase 0～2 与双号依赖本地候选已实现；发布结果与生产 E4 仍须按本文独立验证。
