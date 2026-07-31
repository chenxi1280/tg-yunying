# AI 活跃群安全 Prompt 与多模型回退设计

> **2026-07-31 supersede：** Provider 重试次数、静态签到适用范围、单 Action late binding 与跨群 scope 失败规则，以 `ai-conversation-humanization-and-group-bot-admission-prd.md` §15.2 为准。本文保留输入安全与 Provider 适配设计；下文“M3→M2.5→Grok 每层一次”和缺少数量槽/scope 校验的旧编排不再是运行合同。

## 1. 目标与范围

本设计仅覆盖 `group_ai_chat` 文本生成链。生产默认使用 `MiniMax-M3`，失败后依次尝试独立的 `MiniMax-M2.5` Provider、受限 `Grok 4.5` CLI Bridge，最后使用审核过的签到短句或文本表情。验证码图片识别继续复用现有多模态 Provider 选择，不进入本链。

本次同时统一安全上下文和 Prompt 口径：指令使用英文，安全动态上下文可以使用中文，模型输出必须是中文固定 JSON。明确成年人的非露骨身材、穿搭和轻度暧昧既有话题允许自然承接；交易撮合、联系方式、预约、服务、具体性行为和未成年人风险在生成前过滤。

## 2. 当前事实与目标状态

当前生产租户 1 只有一个健康 MiniMax Provider，记录模型为 `MiniMax-M2.5`；六个运行中的 AI 活跃群任务没有 `ai_model` 或 Provider 覆盖，因此继承 M2.5。生产 Linux 已安装并登录 Grok CLI 0.2.93，可用模型为 `grok-4.5`。

目标状态保留现有 M2.5 Provider，新增 M3 Provider 并设为租户默认。两条 MiniMax 记录可以复用同一服务端凭据，但各自保留模型名、健康、失败率和最近检查；不得通过每次请求临时改写同一 Provider 行模拟两个应用。

## 3. 输入过滤与 Prompt 契约

### 3.1 动态字段过滤

生成输入按字段独立处理：

- 群标题：仅保留安全兴趣 / 日常标签，否则使用中性群标签。
- 账号 persona / 长期画像：仅保留表达风格和安全身份，不保留交易偏好、联系方式或具体服务描述。
- 话题方向 / 讨论老师：仅保留普通群聊和明确成年人的非露骨外貌 / 穿搭话题；危险项省略。
- 真人上下文：按短句切分，只保留最近五条安全短句；无安全短句时使用 `generic_warmup`。
- ID、账号槽位、节奏配置：原样保留，用于生产结构和审计，不参与自然语言推断。

### 3.2 允许与禁止

允许已有上下文中的普通签到、积分、天气、城市、在场询问，以及明确成年人的漂亮、身材、曲线、腿长腿白、丝袜 / 高跟鞋、性感穿搭、撩人气质和成人活力评价。模型只能围绕原文做非露骨短评，不得扩大为亲密部位、性行为、可用性或服务能力。

禁止价格、预算、付款、联系方式、私聊邀请、地点 / 酒店、预约、推荐资源、服务反馈、交易黑话、具体性行为，以及学生、少女、好嫩等未成年人或年龄歧义表达。危险输入不得因为首选模型拒答而转交更宽松模型强行生成。

### 3.3 输出 JSON

输出顶层固定为 `decision`、`context_source`、`drafts`；`drafts` 必须只有一项，并固定包含 `sequence_index`、`reply_to_sequence_index`、`persona`、`content`、`risk_level`、`intent`、`mood`、`material_intent`、`allow_material`。不得输出 `<think>`、Markdown 围栏、解释或额外字段。所有 Provider 使用同一个解析和质量门禁。

## 4. 回退编排

```text
sanitized generation request
  -> MiniMax-M3 Provider
  -> MiniMax-M2.5 Provider
  -> Grok CLI Bridge (grok-4.5)
  -> static safe check-in / text emoji
  -> planned message or visible skipped round
```

回退触发条件包括调用异常 / 超时、配额或未知模型错误、空回复、拒答、JSON 解析失败、候选不足、交易 / 年龄残留、上下文不锚定、重复或真人感质量失败。运行合同统一为主 Provider 最多 3 次、不同备用 Provider 最多 3 次；相同 attempt 使用同一 scope 下的冻结安全上下文和输出契约，只有 Provider 适配参数不同。Grok 若被配置为备用实现也受备用阶段总次数约束，不再形成第三套独立重试层。

