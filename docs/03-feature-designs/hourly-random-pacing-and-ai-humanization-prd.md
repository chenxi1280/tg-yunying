# 四类互动确定性随机节奏与 AI 内容自然度治理 PRD

## 0. 文档状态

| 项 | 结论 |
| --- | --- |
| Intake ID | `intake-2026-08-15-pacing-quality-001` |
| 问题级别 | L2 / P1，生产相关，必须走 Release Gate |
| 文档版本 | v8 capacity-and-quality resync |
| 设计状态 | `product_design_complete / implementation_resynced_2026-08-19` |
| 实现状态 | `local_first_phase_complete / targeted_qa_pass`；stable due/source admission、跨 Task capacity 确定性抖动/late-tail、curve-aware pairwise gap/headroom、AI 批量绑定、逐 owner typed shortfall 已实现；PostgreSQL 压力、shadow/canary 与 E4 待证明 |
| 适用范围 | `group_ai_chat`、`channel_view`、`channel_comment`、`channel_like` |
| 证据状态 | 仅有本地代码、迁移、定向测试与前端构建证据；任务级 Action/Attempt/Telegram E4 保持 `unproven` |
| 真相源边界 | 本文记录本次局部实现；不把本地 QA、部署健康或 Action success 当成生产 typed remote fact，也不覆盖更大范围的 AI current-owner 收敛合同 |

AI 内容专项补正以 `ai-content-routing-and-quality-upgrade-prd.md` v1.2 及其运行、评测附录为准；本文继续负责数量 owner、跨 Task 来源容量、节奏和 Telegram E4。专项当前仅设计完成，未实现或发布。

本版取代 v5 讨论稿，记录已落地实现、验收边界和仍需发布阶段证明的事项：

1. 不把四类集中执行归成同一个已实锤根因。
2. 不采用可能自然成团的“纯独立均匀随机”。
3. 不采用“放不下也必须压缩完成”的突发追量。
4. 不允许二次改写失败后把低质 Stage 1 草稿继续发出。
5. 不预先拍板 `temperature=0.9` 或 `presence_penalty`；参数必须由同一评测集选出。
6. 不把语义判断伪装成纯规则的“确定性质量闸”。
7. 不使用仅按 Task/批次 seed 的独立均匀随机、固定 45 秒或 deadline 前压缩完成。

---

## 1. 原始需求与成功定义

### 1.1 用户问题

1. 任务规划后在一分钟或短时间内集中执行，没有在完整小时/任务周期内自然分布。
2. 不同账号在同一时刻成批执行，AI 活群、浏览、评论、点赞均有该现象。
3. AI 活群和频道 AI 评论有明显模板感、AI 腔和语境错位。
4. 三张对标图中的评论在时间、长度、句型、意图和账号口吻上更自然。
5. 本轮先定方案和 PRD，讨论确认后再开发，不允许直接改生产。

### 1.2 产品成功定义

- 正常运行时，每个任务周期的义务先获得稳定业务 `due_at`；实际领取另由可审计 `effective_claim_at` 控制，Planner 重跑、worker 重启和 retry 均不改写原 `due_at`。
- 同一小时的计划覆盖完整可执行窗口，间隔不等距，但密度有界；同一账号不会同秒或连续成团执行。
- 服务停机、账号不足、Telegram 限制或内容质量不足时如实形成 late/shortfall，不把欠额压进几分钟内追完。
- AI 正文必须同时满足语境锚点、事实安全、账号声线、意图差异和跨批去重；不再只以“模型返回了非空文本”为通过。
- 生产验收以各任务类型的 Telegram typed remote fact 为终点；Action success、容器 healthy 或页面 completed 均不能替代。

### 1.3 非目标

- 不设计规避 Telegram 规则、审核或检测的策略。
- 不恢复退役的 AI 硬小时目标、群冷却、中央任务份额或容量预扣；账号级软节奏原子门禁不属于任务配额。
- 不用提高随机度掩盖错误上下文、错误引用、账号面具缺失或 Provider 故障。
- 不复制对标图中的成人暗示、营销、冒犯或低信息量文本；只提炼时间与语言结构。

---

## 2. 当前证据与根因分层

### 2.1 证据矩阵

| unit | service | schedule/ledger | Action | Attempt/Gateway | remote fact | first blocker | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 生产发布 | SHA `621e1a00` 容器健康 | 未运行本次专项诊断 | 未核对 | 未核对 | 未核对 | 发布步骤跳过任务 E4/质量诊断 | `pass(E3) / E4 unproven` |
| AI 活群 | deployed/committed 代码已定位 | current 批次调用 `schedule_due_times` | 同批 `scheduled_at=earliest` | 未核对 | 未核对 | DueSet 到期后被整体置为同刻 | `failed(code contract) / production E4 unproven` |
| 频道浏览 | deployed/committed 代码已定位 | current 批次调用 `schedule_due_times` | 同批 `scheduled_at=earliest` | 未核对 | 未核对 | 与 AI 相同的同刻 helper | `failed(code contract) / production E4 unproven` |
| 频道评论 | committed 代码已定位 | 首次计划走 `schedule_times` | replacement `attempt_no>1` 可 future→now | 未核对 | 未核对 | 首次集中根因仍缺任务配置；replacement 有局部提前 | `partial / production unproven` |
| 频道点赞 | committed 代码已定位 | 走 `source_rolling_pacing_due + schedule_times` | fixed immediate 配置可同刻 | 未核对 | 未核对 | 需核对生产 pacing_config 与既有 future backlog | `unproven` |
| AI 内容质量 | Prompt/面具/quality gate 已读 | 不适用 | 有静态/emoji fallback | 未核对 | 对标图仅为外部样例 | 现有规则未形成逐槽可验收风格合同 | `failed(design gap) / production sample unproven` |

