# Findings & Decisions

> 2026-08-10：本文件是第一版设计证据；其Product Design Complete已被实现就绪复核推翻。current AI+channel_view结论只认`../2026-08-09-ai-group-failure-repair-readiness-gap/`与两份current专项。

## Requirements

- 为“西安天上人间”线上大量失败消息设计整体修复方案。
- 方案必须覆盖根因，而不是只清理失败记录、扩大并发、放宽质量或增加静默重试上限。
- 遵循 `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`，当前阶段完成 Product Design Complete 和开发交接。
- 不修改生产数据、不重启服务、不触发 Planner drain 或补发。

## Current Production Evidence

- 2026-08-09 21:44 +08：目标 4800，due 4432，confirmed 1027，coverage 889/891。
- 最近 60 分钟 52 success、140 failed；全部成功均进入 Gateway 并有远端消息事实，失败均未进入 Gateway。
- `direct_check_in_10d_duplicate` 100 次，集中于两个缺面具 coverage 账号；两账号累计失败 200/127 次。
- 两账号当天在其他 Task、其他群已有真实 check-in success；当前实现按 tenant+account+精确正文做跨 Task/群 10 天去重。
- 两个 coverage 失败后回到 `ready` 且 `next_eligible_at=NULL`，形成分钟级重新物化。
- 两账号最新面具生成项均在 4 次尝试后 `manual_required`，错误分别为 provider timeout/unavailable，无 active 面具。
- `duplicate_message` 最近 60 分钟 39 次、19 个账号；全部为 extra-volume、均调用 Provider、均无 `content_variation_key`。
- 单账号一小时可产生 10 个不同 generation ID，但全部命中同一 duplicate reference。
- 当前 open 仅一条未来 pending，Dispatcher active 0/26，无 claiming/executing/unknown；不是队列或 Gateway 拥堵。

## Contract Findings

- 当前主 PRD 与履约闭合合同要求 check-in 唯一范围为 `(task, group, account, task-day)`，且不进入普通正文 10 天去重。
- check-in 资格用尽且正常正文不可用时应投影 `content_capacity_gap`，不能降低目标或无限重建。
- `duplicate_message` 等质量拒绝必须创建新的 `content_variation_key`，并带入命中禁用语义和新角度。
- 当前代码 `_with_content_variation_key()` 对无 coverage 的 extra-volume 提前返回，造成新 Action 无 variation 身份。
- 当前 check-in 仍写 legacy `mask_missing_check_in`，与统一 `check_in` 合同存在实现漂移。
- 主 PRD 已精确要求 `ai_group_message_memory` 补齐 task-day/coverage/trigger/dedupe/duplicate 字段，并对 `content_source=check_in` 的 open/Gateway/unknown/confirmed 建 scoped partial unique；当前模型/迁移尚未兑现这些字段与索引。
- 当前 message memory 仅有通用 reservation 唯一和账号窗口查询；缺 `task_day_ledger_id`、`coverage_ledger_id`、`check_in_trigger_reason`、`gateway_started_at`、`dedupe_expires_at` 等合同字段，导致 check-in 只能用正文查询模拟业务资格。
- 当前任务明细的“已执行”条件是所有非 open Action，因而数百条 pre-Gateway 终态尝试与真实执行成功混在同一列表。产品修复必须新增按义务/原因聚合的运行视图，同时保留历史 Action 明细审计。
- 文档中仍散落“check-in 参与账号 10 天去重”或旧 `mask_missing_check_in` 规则，但 2026-08-04 supersede、主 PRD、分类恢复和闭合合同一致：新数据只允许统一 `check_in`，且明确排除普通 10 天查询。本次专项设计需再次声明优先级，避免开发继续引用历史段落。
- `ai-group-daily-group-target-redesign-prd.md` 已有稳定数量义务、check-in partial unique 与 `quality_waiting_context` 的产品要求，但其文档状态和旧“立即执行”文字与 2026-08-07 pacing 纠偏冲突。新的事故专项应只补闭环和实现交接，并明确由主 PRD 顶部最新 supersede 决定 pacing。
- 数据流索引 current 段仍明确记载 fact-first extra-volume “无 coverage 时使用 Action dedupe identity”，这正是失败风暴的结构缺口；应在顶部新增最新 DF 补正：coverage/extra-volume 都先绑定稳定 AI message obligation，再建 Action。
- 结构索引仍把 `direct_check_in.py` 的账号 10 天签到去重描述为实现事实。产品设计本轮可标为 known drift；实际模块/行数/测试入口应由 dev 完成代码后同步，避免设计阶段虚报已实现。
- 当前 `fact_first_v3` 的正式发送链为业务义务到账本/Action/Generation/Attempt；本次不能退回旧 ContentMix 数量槽，也不能把批次再缩成单条。
- 旧设计中存在“资源空闲即打满”的过时描述；当前主 PRD 与线上实现采用自然日 due pacing。本次明确保持 pacing，不借失败修复改变出量节奏。

