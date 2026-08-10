# Progress Log

## Session: 2026-08-09

### Phase 1: 基线与需求重新同步

- **Status:** complete
- **Started:** 2026-08-09
- Actions taken:
  - 读取 `planning-with-files` 技能与模板。
  - 运行 session catch-up；没有需要恢复的未同步上下文。
  - 核对所有工作树、隔离设计分支状态和最新可见 `origin/master`。
  - 比较 `9a1405aa..origin/master`：AI 业务代码未漂移，但 worker-role/compose 发生变化，需纳入新 wake/reconcile 运行拓扑交接。
  - 开始抽查现有模型、Planner、Generation、Gateway、TaskDayLedger 与 worker role 边界。
  - 确认 `TaskDayLedger` 已冻结时区 revision、业务日和 deadline；不需要新增第二套日期字段，但需写 FK/跨日不变量。
  - 确认现有 `recovery`/`ai-generation` role 可分别承接 DB wake/Gateway 与 Provider reconcile；当前 PRD 尚未明确运行 ownership。
  - 发现 §8 尚缺可并行开发的具体 API 路由/分页/一致性/兼容合同，列为候选 P1。
  - 搜索 API 时使用了旧猜测路径；已定位真实路由为 `api/routers/task_center.py` 并记录。
  - 隔离工作树无本地 venv；将改用主项目共享 `backend/.venv` 做只读 Alembic heads 检查。
  - 初查远端事实投影与物理删除，发现 projector retry consumer ownership 和新表删除顺序需要明确。
  - 使用共享 venv 验证 Alembic head 为 `0144_avatar_material_sources`。
  - 定位现有 daily-fulfillment/actions/attempts API 与 TaskDetail/TS 类型，确认应新增独立分页资源并冻结兼容合同。
  - 审查 generic lifecycle API，确认 retry/reset/stop 当前会绕过新义务合同；列为必须在开发前补齐的 P0 控制面契约。
  - 审查既有 activation/takeover route，确认 task-level route 与新 task-day route 的组合关系和 lifecycle epoch 校验尚未定义。

### Phase 2: 实现就绪审查

- **Status:** complete
- Actions taken:
  - 按模型、投影 worker、API、生命周期控制面和发布 fence 分解缺口。
  - 审查 TaskGroupDailyTarget/due 代码，确认 target scope/version/FK 与 TaskDayLedger/typed-fact 读法需要在交接中显式补齐。
  - 审查现有 Cycle/messages_per_round 语义，发现 aggregate plan scope 公式和主 PRD due-by-now 冲突需要补齐。
  - 审查 generation basis 与当前模型，发现 memory/provider revision owner 和跨表 transaction order 尚未定义。
  - 独立 reviewer 确认 route activation/takeover 顺序、Gateway call-issued 崩溃窗口和 remote-fact obligation unique 为新增 P0。
  - 复核最新 origin/master 三个提交：业务根因未改变，但共享 Gateway/worker/部署路径已漂移，开发需新工作树 resync。
  - 定位 Provider、context、profile 的事实写入口，确认 wake producer inventory 必须显式列出。
  - 独立 reviewer 完成只读复核，结论 `blocked`；其 9 个 P0 与本地审查收敛，并补充 assignment/API/Task status/provider reconcile/Release artifact 的 P1。
  - 确定不扩充 Task status：迁移/兼容阻断归 task-day route/runtime，Task 保持现有 paused 状态。

### Phase 3: 缺口补齐

