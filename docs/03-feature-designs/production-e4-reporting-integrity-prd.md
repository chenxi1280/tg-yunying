# 生产 E4 诊断统计与证据完整性修订

## Intake 与交付边界

- intake_id: production-e4-reporting-integrity-20260909；L3/P1。
- 用户要求先修 PRD，再实现并通过测试，同时避免与两个正在修复的任务冲突。
- 本切片只修复统计、状态说明与验收判定；不改变调度、生成、发送、账号资格、重登、准入或历史数据。
- 基线：2839c10f；独立工作树 `/tmp/tg-yunying-e4-reporting-20260909`，分支 `codex/e4-reporting-integrity-20260909`。
- merge_owner：本任务仅管理本分支；其他两个任务分别持有运行修复/发布与Clone范围，均已确认无诊断路径重叠。不得自动并入其正在验证的候选。
- locked_paths：`.github/scripts/task_fulfillment_e4_diagnostics.py`、本切片新增 `backend/app/services/task_center/production_e4_{group,attempts,blockers}.py`、E4定向测试、本PRD和独立实施记录。共享索引入口说明交统一发布owner整合，不覆盖他人的索引修改。

## 反向检查与问题分组

1. `_group_blockers` 以 `attempts.post_release_remote_success_count` 判断新消息事实；其来源仅是成功Attempt和非空message id，无法区分发送后不可见、缺少typed fact、成员操作或错误身份关联。
2. `_group_daily_snapshot` 排除 `abandoned_for_day`，令未完成覆盖从报告分母消失，不能说明完整历史需求和欠量。
3. `_group_runtime_snapshot.coverage_counts` 只有行数；多目标同账号、不同状态的交叉集合容易被误读成独立账号数。

## 产品合同

### 消息事实与执行回执分开

- 保留原 `post_release_remote_success_count` 作为发送回执诊断，不据此判定AI活群E4通过。
- 增加 `group_daily.post_release_remote_fact_count`，仅统计同tenant、Task、当前ledger、task_type=group_ai_chat、fact_kind=remote_message_observed、mutation_kind=send_message的事实，并关联原Action和Attempt。
- 关联必须满足 fact.attempt_id 对应 Attempt、fact.action_id 对应 Action、Attempt.action_id 与 Action相同、tenant一致，Action的Task/类型/payload任务日与事实相同、Action.account_id 与 Attempt.account_id 相同、Action为send_message、Attempt.status=success、非空remote_message_id与fact.outcome记录一致。Attempt没有独立task_id，由原Action建立Task关联。
- 事实 observed_at 与原 Gateway call-start 均不早于输入发布锚点，且观察不得早于调用。旧调用迟到回填不冒充发布后的新执行。没有Gateway时间不得推断已执行。
- 相同Action的重复观察只计一次。成员事实、unknown、其它任务日、其它租户、错误账号/Attempt关联均不能贡献本次新消息数量。
- 没有该字段或计数为零时，既有 `ai_post_release_remote_fact_missing` 明确保留；不从旧回执字段降级补值。typed fact是既有可见性合同的结果，本切片不新增远端探测。

### 完整分母与当前状态分开

- 本修订仅更改诊断读模型。反向审查发现群日总量专项PRD的2026-09-08补正明确区分legacy动态scope与unified冻结selected：不得把统一分母规则反向套到合法legacy任务。
- `coverage_total_count` 始终是同tenant/Task/ledger的全部覆盖义务行数。统一合同`unified_engagement_v1`的`coverage_required_count`等于该全量，包含abandoned_for_day、unknown、待准入和受阻记录，账号后来冻结/失效不改变该值；legacy保留原active scope必达数，并同时展示完整历史与abandoned数。本修订取代的是统一任务误用legacy排除规则的行为。
- 增加 `coverage_active_count`、`coverage_abandoned_count` 供解释历史与当前；active仅表示未abandoned，不表示当前可执行。
- `coverage_confirmed_count` 仅采用state=confirmed、正target、confirmed达到target且非空远端ID的既有覆盖投影。它是覆盖投影，不冒充逐条typed审计结果。
- 保留原群数量target/due/confirmed语义；不得因查询账号健康而降低目标，不写回任何账本。
- `group_runtime.coverage_counts[].count` 保持记录行数，增加 `distinct_account_count`；增加当前ledger全量去重 `coverage_distinct_account_count`。不同状态/原因分组存在交集，各分组去重人数不得直接相加；全量去重必须单独计算。

