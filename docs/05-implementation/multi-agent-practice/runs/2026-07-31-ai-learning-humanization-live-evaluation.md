# 2026-07-31 AI 学习群真人化线上小流量验证

## Intake Card

- `intake_id`: `intake-2026-07-31-ai-learning-humanization-live-eval`
- `source`: `user`
- `raw_input`: 已添加 AI 学习群，但生成效果不好；先在线上通过脚本或 SSH 小规模测试，可行后再做方案设计和代码修改。
- `created_at`: `2026-07-31`
- `owner_agent`: `prod-diagnosis`
- `suspected_type`: `online_issue + feature`
- `affected_surface`: `TenantLearningSource / TenantLearningSample / target profile / group_ai_chat generation / humanization quality`
- `production_related`: `true`
- `level/lane`: `L2 / ai-group-quality/learning-humanization`
- `initial_evidence_level`: `E0`
- `next_route`: `prod-diagnosis_hold`

## 原始描述

生产已配置 AI 学习群，但 AI 活群说话效果不够自然。用户认可“单句实时生成、对话动作、结构化账号面具、真实样例、多候选 Judge、禁止人工制造错字、真人感验收集”等方向，要求沿用其他线上问题的处理方式：先在线上做脚本或 SSH 小规模验证，确认可行后再进入方案设计和代码修改。

## 已知证据

- 当前仓库存在学习来源、学习样本、质量规则、目标画像和监听采集模块。
- 当前 AI 活群 Prompt 支持一次生成多个账号的多条消息。
- 当前账号面具质量门不足以证明完整的账号风格一致性。
- 以上仅为本地代码审查；尚无本轮生产学习来源、样本数量、画像内容或真实模型对比证据。

## 测试边界

1. 生产阶段先只读数据库和运行配置。
2. 真实 Provider 干跑必须停在生成结果，不创建 Task/Action，不调用 Telegram Gateway。
3. 样本输出需脱敏；禁止把账号、群、用户身份和原始敏感文本写入仓库。
4. 当前链与候选链必须使用同一份冻结生产上下文。
5. 本地检查、生产干跑、QA、部署和真实 Telegram 效果分别记录，不互相替代。

## 成功标准

- 证明学习群采集、样本筛选、画像提炼和 AI 活群消费链各自的真实生产状态。
- 至少取得一组同场景当前链与候选链输出，且全程无 Telegram 发送。
- 通过确定性门禁与交换位置 pairwise 盲评，得到可复现的优劣证据。
- 若候选方案不稳定优于当前链，停止进入代码阶段并保留 `unproven`。
- 若稳定优于当前链，先完成 Product Design Complete，再进入 dev/qa/release。

## 当前状态

- `phase`: `prod-diagnosis`
- `design_status`: `not_started`
- `qa_pass`: `not_done`
- `product_accepted`: `not_done`
- `release_gate`: `not_ready`
- `production_effect`: `unproven`

## 生产只读基线（2026-07-31 13:35 CST）

- 当前 release：`997e884b674975d777fa517338b4670604f27f19`；backend 和 11 个业务 worker healthy，宿主健康接口正常。此项只证明 runtime 可访问。
- tenant 1 学习画像：version 2、active、learning enabled，但 `last_rebuilt_at=2026-05-29 17:12 CST`、`last_used_at=null`。
- 画像记录 491 条 source sample，但 style summary 仅 31 字符；topic/phrase/reply/comment 四类结构化模式全部为空。
- 三个学习来源仅一个 enabled；最后同步同样停在 2026-05-29。
- 数据库中 group_chat 样本共 590：accepted 500、rejected 90；accepted 平均长度 6.32 字。最后一次 sync 在 profile v2 重建后又新增 9 条 accepted，未进入当前画像。
- 当前存在 3 个 running AI 活群任务。静态链路确认群聊 Action 只保存学习画像审计字段，Phase B 群聊 Prompt 不读取 `profile_hit_summary`；频道评论存在明确注入路径。

当前结论：

- `runtime`: `pass`
- `learning_source_freshness`: `blocked`
- `profile_extraction_quality`: `blocked`
- `group_ai_chat_profile_consumption`: `blocked_by_code_path`
- `no_telegram_provider_comparison`: `pass`
- `production_humanization_effect`: `unproven`

## No-Telegram 真实 Provider 干跑