## Existing Model Reuse Findings

- `FulfillmentObligationProjection` 已提供 `(obligation_type, obligation_id)` 唯一投影、CAS version 和 Action 非终态义务唯一索引，可承载 AI coverage/volume 的通用远端事实所有权。
- `GenerationJob` 已按 `(obligation_type, obligation_id, generation_sequence)` 建模；前提是所有 AI Action 都绑定稳定 obligation。extra-volume 当前缺少该身份，因此无法使用已有并行生成幂等合同。
- `AiCoverageVariationIntent` 唯一键限定在 coverage ledger；它不能直接覆盖无 coverage ledger 的 extra-volume。方案应把 variation 身份上提到通用 AI 义务，而不是再复制一套仅 Action 的字段。
- `fulfillment_remote_facts.py` 已能将带 obligation 的 Action 收口到通用投影；优先复用此路径，避免另建一套远端成功真相源。
- `Action` 已有数据库级 partial unique：同一 `(obligation_type, obligation_id)` 在 pending/claiming/executing/unknown 只能一条；`GenerationJob` 对 pending/generating/unknown 也有对应唯一门。
- 通用投影只有状态、active action、materialization/version 等最小字段，不适合塞入 AI 专属的账号绑定、variation、重复命中和唤醒依据。产品方案应采用“AI typed obligation + 通用投影”，而非污染通用表。
- coverage ledger 本身已按 `(tenant, task, group, account, coverage_date)` 唯一，可作为 scoped check-in 的业务根；但仍需对 check-in 资格/内存做数据库级保留态唯一，不能只依赖应用查询。
- 通用远端事实投影只处理 Gateway 后的 `safely_not_executed -> open`、`remote_outcome_unknown -> remote_reconcile_only`、成功 -> `confirmed`；`duplicate_message`/check-in 资格错误发生在 Gateway 前、没有远端事实，必须由 AI typed obligation 的显式 CAS 状态机接管。
- schema 已通过 migration 0137 引入通用投影/GenerationJob；本次应做 additive migration，并沿用现有 partial unique 与远端事实投影，不破坏旧 writer 的读取能力。
- 旧 `TaskGroupDailyMessageSlot/ContentMixCycleSlot` 仍有代码入口，但 current `fact_first_v3` 正式发送明确绕过 legacy ContentMix。extra-volume 的稳定身份应新增 current typed obligation，不应把旧数量槽重新启用。
- `TaskGroupDailyMessageSlot` 虽有 task-day+ordinal/coverage 唯一性，但与 legacy ContentMix 强绑定且已被 current fact-first 合同 supersede；直接复活会重新引入双账本与旧 settlement 语义。
- `TaskGroupDailyTarget` 已分别统计 confirmed、Gateway-started、unknown hold；新的 message obligation 只负责稳定占有一个待满足单位，目标完成仍必须由这些真实远端事实投影计算，不能以 obligation closed 或 capacity gap 计成功。
- 当前 daily fulfillment 详情基本只按 coverage row 汇总，无法表示 extra-volume 的 `quality_waiting_context/content_capacity_gap`；新读模型必须同时汇总 coverage 与 volume typed obligations。
- 当前 migration head 为 `0144_avatar_material_sources.py`；开发交接需从实际合并时 head 新建下一条 additive migration，不能在设计文档硬编码可能漂移的 revision/down_revision。
- `task_center.py` 817 行、`daily_coverage.py` 912 行、`group_ai_chat.py` 5525 行、TaskCenterDetailModal 1130 行，均超过项目硬限制。dev 不能继续把新状态机堆入这些文件，应按 obligation/content/wake/takeover/read-model 责任拆模块，并只在旧入口保留薄协调调用。

