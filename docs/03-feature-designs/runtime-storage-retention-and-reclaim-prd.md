# 运行历史存储保留、清理与物理回收专项 PRD

> 状态：2026-08-30 Product Design Complete；本地实现、62 项无 PostgreSQL 回归、隔离 PostgreSQL 全迁移/并发清理/索引演练通过；待发布与生产 preview/apply/readback。
>
> 适用范围：`actions`、`execution_attempts`、Action 关联审核队列、运行汇总、明确命名的运行历史索引。Telegram 远端事实、未决状态、账号会话与业务主数据不在普通保留清理范围内。

## 1. 背景、目标与非目标

生产 PostgreSQL 已出现“每日产生大量 Action 明细，但长期排障价值快速衰减”的结构性增长。当前单一五日保留期无法区分成功、跳过、失败和远端未知状态；现有汇总又只保留状态数量，删除后无法按 Action 类型和类型化原因解释失败/跳过趋势。

本专项目标：

1. 用分状态热明细保留期替代统一保留期。
2. 在删除明细前固化足以运营诊断的类型化日汇总。
3. 用受保护的 `preview -> apply -> readback` 路径执行清理，拒绝漂移和越界目标。
4. 将逻辑删除与物理索引回收分开执行和验收。
5. 保持 Telegram 防重、远端未知、履约事实与审计合同不变。

非目标：

- 不用清理伪造任务履约、Telegram 成功或线上恢复。
- 不删除 `FulfillmentRemoteFact` 及各任务类型的权威远端事实。
- 不按时间删除开放、可重试、Gateway 已启动但结果未知的运行状态。
- 不在本期对 `generation_jobs`、群上下文、会话 speaker turn 做未经合同证明的批量删除。
- 不使用 `VACUUM FULL` 作为在线默认动作，也不以普通 `VACUUM` 冒充云盘缩容。

## 2. 生产基线与问题定义

2026-08-30 只读快照显示：生产 SHA 为 `c664c0138a5ac9bb50d2dde59ae020013c68eea0`，数据库约 14.94 GB，`actions` 约 5.44 GB，其中索引约 2.20 GB、TOAST 约 2.65 GB。`send_message` 占 Action 逻辑字节约九成，payload 中重复保存生成历史、话题计划、账号画像与记忆快照。

当前只清理 `success/failed/skipped`，三类状态共用五个自然日保留期；删除前仅按日期、状态、任务、账号、任务类型、目标聚合，未保留 `action_type` 与类型化失败/跳过原因。

问题不是“所有 Action 都没意义”，而是：已经明确终结的低价值明细保留过久；远端未知与业务事实必须长期保护；汇总粒度不足导致无法安全缩短 TTL；大量逻辑删除后索引物理文件不会自动缩小。

## 3. 状态分类与保留合同

### 3.1 时间口径

- 所有截止点以业务时区 `Asia/Shanghai` 的自然日零点计算，再转换为 UTC 查询。
- `as_of` 在 preview 时冻结；apply 不使用执行时的“现在”重新解释目标。
- 资格时间使用 Action 终态最后更新时间；状态未进入合同终态时，时间再旧也不清理。
- preview 冻结 `as_of`、各状态 cutoff、有界批次的确定性排序边界、候选数量、候选逻辑字节估算和集合指纹。Action UUID 不得被误作单调水位。

### 3.2 热明细保留期

| Action 状态 | 热明细保留 | 普通清理 | 理由 |
| --- | ---: | --- | --- |
| `skipped` | 1 个完整自然日 | 允许 | 多为可汇总的合同/节奏原因，不代表远端调用 |
| `success` | 2 个完整自然日 | 允许 | 近期定位和客服核验后由汇总与远端事实承接 |
| `failed` | 7 个完整自然日 | 允许 | 给失败复盘、规则修正和人工排查留窗口 |
| `closed_unknown` | 不设时间 TTL | 禁止 | 必须先证明它是权威关闭事实并另立冷归档合同 |
| `unknown_after_send` | 永久排除 | 禁止 | Gateway 可能已调用，时间不能消除远端不确定性 |
| `pending/claiming/executing/retryable_failed` | 不适用 | 禁止 | 开放或仍可推进状态 |
| `cancelled` | 不适用 | 本期禁止 | 取消原因合同尚未完成类型化覆盖 |

保留期是代码默认值和可部署配置，但配置不得把任何状态加入允许集合；允许集合只能由版本化代码合同变化。配置必须满足 `skipped >= 1`、`success >= 2`、`failed >= 7`，更长保留允许，更短必须走新的产品合同、迁移与验收。

### 3.3 关联 Attempt 保护

即使 Action 状态表面可清理，只要关联 `ExecutionAttempt` 存在 `pending`、`gateway_call_started` 或 `result_unknown`，整次 apply 必须在删除前失败关闭。不得只跳过异常行后继续，也不得删除 Attempt 来消除冲突。运维需要先按既有 reconcile 合同处理状态不一致，再重新 preview。

## 4. 删除前必须固化的证据