- 使用生产 MiniMax-M3、同一账号面具、同一冻结安全上下文，对比 current prompt 与“accepted 学习示例 + 单句 late-binding speech-act”实验 prompt。
- 第一组：current“积分确实给力 慢慢攒起来”；experimental“水一水积分 攒起来哈哈”。
- 扩展场景 0：current“在岗在岗 慢慢来不急”；experimental“来了来了 兄弟们慢慢搞”。
- 扩展场景 8：current“这手法听着确实专业 想试试效果”；experimental“现在还接吗有空过来”。
- 所有输出通过当前 cleaner；每轮前后 Action、Attempt、remote id 完全一致，脚本未调用 Telegram Gateway。
- 当前只证明候选方案能改变表达，尚未证明稳定更好。下一步必须让独立 Judge 在冻结上下文内做位置交换 pairwise，重点检查上下文贴合、真人感、模板腔、事实与交易越界。
- offset 16：current“这手法听着确实有两下子 啥时候来一发”；experimental“治疗级手法 这谁顶得住啊”。独立 M2.5 Judge 交换 A/B 位置后两次均判整体 TIE。
- Judge 第一次尝试曾返回不符合锁定枚举的 JSON，脚本 fail closed；随后相同严格合同返回合法结果，不做静默修复。
- 干跑期间生产 worker 继续真实推进，Attempt 与 remote id 各增加 1。前后计数仅用于发现并发活动，不再作为无写入证明；脚本改为检查 PostgreSQL `transaction_read_only=on`，任何 ORM 写入都会被数据库拒绝。
- offset 0：current“洗完脸清醒多了 上班搭子们都在不”对 experimental“来来来推荐啥”；两次位置交换映射后均为 current 胜，confidence 0.7。
- offset 8：current“兄弟们都不急 那我就慢慢来吧 在岗就行”对 experimental“在岗的都来报个到呗”；交换位置后 Judge 结论相反，按合同降为 TIE/confidence 0.5。

阶段汇总：

- Judge 场景数：3。
- current win：1。
- experimental win：0。
- TIE：2。
- 结论：简单把最近 accepted 学习短句作为 few-shot，并固定单个 speech-act，没有证明优于当前链，`feasibility=failed_for_simple_injection`，不得据此进入代码。
- 下一候选限定为：结构化风格统计 + 同一 slot 三候选 + 独立选择 + pairwise；若仍不能稳定胜出，停止开发并保持 `unproven`。

## 第二轮：结构化风格 + 三候选影子实验

实验链：

1. 从生产最近 200 条 accepted `group_chat` 样本中取得 196 条安全样本，只提取统计特征，不把学习群事实写入画像。
2. 生产样本统计为：中位长度 5 字、P80 6 字、8 字以内占比 92.9%、问句占比 0、句末标点占比 0。
3. MiniMax-M3 为同一个 slot 分别生成 `short_react / specific_question / light_disagree` 三个候选，并按五维固定权重选择。
4. 独立 `mimo-v2.5` 对 selected 与 current 做五维直评分；交换 A/B 位置，结论不一致时强制记 TIE。
5. PostgreSQL 全程 `transaction_read_only=on`，不创建 Action/Attempt，不调用 Telegram Gateway。

逐场结果：

| offset | current | selected | 正序 / 换位 | 最终 |
|---|---|---|---|---|
| 0 | 都在附近嘛 有空滴滴🤏 | 刷屏有点快了吧 | experimental / experimental | experimental win |
| 8 | 美腿丝足这话题今天有人聊吗？ | 六百六十六是啥梗 | current / TIE | TIE |
| 16 | 包时不限次那还挺划算的 不过精神头是真足啊 | 睡醒就这么精神啊 | current / experimental | TIE |

结果汇总：

- experimental win：1。
- current win：0。
- TIE：2。
- offset 0 暴露 current 会引入上下文未支持的“附近/滴滴”事实与联系暗示；多 speech-act 候选为避开编造提供了有效空间。
- offset 8/16 的独立终评仍存在位置敏感，按预设合同降为 TIE，没有把不稳定结果包装成胜出。
- MiniMax-M2.5 在实验中分别出现漏 `overall_winner` 和非法 JSON，均严格中止；它的结构化合同稳定性不足，已从候选链淘汰。

## 阶段结论与闸门

- `production_readonly_diagnosis`: `pass`
- `no_telegram_shadow_generation`: `pass`
- `simple_few_shot_feasibility`: `failed`
- `structured_candidate_set_technical_feasibility`: `pass`
- `structured_candidate_set_quality_superiority`: `unproven`
- `product_design_gate`: `not_entered`
- `dev_gate`: `not_entered`
- `qa/release/telegram_effect`: `not_done`

第二轮证明“三候选 + 结构化风格”技术上可运行，并在一个真实场景稳定胜出，但 3 个场景只有 1 胜 2 平，未达到“稳定优于当前链”的预设成功标准。因此本轮停止在 `prod-diagnosis`，不更新 PRD、不修改业务代码、不进入 QA/发布。当前最明确的生产根因仍是：学习来源两个月未更新、画像提炼丢失绝大多数结构化信息、`group_ai_chat` 生成 Prompt 根本不消费学习画像。