### 2.2 调度根因

`schedule_due_times()` 的同刻行为由 2026-08-11 的到期积压止血引入。该止血当时解决的是：

- current DueSet 已判定“到期”后又套 6 小时模板，形成二次延期和跨日积压；
- open owner 误计导致同一欠额重复物化。

止血将 AI/浏览当前批次改为 `earliest-safe`，成功切断二次延期，但也把“节奏判断”退化为：

```text
累计 due_by_now = N
  -> 一次物化 N 条
  -> N 条 scheduled_at 相同
  -> Dispatcher 在资源可用时快速排空
```

所以长期修复不能简单恢复旧模板，也不能在 DueSet 之后再随手随机一次。节奏必须前移到每个稳定业务义务的 `due_at`，成为 DueSet 的组成部分。

评论和点赞不直接走该 helper，必须独立核对：

- 评论 replacement 的 `future -> now` 提前路径；
- 评论/点赞实际 `pacing_config` 是否为 fixed immediate；
- 是否已有大量 future Action、是否在任务/来源批次间重复续排；
- Dispatcher 是否只领取 `scheduled_at <= now`，以及实际 remote fact 间隔。

二次审查期间曾出现一版未提交本地草案，采用“Task 级 batch seed + 桶内独立均匀随机 + deadline 内完成量优先压缩 + 固定 45 秒”，并把评论 current deadline 改为本地自然日。它不是生产事实，也不是已接受实现；这些做法分别违反 stable slot、来源滚动 24h、不突发与无魔法间隔合同，只作为 §4.8 反例。

### 2.3 内容质量根因

当前实现并非“完全没有人性化提示词”。它已经包含短句、上下文锚点、模板黑名单、账号画像和重复过滤。真正缺口是：

1. `account_profile` 仍以自由文本进入安全清洗，风格字段可能被丢失；最终 Provider 的 persona 仍容易退化为“普通群友”。
2. 账号面具的发送前判断偏向关键词/长文本命中，无法验证句长、提问比例、标点、语气词、克制程度等声线特征。
3. Prompt 同时承担理解上下文、规划多人意图、写最终文案和自我去重，约束互相挤压。
4. 评论/群聊失败后仍可能使用 emoji、`签到` 或其他 fallback 补数量；低质内容与真实履约被混在一起。
5. 没有冻结评测集、版本化 rubric 和 baseline/candidate 成对盲评，无法知道改 Prompt 是变好还是换了一种 AI 腔。

### 2.4 2026-08-17 生产 resync

当前生产已不再表现为“整批几秒完成”，但 AI 单来源 release 仍可达 10～12/min、实际执行 11～15/min，明显高于计划密度。已证明的缺口是 overdue recovery cursor 只读取当前技术批次的 frozen releases；下一批为空时又从 `now - 0.8 × gap` 起步，导致多个批次在同一组分钟重叠。

本节不改变 stable slot、`due_at` 或数量 owner。修复必须在来源级事务锁内，从当前 task/lifecycle/period/source/plan 的全部已冻结 owner 读取最大 release/ordinal，作为下一批 cursor；首次无 owner 才允许使用 recovery 起点。排期分散还不等于调用分散：AI/浏览/评论/点赞在真实 Gateway 调用前都必须经过来源级 admission，未来时间只 defer 并释放 worker，DB/锁/身份失败时禁止远程调用。完整 owner 映射、最终闸门、在线迁移、shadow/canary 与 E4 合同见 [production-planner-pacing-and-memory-remediation-prd.md](production-planner-pacing-and-memory-remediation-prd.md) §4.5～§4.6、§8～§10。v6 本地通过不能再外推为生产节奏已闭合。

---

## 3. 已实现的产品默认（待最终产品验收）

| 决策 | 推荐默认 | 理由 |
| --- | --- | --- |
| “每小时 N 条”是什么 | AI/浏览以日目标、评论/点赞以来源滚动目标，经 system soft curve 派生只读小时计划数；不是小时债务 | 当前总合同已退役 AI 硬小时；system `natural_full_day` 也不开放 AI 小时权重手工编辑 |
| 达量与不爆发如何同时处理 | start/新 period 前聚合同来源总需求并预留 replacement headroom；运行时只用冻结余量，截止后 typed shortfall | 可预见容量不足应提前阻断，不能先接受任务再靠突发追量或必然欠量收尾 |
| 账号跨任务如何错峰 | 使用版本化账号软节奏策略和原子 guard；不先写死全局 45 秒 | 同账号跨任务需统一审计，但 Telegram/任务密度不同，单一魔法值不可证明合理 |
| 低质候选是否允许降级发送 | 默认不发送；进入 `quality_wait`，截止后 `quality_shortfall` | 发送 Stage 1/emoji/签到会用低质量伪装履约 |
| 质量重生成预算 | canary 默认初次生成后最多 1 次按 rejection code 定向重生成；策略版本化、可关闭，扩容前由评测复核 | 明确成本/延迟边界且不静默 fallback |
| 对标图如何使用 | 只抽取结构特征，建立人工批准的脱敏样例库 | 原图含不适合直接模仿的内容，也不能把他人消息原样喂给模型复制 |
| 模型参数如何确定 | 通过离线 A/B + 盲评选版本，不写死 temperature | 当前质量问题不是单参数问题，且 Provider 参数支持度不同 |