- **Status:** complete
- Actions taken:
  - 将专项设计状态保持为 `partial`，开始逐节补 control-plane、route、Gateway、projector、migration、API 与事务合同。
  - 补齐 ledger-bound target/ordinal/due、canonical fact identity+quantity binding conflict、assignment/current-intent matrix、wake producer/consumer ownership。
  - 补齐三层 route+epoch、generic lifecycle command matrix、Gateway Tx A/Tx B/call/Tx C、provider reconcile 与 durable fact projector。
  - 补齐 canonical CAS 顺序、typed API/snapshot cursor、takeover quiescence/排序/分块/delta/readback、Release Gate artifacts 与 QA/E4。
  - 发现主 PRD 禁止 SKIP LOCKED 后，将 wake/projector 统一改为 partial-index keyset + 逐行 CAS，消除合同冲突。
  - 同步主 PRD、DF-193D 和旧 AI 群日专项 supersede 状态；修正旧“无 due_by_now”、旧 Gateway 与 UI 摘要口径。

### Phase 4: 独立验收与静态校验

- **Status:** in_progress
- Actions taken:
  - `git diff --check` 初检通过。
  - 准备让原独立 reviewer 对冻结快照重新完整复核 P0/P1。
  - 独立复核连续发现并已补齐：fleet policy与Dispatcher runtime contract分离、inventory在线membership、rollback compatibility baseline、ledger bootstrap、lifecycle control/adoption、Gateway deadline硬门、event-before-subscribe、takeover A/B source fence、legacy Action current-owner替代、remote fact quantity binding、PATCH字段族和API snapshot。
  - 补齐TaskGroupDailyTarget实际DDL：drop旧full unique，legacy/current partial predicates、CHECK、takeover新建current row及旧row hash readback。
  - 补齐物理删除留存：item/enrollment retired、logical task ID+nullable导航FK、ContractTombstone、fleet hash/count和remote fact不随Task级联。
  - 清理adoption残留`idle`枚举，固定Adoption owner索引；不同command ID的重复pause/stop改为Task状态级幂等并补并发QA。
  - 当时刷新`origin/master=4bfdb946`；该值已过期，2026-08-10最新复核见下方续记，实现仍须在开工时重新fetch并从最新master建新工作树。
- Files created/modified:
  - `.planning/.active_plan`
  - `.planning/2026-08-09-ai-group-failure-repair-readiness-gap/task_plan.md`
  - `.planning/2026-08-09-ai-group-failure-repair-readiness-gap/findings.md`
  - `.planning/2026-08-09-ai-group-failure-repair-readiness-gap/progress.md`

## Test Results

| Test | Expected | Actual | Status |
| --- | --- | --- | --- |
| 工作树隔离 | 不接触主 release 脏工作树 | 使用既有隔离设计工作树 | pass |

## Error Log

| Timestamp | Error | Attempt | Resolution |
| --- | --- | ---: | --- |
| 2026-08-09 | `backend/app/api/task_center.py` 不存在 | 1 | 改用 `backend/app/api/routers/task_center.py` |
| 2026-08-09 | 隔离工作树无 `backend/.venv/bin/alembic` | 1 | 使用主项目共享 venv |

## 5-Question Reboot Check

| Question | Answer |
| --- | --- |
| Where am I? | Phase 4：独立验收与静态校验 |
| Where am I going? | 独立 reviewer 零P0/P1结论、静态校验、交付 |
| What's the goal? | 找全从设计完成到生产修复完成的硬缺口 |
| What have I learned? | 原设计通过不等于实现就绪；接管、wake、lifecycle、删除留存的并发/审计合同必须逐项闭合，所有实现和E4仍未开始 |
| What have I done? | 补齐实现合同并同步专项/主PRD/数据流，正在等待最终独立复核 |

## Session: 2026-08-10

### Phase 3/5: AI设计再同步与浏览生产诊断