### 4.1 汇总维度与原因

每个候选 Action 删除前，至少累计到以下不可变日汇总：本地业务日期、Action 终态、`action_type`、类型化原因码，以及既有任务、账号、任务类型、目标维度总量。

原因码只读取结构化字段，优先级为：

1. `result.reason_code`
2. `result.error_code`
3. `result.failure_type`
4. `result.skip_reason`
5. `result.generation_outcome`
6. `ExecutionAttempt.failure_type`
7. `unclassified`

不得把原始异常、自由文本消息、Prompt、生成正文、账号记忆或 Telegram 内容写入长期汇总。原因码必须经过长度和字符规范化；空值统一为 `unclassified`，不得猜测。

### 4.2 守恒与幂等

- 同一 Action 的汇总记账和明细删除必须具备同一持久幂等身份。
- 每个 apply run 固化 manifest；每个 Action 只能在一个已提交清理批次中计数一次。
- `汇总新增总数 = 本次删除 Action 数`，按状态、类型和原因的分项之和也必须守恒。
- 进程中断后只能按同一 run manifest 续跑；不得重新解释新 cutoff，也不得重复累计。
- Action/Attempt/审核队列删除顺序继续遵守外键边界，任何删除失败都不得把该批次标记成功。

## 5. 受保护运维流程

### 5.1 Preview

preview 是只读操作，输入必须包含 `environment=production`、expected deployed SHA、actor、approval/incident reference、frozen `as_of` 和明确的保留期合同版本。

输出必须包含：数据库身份与当前应用 SHA；每个状态 cutoff、候选行数、逻辑字节估算；受保护 Attempt 冲突数；候选集合 SHA-256 指纹；有界批次排序边界与精确候选 ID；将写入的汇总维度数量；预计关联 Attempt/审核行数；精确命名的后续索引候选及当前大小；apply gate 结论。

preview 不创建 Action、不改变任务状态、不触发 Telegram/Provider 调用。

### 5.2 Apply

apply 必须显式回传 preview 的 `as_of`、expected deployed SHA、policy version、精确候选 ID、candidate count、fingerprint、actor 与 approval reference。执行前重新计算同一冻结集合；任一字段或集合漂移即零写失败。每个有界批次在一个事务内执行“汇总 -> 清理关联引用 -> 删除明细 -> 批次审计”；中断后对下一批重新 preview，不沿用旧指纹。

以下条件一律零写失败：

- 部署 SHA、环境或数据库身份不一致；
- 指纹、数量、精确候选 ID 或 cutoff 漂移；
- 出现受保护 Attempt；
- 汇总不守恒；
- run 已完成、actor/ref 或 policy version 不一致；
- 数据库锁/超时超过已声明运维预算。

### 5.3 Readback

独立 readback 不依赖 apply 进程内存，必须回读 maintenance run 状态、批次数、汇总数、删除数和失败信息；冻结候选集合剩余数量必须为零；受保护状态与受保护 Attempt 前后数量不变；`unknown_after_send`、`closed_unknown` 和 remote facts 未被本次 run 删除；汇总守恒；表、TOAST、索引物理大小与数据库总大小。

只有以上全部成立，逻辑清理才能标记 `persisted_verified`。这不等于 Telegram 业务履约证明。

## 6. 物理回收与索引治理

### 6.1 顺序

1. 先完成逻辑清理并独立 readback。
2. 运行普通 `VACUUM (ANALYZE)`，回收可复用空间并刷新统计信息。
3. 用真实执行计划和索引统计复核索引必要性。
4. 只对合同中精确命名的索引逐个执行并发重建或窄索引替换。
5. 每个索引完成后回读有效性、大小、查询计划和邻接索引状态，再处理下一个。

### 6.2 容量与锁闸门

任何并发重建前必须取得当前云盘可用空间/额度证据，并保留至少“目标索引当前大小 + 预估 WAL + 安全余量”。证据缺失时物理重建状态为 `blocked`，不得猜测容量。

禁止默认执行 `VACUUM FULL actions`、全库 `REINDEX`、同时重建多个大索引、未经执行计划验证直接删除索引，或安装扩展后立即用其改写生产表。

### 6.3 首期索引目标

- `actions`：先通过清理 + `VACUUM (ANALYZE)`，再逐个评估体积最大的运行索引；低扫描次数仅是候选信号，不是删除授权。
- `ai_group_message_memory`：用查询计划验证账号感知的窄索引可覆盖真实读取后，先 `CREATE INDEX CONCURRENTLY`，验证有效与命中，再 `DROP INDEX CONCURRENTLY` 旧的宽 INCLUDE 索引；任一步失败保留旧索引。

索引替换不改变 AI 记忆去重、10 日功能窗口、30 日历史回填或消息内容保留语义。

## 7. 配置、接口与页面影响

### 7.1 配置

废弃单值 `RUNTIME_DETAIL_RETENTION_DAYS`，改为：