以上为 v8 设计默认；现有代码是否满足必须按实现、PostgreSQL、shadow/canary 与 Telegram E4 分层验收。

---

## 4. 方案 A：确定性分层随机节奏

### 4.1 单一权威对象

每条业务义务必须拥有不可变节奏快照：

```text
PacingSlot
  slot_key
  task_id / target_or_source_id / period_id / obligation_ordinal
  pacing_contract_version = deterministic_stratified_v1
  pacing_plan_hash / pacing_seed_id
  due_at
  generation_not_before_at (仅 AI/评论正文)
  release_not_before_at (正常为 due_at；恢复时可更晚)
  account_binding_id / assignment_revision
  state = future | due | claimed | remote_unknown | confirmed | missed
```

`PacingSlot` 是业务义务的逻辑时间属性，不新建平行数量 owner，也不是中央容量预扣或完成事实。完成仍只认对应 typed remote fact。领取时间统一为：

```text
effective_claim_at = max(due_at, release_not_before_at, account_policy_not_before_at)
Action.scheduled_at = effective_claim_at
```

`due_at` 永不因迟到、换账号或重试改写；两者差值就是可解释 lateness。

fact-first 浏览/点赞 Action 一旦已由远端事实投影并关闭账号节奏 reservation，旧 Action 不得再进入 legacy 原地重试。direct claim 只选择无账号节奏槽，或仍持有 `reserved|bound` reservation 的 Action；单条历史错态不得抛异常中止整个 Dispatcher 批次。义务仍由 open projection 生成 replacement Action，禁止复用已经结算的 Action/Gateway 身份。

类型映射：

| 类型 | period / anchor | 稳定义务 | `due_at` owner |
| --- | --- | --- | --- |
| AI 活群 | Task 自然日；`max(period_start, planning_anchor_at)` | 本次局部实现复用现有 `TaskGroupDailyMessageSlot(slot_ordinal)`；更大范围的 current-owner 收敛不在本次假定完成 | 群日数量 slot |
| 浏览 | Task 自然日；ledger target accrual anchor | peer-message due unit | 浏览 due unit；账号在 pre-Gateway 前可替换 |
| 评论 | 来源首次采集后的滚动 24h | `CommentFulfillmentObligation(target_ordinal)` | 评论 obligation |
| 点赞 | 来源首次采集后的滚动 24h | `ReactionFulfillmentObligation` | reaction obligation |

### 4.2 小时配额与随机点生成

1. 冻结本 period 的目标、任务时区、system soft curve、anchor、deadline、配置 revision 与一次生成后持久化的 seed ID/hash。
2. 使用最大余数法按“精确份额的小数余数”分配整数，保证小时计划数之和等于有效目标；partial start 不追补 anchor 之前的量。
3. 某小时分到 `q` 条时，将该小时可执行秒数切成 `q` 个连续小分层，每层只放一个点。
4. 每个点用 canonical JSON 的 `SHA-256(seed + slot_key + stratum)` 取确定性偏移；禁止语言内建 `hash()`、运行时随机 seed 或只有 Task ID 的 batch seed。
5. 因为每层最多一个点，结果既不等距，也不会像独立均匀采样那样偶然挤成一团。
6. Planner 重跑、worker 重启、并发 worker 和 pre-Gateway retry 必须得到相同 `due_at`；禁止每次调用 `random` 重新洗牌。

v8 中 curve 权重 0 表示该小时不分配业务 due 或 source release；低活跃必须显式使用正权重，禁止 0→1 静默改写。评论/点赞始终使用来源首次采集后的半开滚动 24h，不得偷换为本地自然日。

20 条/小时的验收示意不是固定模板，而是类似：

```text
22:01:18  22:04:47  22:07:09  22:10:52  22:13:31
22:16:44  22:19:06  22:23:41  22:25:12  22:28:55
22:31:20  22:34:49  22:37:04  22:41:33  22:43:16
22:46:58  22:49:11  22:52:45  22:55:06  22:58:37
```

### 4.3 账号错峰

- 先生成业务 slot，再在 JIT lookahead 内绑定账号；不能先给一个账号整批生成连续 Action。
- 同账号跨四类任务由原子 `AccountPacingGuard` 同时读取最近 typed remote fact、open/future Action、FloodWait/SlowMode 与版本化软间隔；不得只查当前批次，也不硬编码“45 秒”。
- 绑定后若冲突，优先换同任务合法账号；无合法绑定则保留义务并写 `account_timeline_conflict`，由 guard 给出下一次可判定时间。
- 账号只允许在 immutable content intent 建立前直接换绑；之后换绑必须 CAS 终结旧 job/candidate、递增 assignment/intent revision 并重新生成，禁止把 A 账号声线正文交给 B 账号。
- 任一账号不得出现两个相同 `scheduled_at`；账号 FloodWait/SlowMode、授权、代理、准入和 unknown 仍高于软节奏。
- 账号错峰只推进 `account_policy_not_before_at/effective_claim_at`，不改业务 `due_at`；详情必须同时显示 `due_at/effective_claim_at/executed_at`。
- claim 前若出现新冲突，只允许 CAS 将 `effective_claim_at` 向后推进并释放当前 claim；禁止向前抢跑或原 Action 换账号/换正文。

### 4.4 JIT 物化与领取

