# AI 活群准入与数量槽收敛修复 PRD

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-08-03 |
| 分级 | L3 生产履约故障 |
| 状态 | `design_status=complete`、`resync=true` |
| 适用任务 | `group_ai_chat` + `all_accounts_daily` |
| 关联真相源 | `tg-ops-platform-prd.md`、`ai-conversation-humanization-and-group-bot-admission-prd.md`、`all-task-fulfillment-recovery-prd.md` |

## 1. 原始目标与事故事实

用户要求先在不重复发送、不改写远端事实的前提下临时恢复线上任务，再完成永久修复 PRD、自审、代码、测试、发布和生产验收。

2026-08-02 至 2026-08-03 的生产证据显示：

- 郑州师范、郑州楼凤的冻结日目标未完成，大量账号处于群管准入等待；
- Planner 在没有准入可发账号时，会把 `admission_waiting` 账号回填为预留 Action 以驱动准入；旧链路直到 Dispatcher 才以 `group_bot_admission_wait` 终结，导致 AI 已被无效调用、Action 终态噪声以及下一轮重复建单；
- 郑州楼凤在当日存在 766 个 ready coverage 和对应 open 数量槽时仍短暂出现 `quantity_slots_unavailable`，稍后自然形成 2 个 ContentMix、32 条 open Action；
- 保护性生产恢复预览发现现有 open Action 已覆盖当时 `due_by_now`，因此没有执行 `60/12 -> 1/1` 配置写入。临时恢复没有改目标、没有批量改 ready、没有触碰 Gateway-started/unknown；
- 线上 Dispatcher 已具备群管确认源实时刷新、精确收件人匹配和失效 source supersede。永久修复不重复实现该链路，只要求运维恢复工具遵守同一安全合同。
- 首轮永久修复发布后，两任务分别形成约 90 条开放正文义务，但连续多轮 E4 的 AI generation ready 和真实远端成功仍为 0；精确生产诊断确认 261 条 Action 满足生成领取条件、群生成占用为 0，仅 38 条受账号忙影响。
- 3 个 AI generation worker 心跳均在秒级刷新，运行合同为 active，生产全局 AI generation executing claim 为 0；与此同时 PostgreSQL 中恰好存在 3 条相同指纹的 Action 候选查询，已持续执行 51–115 秒且未等待锁。容器健康只证明独立心跳线程存活，主线程实际卡在领取扫描。

## 2. 根因与范围

### 2.1 已确认根因

1. **准入门禁位置过晚。** `_load_daily_coverage_plan_accounts` 的“无 ready 时使用 `admission_waiting`”是现有准入推进器，不能直接删除；问题是 Action 没有冻结准入快照，generation worker 在 Dispatcher 最终门禁之前已经调用 AI，而 Dispatcher 又把等待写成 terminal skipped，随后 Planner 只能重复建单。
2. **数量槽错误被过度归并。** `_freeze_content_mix_cycle` 只比较期望条数与已对齐条数，并把所有差异压成 `quantity_slots_unavailable`；`build_plan` 又捕获全部 `ValueError`，使真实程序错误、并发状态变化和业务不变量损坏无法区分。
3. **分配锁域不完整。** coverage keyset 有每日游标锁，但 ContentMix 冻结阶段未明确锁住 target-day 目标行及被选数量槽。多个 Planner 或同日 replan/new-cycle 交错时，蓝图读取与槽位冻结缺少一个最终一致性栅栏。
4. **历史文档冲突。** `all-task-fulfillment-recovery-prd.md` 同时存在“旧槽等待时继续独立 Cycle”和“任一旧槽等待时禁止新 Cycle”两种口径，导致实现和验收可能反复摇摆。
5. **AI generation 领取缺少生产级候选索引。** `_generation_filters` 在全局 pending Action 上同时判断 task、JSON generation 状态、空正文、账号占用和同群 generation 槽，再按时间排序取首条。现有统计索引以 `tenant_id, task_id` 开头，不能服务跨任务 due claim；通用 due 索引又不能排除大量非 AI 或已有正文 Action。数据量放大后，3 个 worker 都长期执行同一候选 SQL，尚未取得 Action 行锁和 claim，导致“心跳健康但生成量为零”。

### 2.2 本次范围

