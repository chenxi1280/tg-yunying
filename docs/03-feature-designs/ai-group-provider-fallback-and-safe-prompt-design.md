# AI 活跃群安全 Prompt 与多模型回退设计

> **2026-08-04 current contract：** 本文只保留输入安全、统一输出契约和 Provider adapter 原则。全系统只允许一个 active `ai_provider_key_version`；所有文本模型共享这一把 key 和总额度，每个 GenerationJob 独立调用、direct 结果独立提交。固定 M3→M2.5→Grok 拓扑、Grok CLI Bridge、按模型/配置复制 key 额度、验证码 AI/VLM、ContentMix 数量槽和旧双签到均为 `historical_do_not_implement`。当前并发、key 轮换和签到合同以 `task-fulfillment-contract-closure-prd.md` §8 与 `ai-group-daily-group-target-redesign-prd.md` §7.4 为准；搜索验证码只走 RapidOCR→ddddOCR。

> **2026-08-31 scoped supersede：** 本文 §1、§3.1、§3.2 中对地点、服务和行业黑话的全面禁止只适用于普通 route。任务经 `is_adult_content_config()` 明确授权为成人 route 后，其行业词汇、Prompt、8～20 汉字质量门和事实锚定以 `ai-group-chat-quality-and-token-optimization-prd.md` v2.3 §4.1、§4.2、§4.4、§4.8 为准。该授权不放宽未成年人、联系方式、引流、露骨内容和无依据事实等绝对红线。

> **2026-07-31 supersede：** Provider 重试次数、静态签到适用范围、单 Action late binding 与跨群 scope 失败规则，以 `ai-conversation-humanization-and-group-bot-admission-prd.md` §15.2 为准。本文保留输入安全与 Provider 适配设计；下文“M3→M2.5→Grok 每层一次”和缺少数量槽/scope 校验的旧编排不再是运行合同。

## 1. 目标与范围

本设计仅覆盖 `group_ai_chat` 文本生成输入安全和 Provider 适配。模型由版本化 policy 从唯一 active Provider key 支持的 model ID 中选择；每个 GenerationJob 独立调用并可并发，不等待其他 sequence。版本化生成流程明确耗尽后，可按统一签到合同使用精确 `签到`，不得使用文本表情。搜索验证码不复用任何多模态 Provider。

本次同时统一安全上下文和 Prompt 口径：指令使用英文，安全动态上下文可以使用中文，模型输出必须是中文固定 JSON。普通 route 只允许明确成年人的非露骨身材、穿搭和轻度暧昧既有话题自然承接；显式成人 route 按上述 scoped supersede 处理。所有 route 都必须在生成前过滤未成年人、联系方式、引流和露骨内容。

## 2. 当前事实与目标状态

目标状态只维护一个 active Provider key version，Secret 保存密钥管理引用，不落明文。模型名称、健康和质量统计是同一 Provider key 下的 policy/观测维度，不创建第二把 active key，也不按模型复制总 `max_inflight/RPM/TPM`。0 个或多个 active key 都显式阻断生成。

## 3. 输入过滤与 Prompt 契约

### 3.1 动态字段过滤

生成输入按字段独立处理：

- 群标题：仅保留安全兴趣 / 日常标签，否则使用中性群标签。
- 账号 persona / 长期画像：仅保留表达风格和安全身份，不保留交易偏好、联系方式或具体服务描述。
- 话题方向 / 讨论老师：仅保留普通群聊和明确成年人的非露骨外貌 / 穿搭话题；危险项省略。
- 真人上下文：按短句切分，只保留最近五条安全短句；无安全短句时使用 `generic_warmup`。
- ID、账号槽位、节奏配置：原样保留，用于生产结构和审计，不参与自然语言推断。

### 3.2 允许与禁止

普通 route 允许已有上下文中的普通签到、积分、天气、城市、在场询问，以及明确成年人的漂亮、身材、曲线、腿长腿白、丝袜 / 高跟鞋、性感穿搭、撩人气质和成人活力评价。模型只能围绕原文做非露骨短评，不得扩大为亲密部位、性行为、可用性或服务能力。

普通 route 禁止价格、预算、付款、联系方式、私聊邀请、地点 / 酒店、预约、推荐资源、服务反馈、交易黑话、具体性行为，以及学生、少女、好嫩等未成年人或年龄歧义表达。显式成人 route 可以承接专项 PRD allowlist 内的非露骨行业词，但人物、经历、地点和服务反馈仍须来自清洗后的真实上下文；`generic_warmup` 不得附和、求推荐或声称这些事实。危险输入不得因为首选模型拒答而转交更宽松模型强行生成。