- future slot 只在进入 lookahead 后推进；lookahead 由冻结 telemetry/policy 计算，缺少 telemetry 和 approved canary policy 时 start/preflight 显式失败。
- AI/评论的 `GenerationJob.generation_not_before_at=max(period_anchor,due_at-generation_lead)` 是生成 claim 唯一时间权威；claim/candidate/Gateway 前重读 context revision/hash/age。只有 candidate 通过质量闸后才创建 sendable Action。
- 评论 reply target 只在 context-bound window 内临近选择；失效时同义务新 intent revision 重建并继续受 pacing release 约束，不把 replacement 批量改成 `now`。
- Dispatcher 只领取 `scheduled_at <= now`，且 claim 前重读 PacingGuard、assignment/intent/context/reply version。
- 禁止通用 `future -> now` rewrite，包括 AI/view 到期批次、评论 replacement 和 Recovery 唤醒。
- 同一 slot 由 obligation natural key + pacing plan version 做 anti-join；open/unknown/remote fact 均占用身份，不能重复物化。

### 4.5 迟到、停机与截止

| 场景 | 处理 |
| --- | --- |
| Planner/worker 恢复后已有 overdue | 冻结本次 `recovery_release_plan`，按原 period 最大正常密度为 overdue ordinal 生成 `release_not_before_at`；保留原 `due_at`，不得同秒排空 |
| pre-Gateway 明确未调用远端 | 原 obligation 继续，但仍受原 slot 和当前密度约束；不得整批改 `now` |
| Gateway started / unknown | 保留原身份，只读 reconcile，禁止重发 |
| start/新 period 可预见容量不足 | 聚合同 tenant/domain/真实 source 的重叠窗口总需求；保留既有 frozen release，新任务以 `source_capacity_unavailable` 阻断，不缩目标 |
| 运行期不可预见延迟 | 逐 owner 使用冻结 replacement headroom；不得取消同批其他合法 slot，放不下的截止后唯一 typed shortfall |
| period deadline 到达 | 未完成 slot 写 typed `pacing_capacity_shortfall`、`quality_shortfall` 或专项 blocker |
| curve 0 / 低活跃小时 | 0 权重不分配新 release；低活跃用显式正权重，不把多个点平移到下一小时同一秒 |
| 运行中编辑配置 | 当前 period 的 plan/hash 不变；新配置只生成下一 period snapshot |

### 4.6 数据与迁移

开发阶段需要 additive migration，不再坚持 v3 的“绝不改数据模型”：

- 在当前实际数量 owner 上增加 `pacing_contract_version/pacing_plan_hash/pacing_slot_ordinal/pacing_due_at/release_not_before_at`：本次 AI 适配落在 `TaskGroupDailyMessageSlot`，评论/reaction 映射现有 obligation，浏览映射 peer-message due ordinal。该局部适配不等于完成总合同中的 `AiGroupMessageObligation` current-owner 迁移。
- `MessageBrief` 映射 current immutable content intent；candidate、rejection 与 evaluator evidence 进入同 obligation 的 `GenerationJob/variation history`。评论接入通用 GenerationJob，不再把 Action 内缓存当唯一生成审计。
- 新增账号时间状态/claim 只服务跨任务原子错峰，至少持有 tenant/account/policy version、next eligible、绑定 obligation/Action、effective time、state/version；它不是 quantity owner、DispatchReservation 或完成事实。
- 新增 `SourcePacingCapacityPlan` 聚合同一真实 source 的跨 Task 占用和 headroom；owner 冻结 capacity plan hash/ordinal，Admission 使用相邻 capacity slots 的 pairwise gap，禁止单 Task 平均 gap。
- Action 冻结 `pacing_slot_key/pacing_due_at/effective_claim_at/assignment_revision/intent_revision/candidate_hash`，便于 Gateway 前 CAS 与 E4 关联。
- 已 success、Gateway-started、unknown 的历史 Action 永不改写。
- 未进入 Gateway 的存量 pending/future Action 先由受控 workflow 做 preview：固定 Task/Action 集合、旧值、目标 plan hash、deployed SHA 和 candidate hash；用户之后明确授权才可 apply/readback。
- 无法安全映射到新 slot 的存量项进入 typed manual review/shortfall，不批量改成 now。
- 正文只在现有受权限控制的业务存储按 retention 保留；日志、周报和 evaluator 记录只写 hash、特征、版本与脱敏证据引用，不新增长期明文副本。

### 4.7 前端与 API

- current AI 创建/编辑页删除“每小时轮数”和手工小时曲线编辑，改为只读“系统派生小时计划”；其他类型如保留 soft curve，必须明确其是权重而非硬目标。
- AI 活群与频道评论高级设置提供 `ai_two_stage_enabled`、显式 `ai_model` 和独立 `ai_semantic_reviewer_model`；开启时保存前校验两个模型均必填且必须不同，避免自动选型后实际落到同一模型。
- AI 活群与频道评论高级设置同时提供内容路由 v2 的 policy version、allowed routes 与 attestation IDs；保存事务必须生成当前 task revision binding，并验证所有 Provider purpose route 已激活。成人 route 只能由 current evidence 逐条命中，不能因任务仅允许一个成人 route 就全局强转。
- 四类履约任务的节奏页提供 `source_capacity_v2_enabled` 与 `source_capacity_policy_version_id`；启用时保存前必须验证同 tenant、同 pacing domain 的 active policy，Planner 后续只能读取被冻结的显式 policy ID，不再按“当前 active”漂移。
- 创建预览显示：总目标、跨 Task source required/occupied/headroom/available/deficit、configured/effective curve、随机跨度、账号可绑定数和 last safe release；preview/start hash 漂移必须失败。
- 任务详情新增 `pacing_summary`：future/due/late/remote_unknown/confirmed/missed、最早/最晚计划点、同秒碰撞数、5 分钟峰值、账号最小实际间隔；`confirmed` 只认 Action 对应 typed remote fact。
- 单条执行详情同时显示 `due_at/scheduled_at/claimed_at/executed_at` 与 typed blocker；不得只显示 completed。
- API 的 plan preview 与正式 start 必须绑定相同配置 revision/hash，防止预览后配置漂移。

