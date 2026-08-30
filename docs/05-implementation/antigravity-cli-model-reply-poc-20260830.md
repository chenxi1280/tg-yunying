# Antigravity CLI 3.5/3.6/3.7 Flash 业务回复 POC

> 日期：2026-08-30
>
> 状态：`feasibility_poc_complete` / `telegram_not_sent` / `production_route_not_integrated`
>
> 结论边界：本报告只比较一个固定活群样本和一个固定频道评论样本；不是生产 Provider、真实 Telegram 效果或 Release Gate 证据。

## 1. 测试目标

验证当前已登录的独立 Antigravity CLI profile 能否让以下三个模型按 `tg-yunying` 当前 AI 活群与频道评论质量要求返回结构化候选，并比较样本内质量、时延、token 和终态稳定性：

- `gemini-3.5-flash-medium`；
- `gemini-3.6-flash-medium`；
- `gemini-3.7-flash-medium`。

模型 slug 来自本场 `agy models` 的 authenticated readback。三组调用固定 Medium 档、相同 profile、相同 task prompt、相同 JSON Schema；没有因模型输出修改 prompt 后重测。

## 2. 业务要求与样本

### 2.1 AI 活群

固定安全上下文：

1. `频道导航今天加了数码交流板块`
2. `有人最近在研究机械键盘吗`

硬要求：承接已出现事实；8–24 个中文字符优先；像真实群友短问/轻评论；不得编造经历、地点、身份或结果；不得包含联系方式、链接、AI/任务/提示词；禁止空泛模板。

### 2.2 频道评论

固定频道原文：

`频道导航今天更新，新增科技、数码和交流板块，旧入口保持不变。`

硬要求：抓住原文具体事实；6–22 个中文字符优先；像真实读者随手评论；允许问一个具体小问题；不得编造亲身经历、位置、人物、效果或其他事实；不得包含联系方式、链接、AI/任务/提示词；禁止空泛支持/感谢模板。

### 2.3 输出合同

每个 task 单独调用，顶层只接受 `task_type/reply/anchor_fact`，`additionalProperties=false`。评估只读 CLI 顶层 `structured_output`，不把自由文本 `response` 当作业务结果。

## 3. 原始候选

| 模型 | AI 活群候选 | 频道评论候选 |
| --- | --- | --- |
| Gemini 3.5 Flash Medium | `打算入量产的还是客制化？` | `旧入口没变挺好，省得重新找了。` |
| Gemini 3.6 Flash Medium | `你是想看量产键盘还是客制化那种？` | `交流板块是在主频道还是讨论组？` |
| Gemini 3.7 Flash Medium | `想看量产还是客制化？` | `旧入口还在就行，习惯原来位置了` |

三个模型的六次调用均形成终态候选；候选形成不等于当前生产 cleaner 接受。没有 Telegram 发送、Action、ExecutionAttempt 或 remote fact。

## 4. 确定性硬检查

| 模型 | 活群 schema/长度/事实/安全 | 评论 schema/长度/事实/安全 | 判定 |
| --- | --- | --- | --- |
| 3.5 Flash Medium | 全部通过 | 全部通过；`省得重新找`是轻微推断，但没有断言外部事实 | 两场景可发送 |
| 3.6 Flash Medium | 全部通过 | 全部通过；以问句澄清交流板块承载形式，没有把选项断言成事实 | 两场景可发送 |
| 3.7 Flash Medium | 单独检查通过 | 当前 `clean_channel_comment_contents(..., restrict_sensitive_trade=true)` 因`位置`命中敏感交易/地点门而拒绝；同时引入原文未提供的个人习惯 | 评论 deterministic reject + `human_sendable=false` |

硬规则失败不能被其他维度高分覆盖。因此 3.7 虽然活群候选最好，仍不能成为本样本的跨场景质量第一。

把每条候选分别送入当前代码 cleaner 的读回为：活群 `[true,true,true]`，评论 `[true,true,false]`，模型顺序均为 3.5/3.6/3.7。把三个模型候选视作同一窗口时，活群和评论 cleaner 各保留前两条并去掉第 3 条近义候选，说明跨账号/窗口去重仍会进一步收窄可用内容。