## 第三轮：回复锚点优先的影子实验

为验证效果差是否只因候选选择方式，第三轮做了以下调整：

1. 分开测试普通聊天上下文与生产真实 reply target。
2. 先从上下文中选择可校验的回复锚点，再生成不同 speech-act 候选。
3. 把 current baseline 放入候选池；实验候选没有明确优势时保留 current。
4. 使用 Grok JSON Schema Judge 做 A/B 与 B/A 两次位置交换盲评。
5. 对代表场景再做逐候选隔离 pointwise 评分，校准 current 固定在第一位造成的选择偏置。

anchor-first 结果：

| 场景 | offset | selector | 位置交换终评 | 最终 |
|---|---:|---|---|---|
| 普通聊天 | 0 | current | 无需 Judge | TIE |
| 普通聊天 | 8 | current | 无需 Judge | TIE |
| 原生 reply | 0 | current | 无需 Judge | TIE |
| 原生 reply | 8 | experimental | experimental / experimental，confidence 0.85 | experimental win |
| 原生 reply | 16 | current | 无需 Judge | TIE |
| 原生 reply | 24 | current | 无需 Judge | TIE |

阶段汇总为 experimental 1 胜、current 0 胜、5 平。将 current 纳入候选池能避免强制替换造成退化，但只在 1/6 场景测得提升，不能证明稳定增益。

候选池汇总还发现 current 固定在第一位时存在 selector 偏置。对普通聊天 offset 8、reply offset 8、reply offset 24 做隔离 pointwise 重评分后：

- 普通聊天 offset 8 仍保留 current，最终 TIE。
- reply offset 8 改选另一条 experimental 候选，Grok 两次位置交换仍判 experimental，平均 confidence 0.83。
- reply offset 24 仍保留 current，最终 TIE。

因此 reply offset 8 的提升不依赖批量排序或某一条偶然措辞，但其余代表场景仍没有改善。

## 真人回复关系数据核验

第三轮进一步定位到学习数据结构问题：

- `GroupContextMessage` 与 `TenantLearningSample` 均没有 reply、parent 或 quote relation 字段。
- 群学习采集只保存单条消息文本；现有 `GroupMessageSnapshot` 在适配层丢弃 Telegram 原生 `reply_to_msg_id`。
- 频道 discussion reply 虽有父消息 id，但进入租户学习样本时仍只保存子评论文本，没有父正文。
- 生产只读 inspector 确认 relation columns 为空；现有 590 条学习样本全部是 `group_chat`，其中 accepted 500、rejected 90，没有可用于学习接话策略的 parent/reply pair。

为验证原生数据是否存在，本轮复用生产已授权 listener，只调用 Telegram `get_messages(limit=200)`，不调用任何 send API。最近 200 条消息只取得 4 个可用且已脱敏的真人 reply pair；数据库事务保持只读，临时 pair 未写回生产或仓库。

这说明当前“AI 学习群”能学到的主要是孤立短句的长度和表面语气，无法学习“别人说什么时，真人如何接话”。4/200 的临时 pair 只能支持方向性实验，不能构成稳定的回复策略样本集。

## Pair-aware 极小样本实验

本轮将 4 个临时 reply pair 仅作为回复策略示例；事实来源仍严格限定在当前冻结上下文。使用 M3 生成、隔离 pointwise 选择和 Grok 位置交换终评：

| reply offset | 结果 | 说明 |
|---:|---|---|
| 8 | experimental / experimental，confidence 0.80 | 唯一原始胜出候选含重复疑问词，表达拗口 |
| 24 | TIE | 加入重复疑问词流畅度门后保留 current |
| 32 | TIE | 加入重复疑问词流畅度门后保留 current |

offset 8 的候选出现同一疑问词重复，暴露 LLM Judge 可能高估“会追问”而低估语句自然度。加入“同一疑问词不可重复”的确定性流畅度门后，该候选应判无效。因此：

- 原始汇总：experimental 1 胜、current 0 胜、2 平。
- 按最终流畅度门计算：有效样本 0 胜、0 负、2 平；唯一原始胜出不纳入上线证据。

为避免围绕 4 个 reply pair 继续调 Prompt 造成过拟合，本轮停止更多 Provider 实验。

## 最终结论与证据闸门

- `production_runtime`: `pass`
- `production_readonly_diagnosis`: `pass`
- `no_telegram_shadow_generation`: `pass`
- `native_reply_pair_read_probe`: `pass`
- `ordinary_chat_quality_superiority`: `unproven`
- `anchor_first_reply_quality_superiority`: `unproven`
- `pair_aware_quality_superiority`: `unproven`
- `reply_pair_learning_data`: `blocked_by_schema`
- `product_design_gate`: `not_entered`
- `dev_gate`: `not_entered`
- `qa/release/telegram_effect`: `not_done`