### 4.8 明确禁止的实现方式

- 对同批 Action 使用 Task/batch seed 后独立均匀采样；必须按 stable slot/stratum 生成。
- 为“完成量优先”把放不下的量压进 deadline 前、静默结束点或恢复后几分钟。
- 只在当前函数参数内检查账号间隔，或写死全局 45 秒而不查跨任务历史与 future owner。
- 把评论/点赞来源滚动 24h 改为任务本地自然日，或让 replacement 绕过原 pacing identity。
- 生成后过滤超 deadline 时间点却不创建 typed shortfall；任何数量变化都必须守恒可见。
- 不得把 curve 0 静默改为 1，或把该小时的点重采样到下一小时/quiet end；低活跃只能使用显式正权重。
- 账号间隔放不进 deadline 时不得保留原过近时间；必须形成 late/typed shortfall。调用链必须真实传递账号 identity，不能只测未被调用的 helper。
- 测试只证明“不是同一时间/不是等距/总数没少”不够；若未覆盖 stable slot、密度上界、跨批重放和来源 rolling deadline，不得作为验收。

---

## 5. 方案 B：AI 内容“意图计划 → 单槽表达 → 质量闸”

### 5.1 从对标图提炼的可用规律

- 时间非等距：相邻可差 1、2、5、7、11、29 分钟，而不是固定 60 秒。
- 长度混合：极短反馈、半句、具体小问题、轻情绪表达并存。
- 每条只抓一个细节并完成一个意图，不总结整篇内容。
- 有直接评论，也有 Telegram 原生引用回复；引用关系由远端 ID 证明，不靠正文伪装。
- 不同账号的克制程度、疑问习惯、标点和口头词不同。

### 5.2 版本化 MessageBrief

每个待生成 slot 先冻结结构化 `MessageBrief`：

```json
{
  "slot_id": "stable-id",
  "assignment_revision": 3,
  "context_snapshot_hash": "sha256",
  "context_anchor_ids": ["source-1"],
  "allowed_facts": ["已脱敏事实锚点"],
  "speech_act": "question|agreement|reaction|follow_up|light_humor",
  "stance": "positive|neutral|skeptical|curious",
  "length_band": "micro|short|medium",
  "punctuation_profile": "none|question|pause",
  "voice_profile_version": "style_contract_v3",
  "forbidden_claims": ["experience", "location", "transaction"],
  "reply_to_message_id": null
}
```

上例是 legacy/general 基线，不得解释为 v2 对所有 route 永久禁止 location/transaction 语义。MessageBrief v2 的 `forbidden_claims` 由 route-aware grounding 确定：`adult_service_inquiry` 可在 matching evidence 支持时生成价格、区域、空闲、服务、时长、本人、预约类**问题**，但不得生成对应断言、精确金额/地址/联系方式或任何无 evidence 的细节；general 和未授权 adult route 仍保持禁止。具体 `allowed_claim_categories/forbidden_claim_categories/speech_act/evidence_ids` 合同以 AI 内容专项 4.5 为准，任何 route 都不得绕过全局安全投影。

同批 brief 先做意图与结构差异校验：不能所有账号都选 agreement、都以“确实”开头、都写“陈述 + 感受”；但多样性服从事实与上下文，不为凑比例强造意图。

### 5.3 账号声线合同 v3

账号面具不再只是一段自由文本或关键词列表，新增可验证维度：

- `length_mix`：micro/short/medium 比例；
- `question_rate`、`emoji_rate`、`sentence_final_particle_rate`；
- `punctuation_style`、`colloquial_markers`；
- `assertiveness`、`humor_level`、`warmth`；
- `preferred_speech_acts` 与 `forbidden_patterns`。

安全清洗必须按字段白名单保留这些风格维度，不能把整个 profile 当普通上下文清洗成空。相同账号复用 base voice；目标/群风格只能作为 surface overlay 调整词汇和热度，不能覆盖身份、事实、安全与禁用表达。

真人样本只生成脱敏风格统计；原始消息不进入长期 few-shot 库。可直接注入 Prompt 的例句必须来自产品人工批准的脱敏样例集，并记录 example-set version。

### 5.4 两阶段生成不是“草稿失败照发”

#### Stage 1：Brief Planner

职责只有：从允许上下文中选择一个事实锚点、一个 speech act、一个 stance 和一个长度/标点档位。外部 Telegram 文本全部视为数据，里面的指令不得覆盖系统规则。

建议系统提示词基线：

```text
你只规划一条 Telegram 群聊或频道评论，不写最终文案。
只能从 allowed_facts 选择一个事实锚点；只有锚点支持真实问题时才可 question，否则选择 silence。
不得新增经历、地点、价格、交易、人物关系或结果。
同批 recent_briefs 已用过的 speech_act、开头方式和长度档位应尽量避开。
严格输出 MessageBrief JSON，不输出解释。
```