- 正常正文优先选择群管准入 `ready`，以及已经持久绑定、按原义务恢复的 `post_follow_visibility_probe` 账号；没有可发账号时允许为 waiting 账号创建唯一的准入驱动 Action，但该 Action 只能占用原 coverage/数量槽，不得调用 AI 或 Telegram 正文；
- 已存在准入行的 Action 创建时冻结 admission id/state/version；generation worker 在 Provider 前重读准入，waiting 时保留同一 Action为 pending 并定时复检，Dispatcher 在 Gateway 前再次执行相同门禁；发布前遗留的无 admission 行仍遵守既有 DF-192 首条正文可见性 hold 合同，本次不改变该兼容边界；
- ContentMix 在短事务内锁定 target-day 目标与候选数量槽，以精确 coverage 身份对齐并原子冻结；
- 槽位差异输出结构化状态，不再用泛化错误或捕获所有 `ValueError`；
- 旧 Cycle 优先重建，但不能阻塞与其数量槽、coverage 均无交集的新 Cycle；
- 更新总 PRD、数据流转索引、结构索引、定向测试和生产诊断。
- 新增只覆盖“待生成空正文”的 partial due-claim 索引，键序与领取排序一致；临时恢复和 Alembic 最终迁移使用同一索引名与谓词，避免形成两套生产结构。

### 2.3 非目标

- 不降低 `effective_message_target`，不缩冻结账号分母；
- 不把 admission、coverage、Action 或 ContentMix 批量改成成功/ready；
- 不释放 Gateway-started、pending visibility、unknown 或成功事实；
- 不改变 AI 话术、reply 比例、面具、Provider 轮数和 Dispatcher 共享容量；
- 不通过删除当前日账本或历史 Action 恢复吞吐。
- 不用放宽准入、账号互斥或同群 generation 槽换取查询速度；索引只缩小候选扫描，不改变可领取集合。

## 3. 产品不变量

1. 完成只认 `Action success + ExecutionAttempt success + non-empty remote_message_id`；需要可见性核验时还必须 `visible_confirmed`。
2. `TgGroupAccount.can_send` 与 `GroupBotAdmission.state` 独立；waiting 账号只能进入带准入快照的驱动蓝图，未放行前不得生成或发送正文。
3. `admission_waiting` 投影为 `pending_group_bot_admission`，由既有 observation/follow/confirmation/probe lane 推进；驱动 Action 只承载原义务和复检时钟，不是“探路正文”，不得创建替代 Action。
4. 每个 ContentMixSlot 必须绑定同一 Task、同一 task-day ledger 的唯一主数量槽；coverage 正文只能绑定本账号精确 coverage 槽，extra-volume 正文只能绑定 coverage 为空的 extra 槽。
5. 同一 `primary_quantity_slot_id` 永不被两个 CycleSlot 共享。任一步失败，Cycle、CycleSlot、合同和 Action 整体不产生半成品。
6. old-cycle replan 与 independent new-cycle 可并行收敛，但新 Cycle 必须排除所有已绑定旧 Cycle 的 coverage/数量槽。
7. AI generation worker 的数据库心跳、容器 health 与实际 drain 是三类证据；只有 claim/ready 持续增长才能证明生成主线程可工作。

## 4. 规划状态机

### 4.1 账号候选

```text
coverage ready
  -> Telegram online/can_send
  -> GroupBotAdmission ready
       -> normal body candidate
  -> post_follow_visibility_probe + persistent bound/reclaimable action
       -> probe candidate
  -> other admission state
       -> one reserved admission-driver Action
       -> generation pre-provider gate: keep pending; no provider/body
       -> dispatcher pre-gateway gate: keep same Action pending; no Telegram body
```

若本轮无可发正文候选但存在准入等待账号：Task 保持 running，记录等待账号数和 blocker，并为 due 原义务保留幂等驱动 Action。每次复检必须重读 admission；未 ready 时不得加载 Provider 凭据或建立 generation attempt，Action 不进入 terminal，亦不得改写为账号离线或数量槽不足。ready 后由同一 Action继续生成和发送。

### 4.2 数量槽对齐结果

冻结阶段只产生以下互斥结果：