线上测试证明：仅调整 Prompt、短句 few-shot、候选数量或 selector，均无法稳定提高 AI 聊天与回复的拟人感。当前首要根因不是“Prompt 还不够像真人”，而是学习链没有保存真人对话关系，同时学习来源长期未更新、画像结构化信息为空、`group_ai_chat` 又不消费学习画像。

本轮只新增可复现的诊断脚本和运行记录，没有修改业务代码、生产数据或 Telegram 消息；质量优势未达到预设闸门，因此没有进入 PRD、开发、QA 或发布阶段。确认无相关进程后，本轮宿主与容器中的两个精确临时目录已删除，临时 reply pair 和原始影子结果未保留在线上。

## 第四轮：Prompt 与回复对之外的替代方向审计

### 生产只读证据

本轮不再调用 Provider 生成新文案，只审计 7 天生产行为、画像和现有决策合同。查询使用 PostgreSQL 只读事务，不创建 Action/Attempt，不调用 Telegram Gateway。

| 观察面 | 生产证据 | 结论 |
|---|---:|---|
| Planner -> Dispatcher 会话模式 | 最新 2,000 条中约 77.5% 从 `reply` 被重算为 `idle_warmup` | 合同漂移已证明；它影响后置质量门，但尚未证明是文案差的唯一根因 |
| 多账号连续发言 | platform run P50=2、P90=5、P95=8；55.6% 至少连续 2 条 | 轮转有效，但多账号 AI-only burst 明显，缺会话级 `wait/send` 决策 |
| 同账号连发 | 7 天仅 2 次相邻同账号 | speaker rotation 不是当前首要缺口 |
| 自动质量结果 | 最新 2,000 条中 1,997 条为 accepted | 当前规则通过率与真人盲评、用户观感脱节 |
| Voice profile 区分度 | 884 个 active profile 中 478 个摘要精确重复，重复率 54.1% | 非空画像不等于账号可辨识 |
| Persona 相似度校验 | 有分数的 402 个全部为 100，但校验批次大小仅 2 | 仅小批内去重，未做租户全局碰撞检查 |
| AI 后真人接话 | 3,032 个 platform run 中 99.97% 后续出现真人，P50 23 秒 | 原始指标被高活跃群饱和，不能归因于 AI 内容 |

Action 快照在并发生产中持续变化，因此 77.5% 是本次只读快照值；相邻快照约为 76.25%–77.5%，不影响“绝大多数普通上下文被重算为 idle warmup”的判断。最长 platform run 是跨时段统计的离群值，不解释为短时间连续发送 786 条。

### 方向优先级

| 优先级 | 方向 | 为什么优先 | 首个可证伪实验 |
|---|---|---|---|
| P0 | 会话级 `wait/send + reason` 调度 | 当前只有单账号轮转，没有独立判断此刻是否值得说；AI-only run 已有生产证据 | 对历史 SpeakerTurn 做影子回放，比较现有调度与价值调度的发送率、连续平台段和欠账影响 |
| P0 | 修复 Planner -> Dispatcher `chat_mode` 合同 | 约 77.5% 模式漂移，且 idle 质量门忽略已有 anchor ids | 同一批 Action 分别按 planned/derived mode 重放质量门，只比较接受/拒绝差异，不发消息 |
| P1 | 租户全局 persona 去重 + self-history 消融 | 54.1% profile 摘要重复；最近 AI 文案又被拼回未来画像，可能强化模板化 | 同上下文、不同账号做盲测 author identification，对比保留/移除 AI self-history 与全局 archetype 分配 |
| P1 | 多个安全候选的真人校准排序 | 当前首个过硬门候选即接受；99.85% 自动 accepted 无法区分真人感 | 先用小规模真人 pairwise 标注校准 selector，再做位置交换离线对比 |
| P2 | 随机静默 holdout / matched control | next-human 原始指标几乎饱和，现有库又缺真人 reply parent | 同群同小时比较“本可发但静默”和“实际发言”，归一化真人消息率与延迟 |
| P3 | 素材混合 A/B | 图片、sticker、emoji 链已经存在 | 仅作为 outcome A/B，不扩建素材系统 |

### 审计结论

- `alternative_direction_inventory`: `pass`
- `should_speak_gap`: `proven`
- `chat_mode_contract_drift`: `proven`
- `global_persona_distinctiveness_gap`: `proven`
- `human_calibrated_selector`: `missing`
- `behavioral_causal_metric`: `blocked_by_holdout_and_reply_relation`
- `business_code_change`: `not_done`
- `production_humanization_effect`: `unproven`