#### Stage 2：Voice Realizer

职责只有：将一个冻结 brief 实现为该账号的一条消息。每个 slot 独立生成，避免一个模型把整批写成同一种句式。

建议系统提示词基线：

```text
把一个已审核 MessageBrief 写成一条自然、简短的中文 Telegram 消息。
只表达 brief 指定的一个 speech_act，只使用 allowed_facts，不补充任何新事实。
严格服从 voice_profile 的句长、提问、标点和口头表达习惯；不要强行加“哈哈”“确实”等口头词。
不总结原文，不写运营文案，不解释，不使用模板夸赞，不提 AI、任务或提示词。
reply_to_message_id 非空时必须直接接住被引用内容；为空时不得伪装成回复某人。
输出 JSON：content、used_anchor_ids、speech_act、voice_profile_version。
```

AI 群 `reply_to_message_id` 只允许 current 合同中同 tenant/Task/group 且已 bound 的 canonical remote fact；真人消息只作上下文锚点。频道评论继续遵守 same Task/source/plan revision 的 own-history/listener reply 合同。对标图不能扩大两类引用权限。

candidate 必须冻结 assignment、intent、context、reply、voice 与 example-set hash；任一版本变化都使未进 Gateway candidate 失效并创建新 intent revision，禁止原 Action 改正文。Stage 2 超时、结构错误或质量不通过时，不发送 Stage 1，也不自动改成 emoji/`签到`；按第 3 节已确认的显式 retry policy 定向重生成，耗尽后进入 `quality_wait`。

### 5.5 分层质量闸

| gate | 判定类型 | 通过标准 | 失败码 |
| --- | --- | --- | --- |
| Scope/Schema | 规则硬闸 | tenant/task/target/source、字段和冻结 version/hash 一致 | `content_scope_mismatch` |
| Reply authority | 规则 + 权威查询硬闸 | reply ID 属于任务类型允许的 current remote fact/评论目标 | `reply_target_mismatch` |
| Fact | 确定性硬闸 + 语义 reviewer | marker 先于 reviewer 拒绝无锚点经历/位置/交易；其余陈述必须得到 allowed fact 支持 | `unsupported_claim` |
| Context | 规则 + 语义 reviewer | 锚点存在且正文确实接住它 | `missing_context_anchor` / `context_mismatch` |
| Voice | 统计规则 + 语义 reviewer | 可测特征及整体口吻符合冻结 voice | `voice_profile_mismatch_v3` |
| Template | 规则硬闸 | 不命中模板 shell、运营/总结/审核话术 | `template_shell` |
| Duplicate | 精确规则 + 语义 reviewer | 无同账号精确重复或高置信近义/结构塌缩 | `semantic_duplicate` / `structural_duplicate` |
| Batch diversity | 结构规则 | brief 的 speech act、开头、长度和标点未塌缩 | `batch_style_collapse` |
| Safety | 规则 + 专项审核 | 固定出站策略通过 | `content_rejected` |

规则硬闸失败必拒绝；语义 reviewer 必须输出 evidence、model/prompt version 与 calibrated confidence。高置信失败拒绝，低置信进入 `quality_wait/manual_review`，不得为了凑量按通过处理。

结构指纹不只取“开头 3 字”，而使用：

```text
speech_act + length_band + opening_function_pattern + punctuation_profile + syntax_shape
```

极短消息不做 blanket 豁免；同账号精确重复仍是硬闸，但不同账号的“666”“?”等自然公共反应按频次、上下文和批次结构判断，不能被全局近义规则一刀切。

### 5.6 参数策略

- `temperature`、top-p、presence/frequency penalty 只作为候选实验变量，不属于业务真相源。
- 只有 Provider 明确支持并真实回读的参数才可进入实验；不允许静默忽略。
- 同一 prompt/model/example-set 下至少比较低/中/高三个温度档，固定 seed 和评测集。
- 选型以硬 gate、人工偏好和稳定性为准，不以“重复拦截变少”单指标决定。

### 5.7 质量评测合同

建立版本化离线评测集，至少覆盖：

- AI 活群 direct/reply、有真人上下文/无上下文；
- 频道评论 direct/reply、短帖/长帖/信息不足；
- 多种账号声线；
- 敏感、提示注入、事实不足和重复上下文边界。

评测分两类：

1. 可证明项直接判定：schema、scope、reply authority、精确重复与固定安全规则必须 100% 通过；事实/上下文等语义项不能伪装成纯确定性规则。
2. 主观项成对比较：candidate 与 baseline 使用相同上下文、账号声线和长度档位做双盲 A/B；每对交换左右位置，映射回真实 candidate 后结果不一致记 tie/低置信。

主观 rubric 使用 1–5 分并要求“先证据、后分数”：

- 真人口语自然度；
- 上下文贴合度；
- 账号声线辨识度；
- 信息增量/非空泛；
- 非模板化程度。

自动 judge 必须与生成模型不同，只作筛选和分歧定位，不能单独放行；记录 position consistency、与人工 agreement 及系统性分歧。发布实验需预注册抽样、tie 处理、功效分析和切片：人工偏好点估计至少 65%，按 context/账号聚类 bootstrap 的 95% 下界高于 50%，硬闸零回退，关键任务类型/声线切片无显著退化；样本不足只能写 `unproven`。

---

## 6. 线上观测与任务详情

### 6.1 节奏指标