## 数据流和实现约束

只读Task/ledger → 覆盖全量和状态聚合 → Action/Attempt执行回执 → 精确typed fact关联 → E4 blocker输出。无网络副作用、无行锁、无状态写入、无额外周期任务或模型调用。

既有脚本超过500行，按本次涉及责任拆出Attempt、活群统计及blocker模块，脚本保留原入口和测试所用函数导出；搜索/浏览判定原样迁移，回归验证不变。诊断模块只注入数据库Session，依赖既有模型，不调用调度或恢复实现。

## QA验收与Product Design Complete

必须覆盖：

1. 成功Attempt有message id但无typed fact，不通过E4；未决/不可见不得冒充确认。
2. 合法当前ledger新发送事实通过；其它tenant/Task/ledger/类型、错误Attempt/账号、缺Gateway、发布前调用、重复事实分别验证。
3. unified的abandoned/unknown/待准入均保留分母；legacy合法放弃后的当前required保持原行为，全量历史仍可见；账号冻结不改变历史分母；相同账号多条coverage区分行数与人数。
4. 非confirmed状态即使残留确认数字或remote id也不算confirmed覆盖。
5. 旧字段缺失不得fallback到Attempt回执；搜索和浏览E4回归不变。
6. 新查询不产生UPDATE/DELETE/INSERT，输入对象不被修改；后端测试单次硬超时60秒。

设计自检：原请求、证据层、历史目标、字段兼容、租户/身份、unknown、发布时间、重复观察、无写库及冲突隔离已覆盖。`design_status=complete`、`resync=true`；进入本切片dev。部署和生产验收独立记录，不以本地测试声称production_fixed。

## 2026-09-09 二次检查：验收范围与一致只读快照

- `resync=true`：本节补充同一诊断切片；locked_paths增加`production_e4_scope.py`及`test_production_e4_scope.py`。两个并行任务已确认不修改这些路径，当前Prepare/Deploy窗口由“确认克隆任务引擎更新”持有，本提交另行交接。
- 代码发现：默认channel_view发现查询带固定10条上限且未排除软删除；这是代码反例，不是线上第11条任务漏检的观测结论。
- 未指定任务或只指定`__discover_channel_view__`时，发现全部未软删除、状态为running/completed的channel_view任务；不截断，以updated_at降序、id升序稳定排序。显式ID仍按输入顺序去重，不扩展或替换范围。
- 显式指定的软删除任务必须保留在报告，输出`task_deleted=true`并产生`task_deleted`验收缺口；保留原status和历史证据用于说明，不能因历史ledger/事实齐全而通过。未删除任务输出false；缺失任务保留既有missing口径。
- PostgreSQL CLI必须在任何发现或Task读取前建立REPEATABLE READ READ ONLY事务，同一事务内读取整份报告；设置既有只读排障规范的statement_timeout=20s、lock_timeout=2s。事务设置或查询失败直接暴露，不生成通过摘要、不退回普通事务、不写入业务表；会话退出回滚并释放连接。不会改变业务执行边界。
- QA新增：11条合格任务全部被发现、软删除/不合格类型及状态排除、同更新时间排序稳定、显式ID顺序/去重不变、显式软删除Task快照及三种已支持类型均不可通过；验证事务命令先于发现/读取、设置失败不继续读取或输出通过摘要。
- Product Design Complete复核：无需迁移、前端或执行链变更；自动范围与显式范围、历史证据语义、并发读取一致性和失败路径均闭合，`design_status=complete`，进入dev。