本轮找到的核心新方向不是继续让单句“更口语”，而是把真人感拆成四个独立问题：什么时候说、连续说多少、谁在说、过安全门后哪条最值得说。公开多方对话研究也普遍把发言时机与文本生成分开，并用候选后评估和真人标注校准质量选择。以上方向已有生产证据支撑排序，但尚未经过受控线上效果实验，因此仍停在 `prod-diagnosis`，不进入 PRD、开发、QA 或发布。

确认无相关 inventory 进程后，本轮宿主和 backend 容器中的两个精确临时目录均已删除；线上未保留本次聚合结果。

## 第五轮：可在线测试项与待修复 PRD 分流

### 已完成的生产只读测试

| 测试 | 输入与边界 | 结果 | 判定 |
| --- | --- | --- | --- |
| planned/derived `chat_mode` 最终消息重放 | 最近 7 天 4,559 条有最终正文 Action；PostgreSQL read-only；无 Provider/Telegram | 3,825 条存在 mode drift，但 planned/derived 两种质量门均接受全部最终正文 | `pass_but_inconclusive`：库内只保留通过候选，无法观测误杀 |
| platform run budget 敏感性 | 3,072 个 platform run、10,655 条 platform turn；仅历史回放 | budget 1/2/3 分别保留 28.83%/44.82%/54.56%；5 个观测任务全部受损 | 固定连续条数 cap `rejected` |

固定 cap 只能把 run P90 从 5 压到 1/2/3，却会同时丢失 71.17%/55.18%/45.44% 的现有平台发言；它没有理解任务欠账或当前内容价值，不能作为真人化修复。

最终消息 chat-mode 重放没有发现判定差异，也不能反证合同漂移无害：被质量门拒绝的 Provider 候选不会持久化到 Action。为补这个观测缺口，本轮新增冻结生产上下文、只生成不发送的候选池脚本，计划对同一候选分别执行 planned/derived mode。

### 尚未完成的线上影子测试

候选池首次六场景前台执行留下 0 字节 stdout/stderr，容器无已确认结果；改为单场景后，生产 SSH 连续出现 `Connection timed out during banner exchange`。相同连接阻断累计三次后停止重复启动，避免无法确认远端进程时叠加 Provider 调用。

| 测试 | 当前状态 | 未宣告的结论 |
| --- | --- | --- |
| chat-mode 冻结候选池双重质量门 | `blocked_by_production_ssh` | 未证明 anchored false reject 比例 |
| current persona vs 去除 `近期表达` self-history | `blocked_by_production_ssh` | 未证明消融提升或退化 |
| 真人校准 selector | `blocked_by_human_gold_set` | LLM Judge 不能替代 gold set |
| 随机静默 holdout | `blocked_by_live_traffic_authority` | 未改变生产真实发言流量 |
| 显式真人回复率 | `blocked_by_reply_relation_schema` | next-human 不能当作真实回复 |

persona A/B 脚本已经本地完成 py_compile 与工程硬限制检查，严格保留 mask/identity/preference/stance，只删除 `近期表达` self-history，并使用不同模型做位置交换评估；它尚未在生产执行，不能记录任何质量赢家。

### 待修复 PRD 落点

以下文档已同步，但只形成 repair backlog，不是开发交接：

- 专项 PRD §14：H-01 chat-mode 单一真相源、H-02 value-aware should-speak、H-03 persona 稳定/动态分层、H-04 真人 gold selector、H-05 reply relation、H-06 静默 holdout、H-07 节奏指纹、H-08 实际成稿趋同、H-09 single-message late binding。
- 总 PRD：明确固定 cap 已否决，其余状态为 `online_shadow_in_progress|pending|blocked`。
- DF-183：明确 2026-07-31 新范围尚未改变当前数据流，待 Product Design Complete 后再补正式实现映射。

新增修复范围状态为 `humanization_speech_repair_design_status=partial`。2026-07-27 既有群管准入/账号轮换范围仍为 complete，但不得把旧 complete 状态用于本次新增真人话术开发。

### 本轮闸门

- `production_readonly_replay`: `pass`
- `fixed_platform_run_cap`: `failed`
- `chat_mode_candidate_shadow`: `blocked_by_production_ssh`
- `persona_self_history_ab`: `blocked_by_production_ssh`
- `deferred_repair_prd`: `pass`
- `business_code_change`: `not_done`
- `production_data_write`: `none`
- `telegram_operation`: `none`
- `production_humanization_effect`: `unproven`