| code | 含义 | 动作 |
| --- | --- | --- |
| `aligned` | 每个 logical item 都找到精确且未绑定的 open 槽 | 原子创建/复用 Cycle 并物化 |
| `existing_cycle_replan_required` | 期望 coverage 槽已绑定旧 Cycle | 回到旧槽 replan；不得借槽 |
| `quantity_slot_state_changed` | 蓝图后槽被其他合法事务占用或状态变化 | 本事务不建 Cycle，记录差异，下一短事务重新取蓝图 |
| `extra_volume_slot_unavailable` | extra item 没有 coverage 为空的 open 槽 | 不补量、不借 coverage 槽，记录 pacing/shortfall |
| `quantity_slot_invariant_mismatch` | coverage 槽缺失、跨 ledger、身份不一致或数据损坏 | 显式 L3 blocker，停止该蓝图并进入诊断 |

`content_mix_target_missing`、zip 长度错误、数据库异常等不属于数量槽业务结果，必须继续抛出到 `planner_runtime_error`，不能再被 `except ValueError` 吞掉。

## 5. 事务、锁序与幂等

1. Planner 先完成旧 Action takeover/retry 并提交，随后在新事务重新加载 running Task。
2. coverage 选择继续锁 `TaskDailyCoveragePlanCursor`；ContentMix 冻结时按固定顺序锁 `TaskGroupDailyTarget -> TaskGroupDailyMessageSlot`。
3. 未绑定槽使用相关 `NOT EXISTS(ContentMixCycleSlot.primary_quantity_slot_id=slot.id)` 判定，避免宽泛子查询语义；PostgreSQL 对选中槽 `FOR UPDATE`，SQLite 测试保持等价无锁路径。
4. 取得最终锁后重新执行精确 item-to-slot 对齐。对齐失败不得创建 Cycle；下次规划重新读取事实，不复用旧 ORM 蓝图。
5. Cycle 唯一键、CycleSlot 数量槽唯一键和 Action `(cycle_slot_id, slot_attempt)` 继续作为数据库最终幂等栅栏。
6. replan 槽 `created=0` 后可以建立独立蓝图，但 `_bound_coverage_account_ids_for_plan` 必须排除所有旧 Cycle 已绑定 coverage；新旧 Cycle 不共享主数量槽。
7. AI generation due claim 必须命中 `task_type=group_ai_chat + action_type=send_message + status=pending + account_id 非空 + generation_status 可生成 + message_text 为空` 的 partial index，并按 `scheduled_at, created_at, id` 读取；账号占用和同群占用仍由原查询及行锁复核，不得移除安全条件。

本节 supersede `all-task-fulfillment-recovery-prd.md` §4.5.1 中“只要存在待物化/重建槽就不得另建 Cycle”的句子；保留同节关于旧槽不释放、不换号、不改 relation、Gateway-started/unknown 禁止替代的约束。

## 6. 数据、接口与可观测性

本次不新增表，新增 Alembic migration 创建 `ix_actions_ai_generation_due_claim` partial index。PostgreSQL 必须使用 `CREATE INDEX CONCURRENTLY`，SQLite 使用等价 JSON predicate；migration 若发现同名有效索引则 no-op，若存在同名 invalid 索引则显式失败，禁止静默跳过。使用现有 Task stats 记录：

```json
{
  "pending_group_bot_admission_count": 106,
  "quantity_slot_alignment": {
    "code": "quantity_slot_state_changed",
    "requested_count": 20,
    "aligned_count": 19,
    "missing_coverage_ids": ["..."],
    "ledger_id": "...",
    "recorded_at": "..."
  }
}
```

- API 继续通过 Task 详情现有 stats/last_error 展示，不新增写接口；
- Action payload 保存 `group_bot_admission_id/state/admission_version` 快照；存量 open Action 在 Planner 接管时补齐，generation/Dispatcher 每次以数据库最新事实覆盖快照；
- 日志只输出 task/ledger、数量和内部 ID，不输出手机号、Session、callback data 或消息正文；
- 成功完成一次对齐后清除旧 `quantity_slot_alignment`，避免历史 blocker 冒充当前状态；
- `pending_group_bot_admission_count` 只表示当前规划快照，不从冻结分母扣除。

## 7. 运维临时恢复合同