## 5. 直接评分

采用现行评测合同的 1–5 锚点：`1=不可发送`、`3=可理解但有明显偏差`、`4=自然且可直接发送`、`5=高度贴合且无需改写`。先看候选证据，再对 natural/context/voice/route_fit/information_value 各评分；单条样本不评 window_quality。

### 5.1 AI 活群

| 模型 | natural | context | voice | route_fit | information_value | 合计 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3.5 | 4 | 5 | 4 | 5 | 5 | 23/25 |
| 3.6 | 4 | 5 | 4 | 5 | 5 | 23/25 |
| 3.7 | 5 | 5 | 5 | 5 | 5 | 25/25 |

3.7 的 `想看量产还是客制化？`最短、最像即时接话；3.5 的`入量产的`略生硬；3.6 信息完整但比群聊所需稍长。

### 5.2 频道评论

| 模型 | natural | context | voice | route_fit | information_value | 合计 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3.5 | 4 | 5 | 4 | 5 | 3 | 21/25 |
| 3.6 | 4 | 5 | 4 | 5 | 5 | 23/25 |
| 3.7 | 4 | 5 | 4 | 2 | 3 | 18/25，硬检查失败 |

3.6 抓住“新增交流板块”并提出具体可回答的问题，信息价值最高。3.5 自然但推进信息较少。3.7 的个人习惯没有事实来源，且`位置`已被当前静态门拒绝。

## 6. 换位 Pairwise

每一对先用匿名候选 A/B 比较，再交换左右位置复核；显式忽略长度、模型代际和位置。样本内结果：

- 活群：`3.7 > 3.6 ≈ 3.5`；3.7 对另外两者的换位结果一致，3.5/3.6 属低差异 tie。
- 评论：`3.6 > 3.5 > 3.7`；换位结果一致，3.7 因硬规则失败稳定落后。
- 跨场景：`3.6 > 3.5 > 3.7`。3.6 是唯一在两个场景都通过硬检查且主观评分均不低于 23/25 的模型。

该 pairwise 由不同于生成模型 family 的 Codex reviewer 执行，避免 generator self-preference；但只有两个样本，置信度只能记为 `medium_for_sample/low_for_production_selection`。

## 7. 时延、token 与运行事实

| 模型 | 活群 | 频道评论 | 运行观察 |
| --- | --- | --- | --- |
| 3.5 | provider 8.50s；17,274 tokens | 约 17.5s terminal；usage 未从宿主 stdout 读回 | 评论第一次 stream EOF，CLI 内部明确重试后成功；宿主等待先返回，后从 durable conversation 对账 |
| 3.6 | 约 7.57s terminal；usage 未从宿主 stdout 读回 | provider 8.64s；9,883 tokens（含 8,137 cache read） | 活群在宿主 30s yield 边界丢失 stdout，但 durable conversation 已明确完成，没有重放 |
| 3.7 | provider 4.91s；15,642 tokens | provider 4.58s；15,622 tokens | 两场景均直接返回，样本内最快且运行最干净 |

CLI 自带 agent harness，极短业务回复仍可产生约 1 万到 1.7 万 total tokens。`duration_seconds` 是 provider 报告时延，宿主 wall time 还包含启动、model/config/quota refresh，二者不能混用。

## 8. 样本内结论

1. 三个 Medium 模型都能为当前 AI 活群和频道评论要求生成结构化回复。
2. **跨场景质量优先：3.6 Flash Medium 最好。** 两个场景都通过硬检查，评论第一，活群与 3.5 接近。
3. **低延迟与活群即时感优先：3.7 Flash Medium 最好。** 两场景 provider 时延约 4.6–4.9s，活群候选第一；但本次评论命中当前地点/敏感交易 cleaner，并引入未提供的个人习惯，必须保留生成后质量 gate。
4. **3.5 Flash Medium 本样本没有质量优势。** 两场景可发送，但评论发生一次 stream EOF/内部重试，且总体时延更差。
5. 这是可行性 POC，不满足正式选型所需的多样本、每输入至少三次重复、评论独立评测集、人工校准和 Telegram 效果回读；不能据此声称生产质量已验证。