生产临时目录 `/tmp/ai-humanization-test-host.8mK3rI` 和 `/tmp/ai-humanization-test-container.ex8BEn` 尚不能通过 SSH 复核/删除。已知文件仅包含诊断脚本、聚合 JSON 与 0 字节结果/错误文件，不包含本轮输出的生产原始上下文；清理状态保持 `blocked`，不写“已删除”。

## 第六轮：节奏、实际成稿与 late binding

### 新方向与静态证据

| 方向 | 当前证据 | 首个线上测试 | 状态 |
| --- | --- | --- | --- |
| 真人节奏指纹 | 已有 AI-only run 分布，但没有 human/platform gap、整秒尖峰和 schedule lag 对照 | 7 天只读匿名聚合，按群/小时分层 | `blocked_by_production_ingress` |
| 实际成稿趋同 | 画像摘要 54.1% 精确重复；实际发送文本尚未按账号比较 | 统计句长、问句、标点、emoji、exact/prefix collision | `blocked_by_production_ingress` |
| single-message late binding | 当前 worker `GENERATION_BATCH_SIZE=10`；同批只刷新一次上下文，一次请求多个账号 slots | 同一冻结起点比较 batch 预写与逐条刷新上下文生成 | `blocked_by_production_ingress` |

静态调用链只证明当前存在批量预写合同，不能证明它是用户观感差的唯一原因，也不能证明逐条生成一定更好。节奏与成稿脚本已按只读事务、匿名聚合、无 Telegram 边界准备，但没有上传或执行生产。

### 生产替代入口补证

- Silicon SSH 直连两次均在 banner exchange 超时；经 `prod-malaysia-admin` 跳板仍相同，达到三路径失败阈值后停止重试。
- 已登录生产任务中心缓存页可打开；点击“刷新当前数据”后返回 `读取任务列表失败：request timeout`。残留的任务数和 AI 活群日量不是本轮最新事实，不用于效果结论。
- 公网 `GET https://tgyunying.telema.cn/api/health` 在 10 秒内连接超时，HTTP code 为 000。
- 未点击启动、暂停、重试、重置、停止、删除或配置；未调用 Telegram，也没有生产数据库写入。

### 本轮闸门

- `current_batch_contract`: `pass_static_only`
- `production_timing_inventory`: `blocked_by_production_ingress`
- `production_realized_style_inventory`: `blocked_by_production_ingress`
- `late_binding_provider_ab`: `blocked_by_production_ingress`
- `production_snapshot_freshness`: `unproven`
- `repair_prd_h07_h09`: `pass`
- `business_code_change`: `not_done`
- `production_data_write`: `none`
- `telegram_operation`: `none`
- `production_humanization_effect`: `unproven`

专项 PRD 已增加 H-07～H-09，并同步总 PRD 与 DF-183；状态仍为 `humanization_speech_repair_design_status=partial`，不进入开发、QA 或发布。Phase 3D 精确临时目录仍因 SSH 不可达而无法复核清理，状态保持 `blocked`。

## 第七轮：上下文新鲜度、同批编排与问句话轮

### 恢复与安全边界

- 2026-07-31 19:09 CST，`prod-silicon-root` 重新可用；主机 uptime 23 分钟，current release 仍为 `20260730220853_997e884b`，backend 与 AI 相关 worker healthy。
- 两个生产脚本均先执行 `SET TRANSACTION READ ONLY` 并复核 `transaction_read_only=true`；只输出匿名聚合，不输出群聊原文，不调用 Provider、Telegram Gateway，不创建 Action/Attempt。
- runtime 恢复只记 `pass`，不得解释为真人化效果恢复。
- 收口前确认无相关诊断进程，精确删除本轮 Phase 3F、Gateway 严格复测与先前 Phase 3D 的宿主/容器临时目录并验证不存在；随后 backend health=`healthy`、宿主 `/api/health` 返回 `ok`。

### 线上只读结果

| 指标 | 生产结果 | 结论 |
| --- | --- | --- |
| schedule lag | 5,329 条成功 Action，P50=127 秒、P90=1,145 秒 | 长等待是上下文过期风险，不等于单条生成已胜出 |
| 实际成稿长度 | 平台账号 P10/P50/P90=12.4/13.6/14.6 字；真人=3.0/4.76/12.4 字 | 平台输出集中在窄长度带 |
| 跨账号风格 | 平台四字开头碰撞率 66.78%，真人 55.57%；平台账号间距离 P90=0.3482，真人 1.0865 | H-08 问题证据 `pass`，真人偏好仍未证明 |
| 上下文新鲜度 | 5,000 条成功 Action 中 3,058 条在 Gateway 调用前出现新真人消息，过期率 61.16%；1,058 条超过配置阈值 | H-09 send-time revalidation 必要性 `pass` |
| 过期延迟 | 过期 Action 的 schedule-to-gateway-start P50=2,219.1 秒、P90=17,257.0 秒 | 旧批次正文可能在数十分钟到数小时后才进入 Gateway |
| 同批编排 | 7,672 个相邻平台对中 23.55% 在 5 秒内；6.92% 共享 generation，同 generation 内 25.05% 在 5 秒内 | 编排指纹存在，影响低于上下文过期/问句抢话 |
| 问句话轮 | 269 个平台问句中 166 个在 60 秒内被另一平台账号接管，61.71%；300 秒口径 71.38% | H-10 问后让出话轮问题证据 `pass` |