静态兜底在群聊路径上收敛为精确文本 `签到`（见专项 PRD §15.2.8），必须标记为 `static_safe_fallback` / `content_source=check_in_fallback`，不得伪装成模型成功。只有绑定主数量槽、命中明确允许原因、通过 10 天去重，且该 `ContentMixCycleSlot` 没有任何 `pending` 内容义务时才可进入；不能只凭 `material_intent` 为空推断义务已转派。`cross_group_content_scope_mismatch`、`scope_contract_missing`、deadline budget 未执行满六轮、规则/target/context 合同错误和缺数量槽永久禁止签到。历史“我也来签到啦～”类扩写句与 `emoji_react` 表情池不得再作为确定性兜底。租户可关闭静态兜底；关闭后全链失败写可见错误。

## 5. Grok CLI Bridge

Bridge 是低频第三层，不替代标准 HTTP Provider。它固定 Grok CLI 版本和 `grok-4.5`，禁用 web search、memory、subagents 和所有工具，使用独立临时 Git 工作目录、单请求硬超时和有界并发。Backend 通过内部受限接口调用 Bridge；Bridge 只接受已过滤的 system/user Prompt，并返回原始文本、退出码、stop reason 和耗时。

Bridge 不得接触 Telegram session、数据库或 Provider 密钥；认证目录只在 Bridge 运行用户可读。CLI 失败、未登录、额度不足、输出为空或 JSON 不合格时进入静态兜底。部署和健康检查必须验证 CLI 版本、登录状态和 `grok-4.5` 可用，但不得把登录信息写入 API 响应。

## 6. 数据与可观测性

每次生成 attempt 记录：

- `requested_model` / `actual_model`
- `fallback_stage`：`primary_m3`、`fallback_m25`、`fallback_grok`、`static_safe_fallback`
- `fallback_reason` 和标准错误分类
- 每层开始 / 结束时间、耗时、Provider / Bridge 健康快照
- JSON 解析、输入 / 输出规则、上下文锚定、重复和真人感门禁结果
- 最终 `generation_source`

action payload 只保存发送所需内容和非敏感审计摘要，不保存 Provider key、Grok 登录态、完整思考过程或 CLI stderr。任务详情和生产诊断按来源统计成功、回退、静态兜底和全链失败，发布门禁必须检查近 24 小时样本中来源字段完整。

## 7. 错误处理与并发

- 单层超时后立即释放调用资源并进入下一层，不在数据库事务内等待外部模型。
- 下一层启动前以主数量槽任务日 deadline 和真实 AI request timeout 校验 attempt budget；预算不足显式终止，不进入静态签到。
- 同一生成 slot 使用稳定 request id，避免重规划并发产生重复 action。
- 回退成功只完成当前 slot；其他 slot 仍按各自结果审计。
- 静态兜底仍要经过重复、发送频率、账号容量和 Telegram 发送前门禁。
- Grok Bridge 并发已满时返回明确 `bridge_capacity_exhausted`，不得无界排队。

## 8. 测试与发布验收

自动化测试至少覆盖：

1. 安全成年身材 / 轻度暧昧短句保留，交易和年龄风险删除。
2. M3 成功时不调用后续层。
3. M3 的异常、空回复、拒答、JSON 和质量失败分别进入 M2.5。
4. M2.5 同类失败进入 Grok；Grok 失败进入静态兜底。
5. 禁止输入不会通过回退链强行生成。
6. M2.5 `<think>` 内容不会抢先被 JSON 提取器误认。
7. 静态兜底可关闭，关闭时全链失败跳过本轮。
8. 每层来源、原因和耗时写入诊断，且不泄露密钥 / 登录态。

发布走 `master -> release -> Deploy Production`。生产验收必须确认两个 MiniMax Provider 均健康、租户默认指向 M3、六个运行任务无旧模型覆盖、Grok Bridge 健康，并分别获得 M3 成功、M2.5 回退、Grok 回退和静态兜底的受控 dry-run 证据。只有实际发送链继续通过账号、规则、容量和 Telegram 门禁后，才能判断生产任务恢复；Provider 健康或 dry-run 成功不能单独写成 `production_fixed`。

## 9. 回滚

回滚顺序为关闭静态兜底、关闭 Grok 层、把租户默认切回原 M2.5 Provider，再回滚应用版本。独立 Provider 记录和 Bridge 开关使回滚不需要改写密钥。回滚后保留 attempt 审计，不删除失败事实。