临时工具必须 preview-first，并同时满足 expected state hash、零 open Action、零当前日 ContentMix、零 Gateway-started open Action才允许 apply。任一事实变化立即拒绝。允许的临时配置改动不得改变每日目标；禁止直接改 admission ready、remote id、coverage confirmed 或释放 unknown。

若任务已自然形成 open Action 且覆盖 `due_by_now`，正确结果是 no-op，不再强改数据库。本次生产预览即按此合同拒绝 apply。

当生产已证明 generation 主线程卡在无 claim 的候选扫描时，允许执行第二类临时恢复：先校验 deployed SHA、索引不存在或有效、全局 AI generation executing claim 为 0，再以 autocommit `CREATE INDEX CONCURRENTLY` 创建与最终 migration 完全同名同谓词的索引。索引有效后只重启 3 个 AI generation worker 以中断旧查询计划；不得重启 Dispatcher、修改 Action 或终结 Attempt。任一 executing AI claim 出现时拒绝重启并保持索引即可。

确认 source 修复必须复用运行时合同：原消息精确读取为空后，扫描当前账号最近 300 条带按钮控制消息；只接受可信 bot、收件人匹配、频道集合一致的新 source。无匹配时 supersede 旧 callback 并清 source；网络读取失败保持 retry，不能当删除。

## 8. QA 与验收

### 8.1 红测

1. 只有 `admission_waiting` 时，可创建绑定原 coverage/数量槽的唯一驱动 Action；generation worker 必须在加载凭据/调用 Provider 前延后，同一 Action保持 pending，不产生 Attempt 或正文。
2. 同时有准入 ready extra 账号时优先选择 ready extra；waiting 不得挤占可立即履约的 ready 容量。
3. coverage item 只能匹配自身 coverage 槽；extra item 只能匹配 coverage 为空的槽。
4. 已绑定槽不再出现在可用集合；目标行和选中数量槽在 PostgreSQL 路径加锁。
5. 对齐不完整返回结构化 code/count/coverage IDs，且数据库中 Cycle/CycleSlot/Action 数均不增加。
6. `content_mix_target_missing` 等非对齐错误向上暴露并写 `planner_runtime_error`。
7. old replan `created=0` 时，未绑定的独立 coverage 仍可创建新 Cycle；旧数量槽不复用。
8. admission 从 waiting 变为 ready 后复用原 Action 生成；若 generation 后、Gateway 前状态回退，Dispatcher 将同一 Action退回 pending，不能写 `group_bot_admission_wait` terminal 或建替代 Action。
9. migration 在 SQLite/PostgreSQL 分别创建相同语义的有效 partial index；重复 upgrade no-op、invalid 同名索引显式失败。
10. 生产量级回归必须证明 due-claim 查询使用新索引，且候选领取不再出现分钟级扫描；已有“同群 ready 时跳过该群并生成其他群”和“执行中账号不可领取”语义保持不变。

### 8.2 Release Gate

- 定向 SQLite/no-postgres 测试通过；
- PostgreSQL 并发测试证明两个 Planner 不共享数量槽，且 loser 得到可重规划状态；
- 相关后端分区、静态编译、YAML、diff-check 通过；
- master 合并后由 `master -> release -> Deploy Production` 发布；
- deployed SHA、release symlink、容器健康与 migration head 一致。
- PostgreSQL `pg_indexes/pg_index` 证明新索引存在且 valid，`EXPLAIN`/运行态证明 due claim 不再分钟级占用 3 个 worker。

### 8.3 生产 E4

发布后分别验证郑州师范和郑州楼凤：

- waiting 驱动 Action 在 admission ready 前无 Provider 调用、无 generation attempt、无 Gateway attempt；`group_bot_admission_wait` 不再形成新 terminal Action，且同一数量槽没有替代 Action；
- `quantity_slots_unavailable` 不再出现，若存在槽差异则展示精确 code 和计数；
- ContentMix/CycleSlot/Action 数量守恒，无重复 `primary_quantity_slot_id`；
- AI generation ready、Dispatcher claim、ExecutionAttempt success 和非空 remote message id 持续增长；
- 全局 AI generation claim 不再恒为 0，数据库中不得持续出现分钟级同指纹 due-claim 查询；
- unknown/pending visibility 不被替换，daily target 与 coverage confirmed 只随真实远端事实增长。