human-to-human turn 有 83.7% 落在同一秒，说明 listener 批量入库或时间精度污染；该项不能作为真人节奏学习基线。60 秒问句指标用于排除长时间后续消息造成的假阳性，不是拟上线的固定 sleep/cap。

### 本轮闸门

- `production_runtime_after_recovery`: `pass`
- `production_timing_inventory`: `pass_with_human_timestamp_precision_limit`
- `production_realized_style_inventory`: `pass_problem_proven`
- `production_snapshot_freshness`: `pass_problem_proven`
- `production_batch_choreography_inventory`: `pass_problem_proven`
- `production_question_floor_inventory`: `pass_problem_proven`
- `late_binding_provider_ab`: `pending`
- `human_gold_preference`: `blocked_by_human_gold_set`
- `business_code_change`: `not_done`
- `production_data_write`: `none`
- `telegram_operation`: `none`
- `production_humanization_effect`: `unproven`

专项 PRD 已用线上证据更新 H-07～H-09，并新增 H-10；同步总 PRD 与 DF-183。新范围仍为 `humanization_speech_repair_design_status=partial`，本轮没有进入 dev、QA、Release 或真实 Telegram 发送。

## 第八轮：跨群内容隔离硬安全审计

### 用户零容忍口径

- A 群采集、生成和动态记忆的内容只能发送到 A 群，B 群同理；该项是独立硬安全门，不参与真人感平均分。
- 历史 mismatch 为 0 不能证明未来绝对安全；必须同时有代码不变量、损坏 payload 红测和 Provider/Gateway 前 fail-closed。
- 本轮只读、不发送、不写库，不输出群聊原文。

### 静态审计

- 正向：最新群上下文和 reply target 已按 tenant/group 查询；AI message memory、stance、SpeakerTurn 均有 group 字段或 group key；Gateway peer 由 payload group 对应的 TgGroup 得到。
- 失败：`_recent_account_memories()` 在当前任务历史不足时会读取同账号其他群任务成功正文，最多 80 字写入 Prompt；`account_profile_summaries()` 也跨群读取近期正文，最多两条各 60 字写入“近期表达”。
- 缺口：Gateway 前没有统一断言 Task target、Action payload、context/snapshot/reply、memory/cache、SpeakerTurn 和最终 TgGroup 全部属于同一 `tenant_id + group_id`；相应损坏数据红测缺失。

### 生产只读结果

| 检查面 | 样本与结果 | 状态 |
| --- | --- | --- |
| Action/Task/TgGroup | 30 天 26,435 条；target/tenant mismatch=0，成功完整性失败=0 | `pass_zero_observed` |
| Context/snapshot | 1,183,486 个 context 引用、26,435 个 snapshot；非法、缺失、tenant/group mismatch 全为 0 | `pass_zero_observed` |
| Memory/SpeakerTurn | 11,826 个 linked memory、10,784 个 platform turn；scope mismatch 全为 0 | `pass_zero_observed` |
| 跨群完全相同正文 | same-account cross-group exact reuse pair=0 | `pass_zero_observed` |
| 跨任务原文进入 Prompt | 2,696/7,488 条成功 Action 明确带“跨任务”account memory，涉及 5 个群 | `failed` |
| 多群账号近期表达上界 | 7,444/7,488 条 Action 来自有多群历史的账号且 profile 含近期表达 | `risk_upper_bound` |

### 本轮闸门

- `historical_relation_mismatch`: `pass_zero_observed`
- `historical_exact_cross_group_send`: `pass_zero_observed`
- `cross_group_prompt_raw_content_isolation`: `failed`
- `pre_provider_scope_invariant`: `missing`
- `pre_gateway_scope_invariant`: `missing`
- `future_zero_cross_group_guarantee`: `unproven`
- `production_data_write`: `none`
- `telegram_operation`: `none`
- `business_code_change`: `not_done`

专项 PRD 新增 H-11，并同步总 PRD 与 DF-183。当前不能宣称“绝不会串群”，新范围继续 `humanization_speech_repair_design_status=partial`，禁止进入 dev/QA/Release。临时审计目录已精确清理，清理后 backend healthy、宿主 health=`ok`。