- `RUNTIME_ACTION_SKIPPED_RETENTION_DAYS=1`
- `RUNTIME_ACTION_SUCCESS_RETENTION_DAYS=2`
- `RUNTIME_ACTION_FAILED_RETENTION_DAYS=7`
- `ENABLE_STATE_SPECIFIC_ACTION_RETENTION=false`（发布默认 rollout hold，定时任务按显式 5/5/7 长窗口运行；完成生产 preview/apply/readback 后才切为 true）
- 既有 batch size、interval 保持独立配置。

迁移期不允许静默读取旧值并覆盖新合同。部署缺少新配置时使用上述代码默认值，并在启动日志明确输出 policy version 与每个 TTL。

### 7.2 页面和 API

- 本期不新增常驻页面和人工清理按钮。
- 任务中心 Action 明细只承诺保留合同窗口；窗口外返回“明细已按保留策略清理”，不得显示成“从未执行”。
- 运营中心/运营数据的历史趋势读取长期汇总，可按日期、状态、Action 类型和类型化原因查看。
- 现有需要原始 payload/result 的 API 只在热窗口内提供；窗口外不从日志或备份做静默拼接。
- 运维 CLI/workflow 需要 production environment 权限；普通租户和前端用户无 apply 权限。

## 8. 并发、失败与恢复

- 清理选取使用稳定 fence 和行锁/skip-locked；普通 Dispatcher 不应再修改合同终态，但若发生修改，守卫必须暴露冲突。
- 同一环境同一时刻只允许一个 Action retention apply run；定时清理与人工维护 run 共享互斥锁。
- 单批失败保留最后成功游标与错误，run 标记 `failed`；修复根因后显式 resume 同一 run。
- resume 只处理 manifest 中未完成 ID，不能扩大目标集合。
- 部署回滚不会复活已删除明细；因此 apply 只能在新版本完成 QA、发布和 preview 后执行。
- 索引并发创建失败时，清理 INVALID 新索引前必须精确确认名称；旧索引始终保留到新索引验证通过。

## 9. 发布、迁移与回滚

发布顺序：additive schema/汇总/maintenance run -> 新配置与代码（先保持较长窗口或关闭自动 apply）-> preview 与 QA readback -> 打开分状态 TTL -> 受保护生产 apply -> 逻辑 readback -> 容量闸门通过后逐个物理回收。

代码回滚：恢复上一 SHA 和较长 TTL，停止新的清理 run。数据回滚：已经安全汇总并删除的运行明细不在线恢复；长期汇总、远端事实和审计仍保留。若业务明确需要恢复原始明细，只能走数据库备份到隔离环境，不得覆盖生产当前库。

## 10. QA 与生产接受

### 10.1 自动化验收

- 分状态 cutoff 在时区边界、闰日/月末正确。
- `skipped/success/failed` 分别只选择超过 1/2/7 完整自然日的行。
- 所有开放、unknown、closed_unknown、cancelled 状态永不被普通选择。
- 任一候选绑定受保护 Attempt 时整次 apply 零写失败。
- 原因优先级、`unclassified`、长度规范化正确，不保存原始文本。
- preview 指纹对 ID、状态、更新时间或 cutoff 任一漂移敏感。
- apply 重试、崩溃续跑和重复 readback 幂等。
- 汇总总量、按状态、类型、原因均守恒。
- 外键关联清理没有孤儿；remote fact 和未决邻接行不变。
- 新窄索引在代表性数据上被目标查询使用；新索引 valid/readable 后才允许删除旧索引。

### 10.2 生产接受

必须分别报告 deployed SHA、runtime policy、preview exact count/fingerprint、apply audit/summary conservation、protected-state unchanged readback、post-vacuum/reindex table/index/database bytes，以及云盘实际使用量变化。若云厂商指标有延迟，记录采样时间与延迟，不用表内大小代替。

完成定义为 `persisted_verified`。生产容器健康、Action 数下降、数据库大小下降均不能单独替代上述证据。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 用户原始需求 | 覆盖整体优化、Action 每日数据价值和生产清理 |
| 生命周期与状态 | 分状态 TTL；开放/unknown/closed_unknown/cancelled 边界明确 |
| 前端/API | 明细窗口外语义、汇总读取与权限边界明确 |
| 后端/worker | 配置、汇总、manifest、互斥、preview/apply/readback 完整 |
| 数据流 | 终态 Action -> 类型化汇总 -> 关联清理 -> 删除 -> vacuum/index 回收 |
| 并发/幂等 | frozen fence、fingerprint、单 run、批次游标、resume 守恒完整 |
| 权限/安全 | production environment、actor/ref/SHA、零 Telegram 调用 |
| 失败路径 | 漂移、受保护 Attempt、锁、空间、INVALID 索引均 fail-closed |
| 迁移/回滚 | additive 先行、策略后开、代码回滚与数据不可在线恢复边界明确 |
| QA/生产证据 | 本地、PostgreSQL、发布、生产 readback 分层，不混淆业务 E4 |

`design_status=complete`。实现不得放宽本合同；若发现任何需要删除的新状态/表或缩短最低 TTL，必须先回到产品合同补齐并标记 resync。