只有上述生产事实成立才写 `production_fixed`；代码测试或部署成功单独只算 E2/E3。

## 9. 回滚

- 代码回滚到上一 release，不回滚或删除发布期间已产生的 Action、Attempt、ContentMix、coverage 和远端事实；
- 回滚前先停止 Planner/AI generation/Dispatcher，确认无正在进入 Gateway 的新 Action，再切换 symlink 并恢复 worker；
- 新 stats 字段可由旧版本忽略；应用回滚默认保留安全索引，只有确认旧版本无需且数据库负载稳定时才并发 drop；
- 若发布后出现对齐 invariant mismatch，保持任务 running + 显式 blocker，不允许通过批量 ready、删 Cycle 或缩目标解阻。

## 10. 反向审查与补全

| 反证场景 | 设计结论 |
| --- | --- |
| waiting 账号是唯一账号，删除正文补位会不会永远不准入？ | 会，这正是第一版红测暴露的架构反证。因此保留唯一驱动 Action，但把安全边界前移到 Provider 前；既有 admission lane 推进状态，驱动 Action只负责复检并在 ready 后履约。 |
| waiting Action 会不会反复终结、重建并重复占槽？ | 不会。generation 和 Dispatcher 都把同一 Action保持 pending 并更新重试时间；数量槽、coverage、cycle slot 与 Action绑定不变。 |
| Action 创建后 admission 回退怎么办？ | generation 前和 Gateway 前都重读最新 admission；回退只延后同一 Action，禁止调用 Provider、Telegram 或创建替代。 |
| 一个旧 replan 槽暂不可用，是否必须阻塞整个 Task？ | 否。只冻结旧槽自身；其他未绑定 coverage/数量槽可建独立 Cycle。 |
| 两个 Planner 同时读到同一 open 槽怎么办？ | target/slot 锁和唯一键只允许一个提交；另一方不借槽，记录 state changed 后重读。 |
| open 槽数量够但 coverage 身份不匹配怎么办？ | 不是容量不足。输出 invariant mismatch，禁止按 ordinal 借另一账号槽。 |
| 已有 open Action 覆盖当前 due 时是否继续修配置？ | 不。记录 pacing/no-op，等待真实发送。 |
| 捕获专用对齐异常是否会隐藏代码错误？ | 不会；只处理结构化对齐结果，其他异常继续上抛。 |
| 临时恢复能否批量清 admission source？ | 不能。必须逐账号实时读取和可信源匹配；读取错误保持 retry。 |
| worker heartbeat 新鲜是否证明 generation 正常？ | 不证明。心跳由独立线程写入；必须同时看全局 generation claim、ready 增长和数据库活动。本次 3 个心跳新鲜但 3 条同指纹查询持续 51–115 秒且 claim=0，正是反例。 |
| 直接删掉账号/同群占用子查询是否更快？ | 不允许。这会破坏账号串行和同群内容槽。使用精确 partial due-claim 索引缩小外层候选集，保留现有安全复核。 |
| 临时索引和最终 migration 会不会冲突？ | 不会。二者使用完全同名同谓词；migration 对有效同名索引 no-op，对 invalid 同名索引显式失败。 |
| 是否需要数据库迁移？ | 需要新增一个并发 partial index；不新增表、不改业务数据、不回填 Action。 |

自检已覆盖原始需求、产品合同、Planner/generation/Dispatcher/运维职责、数据流、权限安全、并发幂等、unknown、防重复、失败路径、迁移、回滚、QA、Release Gate 和 E4。第一版“删除 waiting Action”已被既有测试和 580 账号边界证伪并撤销；补全后的合同保留驱动 Action、前移 Provider 门禁、复用同一义务，既不切断准入推进，也不允许未 ready 正文。发现的“旧槽是否阻塞独立 Cycle”文档冲突已明确 supersede，非业务异常不再被泛化错误吞掉。首轮发布后的 E4 又反证“容器健康即生成正常”：现已补齐生产量级 due-claim 索引、独立心跳误导、临时并发建索引、worker 安全重启和 migration 幂等边界。

**Product Design Complete：`design_status=complete`，允许进入 dev。**