## Preliminary Design Principles

| Principle | Rationale |
|---|---|
| 义务身份与执行尝试分离 | 一个业务欠额可有多个终态尝试，但同一 variation 只能物化一次 |
| check-in 使用业务唯一键而非正文去重 | 固定正文无法用普通文本去重表达 Task/群/任务日资格 |
| 不可执行缺口留在账本，不制造 Action | `content_capacity_gap` 是容量事实，不是应立即重试的执行状态 |
| 质量拒绝保留且推动新 variation | 防止为提高吞吐放松去重和内容安全 |
| Gateway started/unknown 永不自动重放 | 保留远端副作用防重合同 |
| 历史失败保留审计，UI 按义务聚合 | 既不删证据，也不把终态历史误呈现为活跃积压 |

## Independent Review Findings

- 独立复核同意 canonical `AiGroupMessageObligation + FulfillmentObligationProjection/GenerationJob`，反对恢复 legacy quantity slot/ContentMix 真相源。
- 发布阻断项必须闭合：normalized event wake、immutable content intent、动态 coverage 加入/转换、存量 remote fact alias 映射、activation 后 rollback fence。
- `content_capacity_gap` 是现存 due unit 的 waiting blocker：不进 open 热索引、不占 Action/worker 槽，也不删除或创建 replacement；deadline 后同一 unit 转 shortfall。
- 新 coverage 动态加入需按“已有 scoped fact 直接 confirmed -> 未物化 extra CAS 转 coverage -> 无可转 unit 才新增 coverage 并递增 target revision”处理；Gateway/unknown/confirmed 永不改绑。
- 已批准的正文主/备用各 3 轮与面具 item 4 次后 manual_required 保持；本事故禁止新增通用 max retry 来掩盖状态机缺口。

### Second-gate blockers

- extra-volume 需要跨 Planner decision 稳定 `quantity_ordinal/due_unit_key`；decision id 只能审计，不能防并发重复建义务。
- Gateway 前准备与 remote mutation 已开始必须拆成 `gateway_prepared`、`gateway_started_hold`，并用 evidence journal 分类崩溃边界。
- wake 必须使用 durable monotonic clock 关闭 event-before-subscribe；未绑定 extra-volume 需要 task-day aggregate capacity revision。
- 单义务 content intent 之外还需 aggregate content allocation plan，守住跨 20 条技术批次的 reply/material/act-type 总合同。
- legacy memory 不得规范化改写；应新增 scoped claim/alias。alias 是多 legacy identities -> 一新 obligation，但每个 legacy fact/identity 最多一个新 owner。
- 状态机需补 `abandoned_for_day/contract_error`、Generation persist unknown 和精确 typed->FOP 映射；generation epoch 必须绑定明确 basis hash。
- E4 需直接证明 capacity gap 守恒、零 Action/Generation 热循环，以及 activation 后 legacy writer Gateway 增量为零。

## Scope Boundaries

- **In scope:** check-in 业务唯一性、容量缺口、coverage/extra-volume 质量重规划、稳定义务身份、并发幂等、画像资产门禁、API/UI 聚合、存量接管、发布/回滚/E4。
- **Out of scope:** 调低质量阈值、扩大 Dispatcher 并发、删除历史失败、重置当日 confirmed、改变 4800 目标、改变自然日 pacing、改变 20 条 Planner 有界批次。
- **Completion split:** “失败风暴修复上线并有 E4”与“自然日 4800 已完成”是两个结论，前者不得伪装成后者。

## Resources

- `docs/01-product/tg-ops-platform-prd.md`
- `docs/03-feature-designs/ai-group-daily-group-target-redesign-prd.md`
- `docs/03-feature-designs/task-fulfillment-contract-closure-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `backend/app/services/task_center/direct_check_in.py`
- `backend/app/services/task_center/daily_coverage.py`
- `backend/app/services/task_center/executors/group_ai_chat.py`
- `backend/app/services/task_center/ai_generation_quality.py`