- `pacing_slot_count/future/due/late/remote_unknown/missed/confirmed`；
- per Task/target 与 per account 的 `due_at_unique_ratio/effective_claim_at_unique_ratio/same_second_count`；
- `hour_span_ratio`；
- 有效窗口为 `W` 秒时的 5 分钟滑窗峰值，上界取 `min(q, ceil(q * 300 / W) + 1)`；
- per-account `executed_gap_p05/p50/p95`；
- `due_to_claim_lag`、`due_to_remote_fact_lag`；
- `recovery_release_plan_count`、`assignment_intent_invalidation_count`；
- `source_capacity_required/occupied/headroom/available/deficit`、capacity plan hash、pairwise gap 与 headroom 使用率；
- `future_to_now_rewrite_count` 必须为 0。

### 6.2 内容指标

- prompt/brief/voice/example-set/model/provider 版本；
- 各 quality rejection code 数量与重生成结果；
- `quality_wait`、`quality_shortfall`；
- speech act、长度、标点、开头和结构指纹分布；
- 人工盲评版本、有效/tie 样本数、偏好胜率、95% CI、切片结果、position consistency 和低置信率。

生产日志/周报不输出正文、Prompt、账号敏感信息或完整上下文；只保存必要 hash、版本、特征和受权限控制的脱敏样本引用。

---

## 7. QA、灰度、发布与回滚

### 7.1 Train A：节奏

1. 纯函数属性测试：目标守恒、分层唯一、curve 0、canonical seed、时区/DST、partial start、rolling deadline，以及 due/source capacity 小时直方图一致。
2. 四类 planner 测试：相同义务重复规划不新增，future→now 为 0；跨任务账号 guard 原子错峰，换绑后旧 intent/candidate 不可发送。
3. 同来源多 Task 总需求必须完整分配或 start 前返回精确 deficit；故障注入验证 headroom 逐 slot 替代、同批合法 slot 继续、恢复不在一分钟排空、unknown 不重发。
4. shadow 只生成 PacingPlan，不创建 Action；与旧计划比较 24h 直方图和 shortfall。
5. 每类各 1 个真实 canary，至少覆盖一个完整自然日/来源滚动 24h。
6. E4 分别核对 send remote message fact、ViewRemoteFact、Comment remote fact、ReactionRemoteFact。

20 条/完整小时的定向验收：`due_at` 覆盖至少 90% 窗口、同一 Task/target 同秒重复为 0、任意 5 分钟不超过 3 条、同账号无冲突；生产还要按 typed remote fact 验证实际执行未聚集。`q<4` 不套 90% span，而只验分层、唯一与 deadline。存在 blocker 时展示真实 late/shortfall，不压缩追量。

### 7.2 Train B：内容

1. 固定评测集先跑 current baseline，冻结结果。
2. 新 brief/prompt/voice gate 做分层规则回归、语义 reviewer 校准和 position-swap 盲评；发布前冻结功效分析与 95% CI 算法。
3. shadow 生成但不发送，核对 scope、事实、风格、重复和 Provider 成本/延迟。
4. 1 个 AI 群 + 1 个频道评论任务灰度 3 天；每天按 rubric 盲抽，不用运营主观挑好样本。
5. 质量失败逐 quantity owner 进入 `quality_wait`，deadline 后写唯一 typed shortfall fact；current v2 的 Stage 1/emoji/固定“签到” typed sent fact 必须为 0。
6. 通过后按任务级 feature flag 分批扩容，不热改全租户 Prompt。

### 7.3 回滚

- 节奏 plan/version 一旦为当前 period 创建，不允许回滚为 earliest-safe 或改写既有 `due_at`；异常时停止新物化并前向修复。
- Prompt/voice/example-set 以不可变版本回滚到上一个已通过评测的版本；已生成未发送正文必须重新过当前 scope/quality gate。
- 任何 Gateway-started/unknown 都只 reconcile，不随回滚重发。
- migration 为 additive，旧读路径只在未接管 Task 上保留；canary 接管后不得旧/new writer 双写。

---

## 8. 本地实现交接与验收状态

### 8.1 本地整改完成状态（2026-08-16 复审）

本地实现复审发现并已修复以下合同缺口：

1. 已绑定评论不得由通用容量逻辑或 dispatch-time speaker rotation 原地换号；任何合法换号都必须停止当前 dispatch，递增 assignment/intent revision，失效旧候选与正文，并重新生成和重新占用账号节奏。
2. overdue slot 不得直接 `max(due_at, now)`；四类任务必须共享恢复排期逻辑，在剩余窗口内按不高于原正常密度分层释放，并把首次生成的 `release_not_before_at` 冻结到物理 owner。
3. direct claim 必须在同一事务内锁账号并重新校验账号时间线；新增冲突只能原子前移 Action 与 reservation，不能带着旧有效时间 claim。
4. `unsupported_claim_marker` 是 reviewer 之前的确定性硬失败；reviewer 返回 `pass` 时 `codes` 必须为空。
5. planner/reviewer 独立性按网关规范化后的模型身份判断，不能被大小写、空格或别名绕过。
6. pacing summary 的 `confirmed` 必须有 Action 对应的 typed remote fact；只有本地 attempt/action success 的记录显示为 `remote_unknown`。

上述修复与既有 CPU/内存保护的合并回归已通过 529 条去重后的定向 no-PostgreSQL 用例、Python 编译检查和前端生产 build。账号时间线改为 tenant-scoped 分页 `UNION ALL`，AI slot 对齐保持线性复杂度；pacing summary 的 Action→typed remote fact 回查补充专用部分索引，避免详情查询形成逐 Action 全表扫描。由于本机 PostgreSQL 测试库认证失败，真实 PostgreSQL 行锁/并发建索引集成仍为 `unproven`，不能进入 Release Gate。