- **Status:** in_progress（等待fresh independent zero-P0/P1 review）
- Actions taken:
  - 只读生产确认deployed SHA=`6f594303`且核心role健康，但两个浏览Task分别只有31/25个future Action、0 Attempt、0 ViewRemoteFact；due远高于物化量，最晚Action均到23:57。
  - 复现`reserve_task_schedule_times()`使用Task最晚future Action作floor后把新批整体移出deadline；另确认Task级180秒间隔把理论上限压到约480/日，均发生在Dispatcher前。
  - 确认第三个Task没有fresh source；现有UI/last_error无法区分healthy-empty、listener stalled和source unresolved。
  - 确认eligible lifetime-view身份约869/871，小于每消息1000，必须物化可达部分并报告typed structural shortfall；不能返回0或自动降目标。
  - 新建`channel-view-planner-starvation-remediation-prd.md`，冻结message target+due ordinal、ledger-wide账号slot最大匹配、Action/fact binding、source event/subscription、deadline settlement、fleet inventory/in-place takeover与E4防假绿。
  - 同步主PRD、分类/闭合专项和DF-193E/BG-004F/DF-332；AI旧“无ordinal/prepared新Task从0/只迁移running”口径改为历史。
  - 最新已fetch`origin/master=6db995cb2cc5c94b805b6647219cbd060269a59a`，含独立`worker-material-cache`；设计分支不用于开发，独立验收通过后再次fetch并从当时最新master开实施worktree。

### Phase 3/4续记：current合同冲突收口

- **Status:** in_progress（等待fresh independent zero-P0/P1 review）
- Actions taken:
  - 重写DF-167/168/173/174/187/189/192-197/200：AI/view create只建Task+active enrollment，first-start由TaskStartOperation原子建完整bundle；PATCH使用field-family+expected Task.version；generic retry/reset固定409；delete为202+tombstone；current AI读模型不再以Cycle/Action冒充完成。
  - 收口AI deadline唯一owner：time-due subscription、普通wake、Planner、Generation、Gateway Tx A/Tx B、safely-not-executed与lifecycle仅拒绝新外部工作并幂等激活SettlementOperation；只有settlement target chunk可终结obligation/FOP/handoff/owner并写immutable snapshot。
  - 同步主PRD、closure、classified与DF的ActiveDueRankSet、原Task fleet takeover及Action-first历史标记；current API/worker不能从旧索引恢复空正文Action、prepared新Task从0或Task级future-tail。
  - `git diff --check`通过；endpoint与deadline ownership定向扫描无旧直接终结路径。
  - 补齐settlement缺失rank的唯一终态空内容合同：仅`settlement_shortfall + deadline_unmaterialized* + SettlementTargetItem`可无assignment/current intent，数据库CHECK与所有claim predicate fail-closed；新增QA覆盖普通义务空指针拒绝。
  - 重写DF-182/183/184/186/198并登记DF-200A-D：详情/统计只读typed read model，generic/type PATCH共用field-family与expected Task.version，同步204删除明确HIST并由202 operation唯一取代，AI/view义务与attempt下钻使用repeatable-read signed cursor。
  - 把旧coverage reservation→Action、admission获得Action资格、ready Action原地清正文路径明确标为historical_do_not_implement；current normal正文只走stable obligation/assignment/intent→GenerationJob/variation/memory→ready Action。
  - AI own-history统一为同tenant/Task/group的bound canonical remote fact/quantity binding；Action/Attempt仅provenance/timeliness，不再承担reply资格。
  - 独立终验剩余两组P1已收口：classified GenerationJob移除尚不存在的action_id并冻结obligation/assignment/intent/generation identity，accepted variation+memory事务才创建ready Action；classified reply来源同步bound canonical fact。
  - 主PRD移除“一个open Action整Task延后30秒”的饥饿门禁，改为ActiveDueRankSet逐rank owner anti-join后继续物化其它0～20 gap；30秒只属于精确owner wake，随后公平轮转其它Task。

## Error Log Addendum

| Timestamp | Error | Attempt | Resolution |
| --- | --- | ---: | --- |
| 2026-08-10 | 动态替换DF长表行时`rg`正则转义退化为全文件匹配 | 1 | 改用`rg -F -- '| DF-xxx |'`精确单行，再由apply_patch逐行替换；失败调用未写文件 |
| 2026-08-10 | 独立reviewer完成新P0报告后stream disconnected | 1 | 已按其可见的deadline双owner证据修复；待冻结新hash后复用现有reviewer继续fresh复核 |