### 3.3 输出 JSON

输出顶层固定为 `decision`、`context_source`、`drafts`；`drafts` 必须只有一项，并固定包含 `sequence_index`、`reply_to_sequence_index`、`persona`、`content`、`risk_level`、`intent`、`mood`、`material_intent`、`allow_material`。不得输出 `<think>`、Markdown 围栏、解释或额外字段。所有 Provider 使用同一个解析和质量门禁。

## 4. 模型选择与统一签到

```text
sanitized generation request
  -> selected model on the single active Provider key
  -> common JSON parser and quality gate
  -> optional next approved model on the same key
  -> unified exact check-in
  -> planned message or visible skipped round
```

每个 GenerationJob 冻结 model policy revision、安全上下文和输出契约后独立执行。允许的模型次序、次数和 timeout 必须由版本化 policy 明示；不在文档或代码中固化 M3/M2.5/Grok 拓扑，不调用第二 Provider/CLI，也不做多模型 hedge。不同 Job 可同时使用不同模型，但都先领取同一 active key 的共享 token；Provider 明确存在模型级限制时再领取对应模型子 token。

签到必须使用统一 `content_source=check_in` 和精确正文 `签到`，绑定稳定数量义务与 remote mutation identity；账号未覆盖时可同时绑定 coverage。签到不进入普通正文 10 天去重，不计高质量正文或 reply，unknown 只复探不重发。只有生成流程明确耗尽或面具缺失等合同允许原因才能使用，不能掩盖 Provider key 缺失、quota、准入、target/context 或 transport 故障。

## 5. 已删除的 Grok CLI Bridge

Grok CLI Bridge 不进入当前运行拓扑，不部署、不健康检查、不作为回退，也不得持有第二套认证。历史章节只用于说明已废弃方案。

## 6. 数据与可观测性

每次生成 attempt 记录：

- `provider_key_version`、`requested_model`、`actual_model`、`model_policy_revision`、`generation_attempt_no`
- `fallback_reason` 和标准错误分类
- 每次开始 / 结束时间、耗时、共享 key token 和 Provider 健康快照
- JSON 解析、输入 / 输出规则、上下文锚定、重复和真人感门禁结果
- 最终 `generation_source`

action payload 只保存发送所需内容和非敏感审计摘要，不保存 Provider key 或完整思考过程。任务详情和生产诊断按模型、原因、统一签到和最终失败统计，发布门禁必须检查来源字段完整。

## 7. 错误处理与并发

- 单次模型调用超时后立即释放调用资源；policy 允许时进入下一模型，不在数据库事务内等待外部模型。
- 下一模型启动前以主义务 deadline 和真实 AI request timeout 校验 attempt policy；不满足时显式终止，不自动转签到。
- 同一生成 slot 使用稳定 request id，避免重规划并发产生重复 action。
- 回退成功只完成当前 slot；其他 slot 仍按各自结果审计。
- 统一签到仍要经过义务唯一性、准入、账号安全和 Telegram 发送前门禁。

## 8. 测试与发布验收

自动化测试至少覆盖：

1. 安全成年身材 / 轻度暧昧短句保留，交易和年龄风险删除。
2. 全系统 0/1/2 个 active key 分别为阻断/通过/唯一约束失败，模型配置不能激活第二把 key。
3. 多个 GenerationJob 使用不同模型并发时共享 active key 总额度，direct 后位不等待前位 sequence。
4. 版本化 model policy 允许时可切换同 key 下另一模型；禁止调用第二 Provider、Grok CLI 或 hedge。
5. 禁止输入不会通过回退链强行生成。
6. `<think>` 内容不会抢先被 JSON 提取器误认。
7. 统一签到关闭或原因不合法时生成失败可见，不产生替代文本；unknown 不重发。
8. 每次来源、原因和耗时写入诊断，且不泄露密钥。

发布走 `master -> release -> Deploy Production`。生产验收必须确认只有一个 active key version、多个模型共享总额度、Grok/第二 Provider 调用为 0，并分别取得生成成功、合法模型切换、统一签到和可见失败的受控证据。只有真实发送链继续通过账号、规则和 Telegram 门禁后才能判断任务恢复；Provider 健康或 dry-run 成功不能单独写成 `production_fixed`。

## 9. 回滚

新 writer 未产生 Gateway-started/unknown 前可停用新生成路径并回滚应用；一旦已有新远端事实只允许前向修复。key 轮换通过新 version 原子替换 active 行，旧 in-flight 按旧 version 对账；不得通过激活第二把 key 或恢复 Grok Bridge 回滚。