2026-08-19 二次回归修复补充：source capacity 小时槽改为 source/policy/hour 可重放的分层确定性抖动，并在不改变小时 quota 的前提下把候选贴合 owner 的合法 late-tail 区间；同 logical scope 的不同计划以不可变 revision 保存，后续 Task 复用上一 revision 的冻结槽和 aggregate headroom，避免 `scope_conflict` 或重复占用同一时段。AI group 批次只加载一次 GenerationJob、policy binding 和 purpose route snapshot，structured Provider 调用前把 temperature/max tokens 复制为标量，关闭事务后不再因 ORM setting 过期加载重开长事务。Listener 只有在目标群存在 `pending/running/paused` 且显式启用 v2 的 AI 活群 Task 时才维护 context revision，相关 upsert 使用单条 `RETURNING`。186 条定向 no-PostgreSQL 用例通过；不同起止边界的 overlap 聚合、PostgreSQL 并发、shadow/canary 与 Telegram E4 仍为 `unproven`。

2026-08-19 生产发布后发现存量逾期 Action 仍存在独立缺口：已绑定 Action 不会重新进入 planner recovery，且其来源状态在首次准入或已追平旧 cursor 时会直接按过期 `release_not_before_at` 放行，多个来源因此可在同一 claim 周期同步发送。当前实现增加 `source_pacing_recovery.late_admission_not_before`：仅对超过正常调度容差、尚未进入本次 Gateway 的新 admission 冻结由 Action 身份确定的恢复点，范围为原来源 gap 的 `0.8..1.8` 倍；正常秒级 lag 和显式远端失败 retry 不改写。该修复已通过定向本地回归，生产跨 Task 错峰和 typed remote fact E4 仍须重新发布后验证，不能据此提前写 `production_fixed`。

同日生产还发现日目标变化留下多组 `pacing_plan_total` 时，Planner 会持续报 `pacing_owner_immutable_conflict`，AI 批次还可能因不同 plan hash 各自从 ordinal 0 分配而产生 `pacing_slot_ordinal_duplicate`。已冻结 owner 中已有 Gateway-started/typed remote fact，禁止通过重排或升级其 total/hash/due 来消除冲突。修订后必须保留每个已冻结 owner 的原 `plan_total/plan_hash/ordinal/due/release`；调度器可在同一 source 批次读取多代冻结计划，但只对未冻结 owner 按当前计划计算 due。新 owner 的 cursor 必须跨同 lifecycle/period/source 的全部历史 plan hash 读取最大 ordinal/release 后继续，不能因目标上调或 hash 变化归零。当前 plan 容量不足时形成 typed shortfall，缺少旧计划证明或其他配置漂移仍明确阻断。

### 8.2 已实现但待本轮复核的范围

- pacing：稳定 slot/due、四类 planner 接入、持久账号时间线、移除本次链路的 future→now、详情投影；capacity 候选拆到 `source_capacity_slots.py`，`source_capacity_plans.py` 保持 500 行以内；
- quality：MessageBrief、voice contract v3、两阶段生成、独立语义 reviewer、分层 gates、evaluator evidence、position-swap 与聚类 bootstrap 评测工具；
- migration：`0150_pacing_slot_fields.py` 依赖 `0149_batch_login_principal`，追加字段与 `account_pacing_reservations`，本地真实 Alembic upgrade/downgrade 回归通过；
- UI/API：任务详情显示原始节奏时间/有效执行时间，创建编辑页可配置两阶段生成和独立 reviewer；
- docs：专项 README、数据流转索引和项目结构索引同步本地实现事实。

### 8.3 未完成与禁止外推

- 当前 schema two-stage 默认关闭且 legacy static fallback 默认开启；v8 要求 current v2 忽略这些 legacy fallback flags，迁移/readback、真实评测、canary 与任务级 Telegram E4 均未完成。
- 本次沿用现有 `TaskGroupDailyMessageSlot` 作为 AI 节奏物理 owner；未宣称完成总合同中更大范围的 `AiGroupMessageObligation -> GenerationJob -> Action` current-owner 迁移。
- 未执行生产存量 pending/future Action 的 preview/apply/readback，也未修改生产配置或数据。

### 8.4 Product Design Complete 自检

| 检查项 | 状态 |
| --- | --- |
| 用户原话与四类范围 | 已覆盖 |
| 现状、根因与线上证据强度 | 已区分；任务级 E4 `blocked/unproven` |
| 前端/API/worker | 已覆盖 |
| 数据、迁移、跨 Task source capacity、并发、幂等 | 已覆盖 |
| 权限、安全、Prompt injection、敏感数据 | 已覆盖 |
| 失败、unknown、deadline、回滚 | 已覆盖 |
| QA、盲评、灰度、E4 | 已覆盖 |
| 待产品决策 | 默认合同已落地；任务 flag 启用、shadow/canary 和生产扩容待产品验收 |

当前结论：`product_design_complete_v8 / local_first_phase_complete / targeted_qa_pass / production_fixed=unproven`。本地 pacing QA 不能证明生产跨 Task 总容量、curve-aware Gateway gap、headroom、逐 slot AI 守恒或 current v2 无静态补量；在 PostgreSQL 压力、shadow/canary、数量守恒和 typed remote fact E4 前不得声明生产修复。