## 第九轮：确定性修复与本地 E2

### 实施结果

- 新增 `group_content_scope_v1`，Provider 前与 Gateway 前共用 tenant/Task/group/peer/context/reply/memory/account 校验；Task 直接绑群与运营目标绑群均以真实 Telegram peer 收口。
- 移除跨任务/跨群原文进入 `account_memory` 和近期表达的路径；仅保留独立 voice profile 中的稳定风格摘要。
- normal 生成改为单 Action late binding，generation worker 每次只 claim 一个 Action。Gateway 前发现新真人上下文时过期旧 memory/cache，仅重排当前 Action，已删除整 Cycle sibling skip。
- normal Provider 前增加 listener watermark proof；不可证明时保持 pending，写 `context_freshness_unproven`，并补偿 worker 30 分钟 lookahead 避免立即重领。
- should-speak/question-floor 只写影子 `wait|send + reason + watermark + next_eligible_at`，本轮不改真实流量和义务。
- 普通六轮失败精确签到仅允许静态开关开启、非 reply、无素材义务且绑定主数量槽；缺面具 coverage 继续走独立账本合同，并执行同账号 10 天签到去重。
- 规则绑定缺失写可观测 blocker；任务投影增加 `conversation_quality_status`，只有 E4 旗标可为 `met`。

### 验证分层

| 门 | 证据 | 状态 |
| --- | --- | --- |
| 定向 + 相关无 PostgreSQL 回归 | 235 passed（169 + 66） | `pass` |
| 语法/差异检查 | 改动模块 `py_compile`、`git diff --check` | `pass` |
| 真 PostgreSQL 并发 | 当前环境缺 `TEST_DATABASE_URL/DATABASE_URL`，reset 失败 | `blocked` |
| release / deploy | 未执行 | `not_started` |
| 真实 Telegram 不串群 | 无新 E4 发送证据 | `unproven` |
| 真人接话/话术改善 | should-speak 仍为 shadow，无 canary/连续任务日 | `unproven` |

本轮已进入 dev 并完成本地 E2 的确定性切片；不写 `qa_pass`、`product_accepted` 或 `production_fixed`。

## 第十轮：修复后遗漏审计与补强

### 新发现与修复

- 历史 Action 四个 scope 字段全空时，旧实现会误写 `cross_group_content_scope_mismatch` 并只终结 Action；现改为精确 `scope_contract_missing`，由既有 finalize 把原 CycleSlot 置 `replan_required`、原主数量槽重新 `open`。部分缺失或值不一致仍保持跨群硬失败。
- 六轮失败的静态 `签到` 原先只检查主数量槽、reply 和 `material_intent`；现由数据库查询当前 `ContentMixCycleSlot` 是否仍有 `pending` 内容义务，存在任何义务时禁止签到。
- 主/备用 Provider 3+3 原先没有 deadline budget；现每次 attempt 启动前复用实际 AI request timeout，并与主数量槽所属任务日 deadline 比较。预算不足写 `ai_generation_deadline_budget_exhausted`，未执行轮次不算失败、不能触发签到；原 CycleSlot/主数量槽直接 terminal，内容义务 shortfall，不回 replan 空转。
- 首轮扩大回归暴露 UTC ledger deadline 与北京时间 naive `_now()` 的 8 小时误判；已统一把 ledger UTC deadline 转为北京时间 wall-clock 后比较。
- 生成事务专项遗留的 sibling 批量 claim/生成合同已删除，并同步 Provider fallback、全任务履约、DF-183 与结构索引。

### 验证与剩余边界

| 门 | 证据 | 状态 |
| --- | --- | --- |
| 新增红测 | legacy scope、内容义务、deadline budget 首次 3 failed/1 passed；实现后全绿 | `pass` |
| 相关无 PostgreSQL 回归 | 254 passed、38 deselected | `pass` |
| 语法与工程硬限制 | py_compile、`git diff --check`、改动函数长度/位置参数/复杂度 | `pass` |
| 真 PostgreSQL | `TEST_DATABASE_URL` 目标名通过 test 安全校验，但远程连接超时，0 tests run | `blocked` |
| listener cursor-gap | 普通 listener 尚未持久化连续 remote cursor，不能证明无断档 | `blocked_design_and_migration` |
| 三维 acceptance | 工程仍无统一 quantity/content-mix/quality 组合读模型 | `not_implemented` |
| release / Telegram E4 | 未发布、未发送 canary | `not_started / unproven` |

本轮没有生产写入、没有 Telegram 调用、没有发布；因此只声明代码与本地 E2 补强，不声明线上已修复或真人感已提升。
