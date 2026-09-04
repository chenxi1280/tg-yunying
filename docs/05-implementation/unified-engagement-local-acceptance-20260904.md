# 统一互动引擎本地修复与验收（2026-09-04）

## 范围与结论

本次按用户确认的轻量边界（主 PRD §19.13–19.16）修复四类引擎，重点落实“评论任务与活群任务独立”。对象为当前本地工作区（HEAD `477b1969` 加既有及本轮未提交变更），不是仅验收 HEAD。未提交、未部署、未操作生产配置或数据。

已查实的业务缺口已修复，并完成下表的代码入口、定向回归及迁移核对。本地验收通过不等于真实 Telegram 履约或线上时延达标；不以测试中的 Gateway 替身当远端成功事实。

## 当前需求验收矩阵

### 2026-09-05 三个深层停摆缺陷修复（最新）

本轮修复上一轮确认的全局wake阻塞、晚到transport ACK丢失、预算/韧性策略锁序逆转。仅本地代码与测试，未提交、未部署，无真实Telegram/Provider请求。

- Planner：聊天唤醒使用独立逐条事务，turn先于outbox加锁，候选在LIMIT前跳过锁忙turn；只物化当前领取轮次。savepoint关联写锁等待100ms并恢复原会话值。失败保留pending、显式日志，其他轮次和任务继续。
- Transport：超时异常保留原runner的真实termination Event；Dispatcher登记tenant/action/attempt receipt，并在后续轮询非阻塞写入原Attempt ACK。DB失败保留receipt，提交成功才移除；既有回收释放物理lease，业务unknown/去重身份不变。不用健康探测、TTL或重启冒充原调用终止；进程在证据落盘前崩溃仍unproven，不自动修复无证据历史记录。
- 锁序：资源结算在预算账本之前锁原fence绑定的resilience revision，与资源准入一致。
- 主定向回归：**240 passed, 35 deselected, 9 warnings in 33.68s**，使用backend/.venv及60秒硬超时。35项为本次no_postgres筛选未运行的集成测试，不算通过；9条警告为Python3.12 SQLite日期适配器弃用提醒。扩展capacity发现旧用例用object充当Session，已改为真实SQLite Session和父记录，无产品测试专用绕过。
- 相邻对话/生成隔离回归：6个不重叠文件 **47 passed in 7.58s**（burst assembly/successor、group/comment update stream、negative outcome entries、generation isolation）；本轮两组共 **287个不同用例通过**。
- 独立PG16：实际准入与回收交错均成功；首turn锁忙时健康turn关闭且四类任务进入逐任务规划入口；Action锁忙时receipt保留、解锁后ACK落盘仅释放物理容量；LIMIT 1可跳过锁忙前缀；TaskPlannerWake锁超时回滚当前turn、恢复原lock_timeout，解锁后仅关闭一次。测试数据库为专属/tmp/tgyunying-three-fixes.HrrbWO/data、55476、tg_yunying_test，验证路径和库名后使用，已停止且保留测试数据。
- 新模块函数长度检查、语法编译、git diff --check通过。没有新增迁移、费用预算持久化、历史回放、发送重试或前端页面；不宣称整个平台不存在其他停摆风险，不作为生产履约证据。

### 2026-09-05 停摆加固问题修复

本节覆盖此前“租约新改动未验收、负反馈恢复入口未闭合”的状态。本轮无提交、部署、真实Provider/Telegram请求或生产数据操作。

- 未决保护：共享settlement不再把非终态Attempt当failed。Action失败/取消、租约一小时过期均不能释放已出Gateway的未决调用；已存在transport ACK不被旧snapshot降级。确实未调用的预留要求Attempt未开始、Action已终止、无Gateway/fence开始时间、lease/budget/fence均reserved，才记skipped_before_gateway并释放。
- 回收：先在SQL中选有待结算差异的记录，再取批次，held unknown不占满前缀；Action/Attempt锁序和SKIP LOCKED避免被锁首条拖住后续任务。单条失败savepoint回滚并显式记录，计数只计真实状态迁移。Dispatcher和stale-action recovery取消吞异常后强行释放的路径。
- 策略：首次无历史可初始化；retired/superseded继承原配置生成下一revision；disabled/paused不自动重开。唯一约束UPSERT处理并发初始化，历史版本不被覆盖。
- 负反馈：新增 `GET /api/negative-outcomes` 与 `POST /api/negative-outcomes/{circuit_id}/review`。POST提交 `expected_version`、`reason`、`evidence`，按现有认证/租户解析处理；保存AuditLog、原反馈及去重身份，清除被复核scope的当前停发。新反馈可以重新升级，旧重复消息不会重新累计。无需新发送即可人工复核恢复，包括manual_review；自动恢复不放宽。只提供API，未新增管理页面。
- 最终定向合并回归 **96 passed in 13.36s**；使用backend/.venv、60秒硬超时。包括误释放、未调用释放、unknown保留/ACK单调、首条未决不饥饿、重复回收、单条失败隔离、策略退休/停用、人工复核CAS/租户/重复反馈及HTTP列表/POST入口。
- 独立PostgreSQL16：三类策略双事务同时初始化只产生一个有效身份；回收跳过另一事务锁住的首个Action，后续租约正常结算且再次回收为0；最终SQL谓词验证未调用终止请求释放一次。只有本地隔离库，无Gateway替身被计作真实远端证据。专属测试集群已停止，保留临时测试数据。
- 新模块函数长度检查、Python语法编译与git diff --check通过；未运行仓库全部测试，也不将这些结果扩大为全平台无停摆风险或生产履约验收。

### 2026-09-05 全局停摆专项复验

- **已修：** conversation freshness 与 account/proxy circuit 的 Python 时间比较统一 `as_beijing`。修复前独立 PostgreSQL 两轮 Planner 均在 due wake 处异常，四类任务选择均未执行，wake 一直 pending；修复后 wake/机会/claim 正常提交，实际选择器能将四类任务交给逐任务规划入口。此验证不代表四类 adapter 已真实履约。
- **已修：** expired open / half-open 的探活认领不再因 PG aware 日期报错；独立 PG 验证认领、owner/dependency 核对、结算与业务门禁解除，只有网关被测试替身替代，没有发送真实 Telegram RPC。
- **QA：** 新增时间边界回归及 conversation/circuit/realtime 合计 35 passed in 5.04s；另组 generation isolation、HTTP deadline、dispatch lock-order、负反馈入口、planner isolation、runtime resources 合计 82 passed in 22.37s；两组不重叠，共 117 项。全部后端测试使用 `.venv` 与 60 秒硬超时。语法编译与 `git diff --check` 通过。
- **仍未闭合：** 负反馈 response_restricted 后无 pending visibility 时，两类发言门均拒绝，新恢复证据只能由已被阻止的发送产生。独立复现一小时后仍被阻断，重复结算旧 visible 不恢复；其他 route/peer/account 不受影响。PRD 的负反馈闭环行降为部分完成，不以单独调用 recovery helper 通过宣称生产入口可达。不擅自按超时解除内容隔离，也不通过真实试发制造恢复证据。
- **并发变更边界：** 本次检查期间另有写者加入 runtime stale-lease recovery 与 service 接线，未覆盖这些改动；本段 117 项通过不构成该新增回收路径的专项验收。没有提交、部署或生产数据操作。

### 2026-09-05 四项复审修复

- 0223 未发布 revision 缩短至 `0223_burst_negative_outcome`；验证整条 Alembic graph identity 不超过默认 version 列长度、SQLite 带原 turn 数据的 upgrade/downgrade 与新增 ORM 列/route 唯一键一致。本轮没有运行 PostgreSQL 实库升级或线上迁移。
- 迟到碎片以 successor turn/opportunity/claim 接续；旧 bound claim 终态保持 stale，重复投递幂等，Gateway 已调用或 Provider unknown 不重新聚合旧事件。listener supersession 与 Gateway 发前共同持有 claim 锁，远端 probe 后再刷新，覆盖探测中失效窗口。
- Reaction 普通及相册规划共用的 `message_reaction_plan` 使用完整冻结正文；final gate 同时复核 capability、source revision 与正文互斥。严肃负向内容下庆祝表情不发送；能力不支持和语义不匹配分别返回 typed reason。
- 负反馈从真实活群/评论 update delivery 和 visibility settlement 接入。真人反馈要求明确投诉、原生引用、同 tenant/route/peer 的唯一已确认 parent；感谢 AI、泛泛讨论或仅描述自动化身份不触发。稳定远端事件键去重；not_visible 不推断管理员拦截；评论/活群/其他账号作用域隔离，点赞/浏览不消费发言质量熔断。自动恢复需 hold/window 满足且出现新 visible 证据；manual_review 不自动恢复。
- 最终合并回归：25 个文件 **159 passed in 26.54s**；随后收紧“像机器人”中性描述，新增一条不误伤场景，负反馈入口 **15 passed**（独立场景合计 160）。全部使用 `backend/.venv`、`-m no_postgres`、60 秒硬超时。唯一 warning 是既有 Alembic `path_separator` 配置弃用提示。
- PRD §19.1.9、结构索引与数据流索引已同步；本地修复不代表 PostgreSQL 双事务竞态实测或 Telegram 真实履约。未提交、未部署、未修改生产数据。

### 未提交审查三项修复复验

- 编辑契约：新增 `EngagementSettingsUpdate` 可选字段模型接入 PATCH，补齐活群 attention allowlist；四类任务真实创建/编辑/回读与部分更新均回归，非法范围、重复/空分组和错用任务字段仍拒绝。
- 首次来源：`channel_snapshot_binding` 校验当前任务的历史 N 完成证明，不能复用旧任务 ready 快照提前冻结。验证分页中途加入、生命周期/启动时间/任务类型/N 变化、重启持久化、历史真实不足 N、初始化后不重抽，以及 legacy/specific 入口不受影响。
- 前端保存：创建/编辑共用 `channelIntakePayload`，三类频道编辑携带首次历史条数和来源预期，不携带不可编辑的目标字段。
- 本轮独立证据：后端10个定向文件 **94 passed in 9.95s**，含新增26项；前端实际 payload 转译测试 **3 passed**；`npm run build` 通过（保留构建大 chunk 提示）；`git diff --check` 通过。后端使用 `.venv` 与60秒硬超时，无生产连接。
- PRD §10.2.1 和结构/数据流索引同步；无新增迁移、未提交、未部署。下表与历史测试记录不能替代本段新增场景的验证。

| 当前业务要求 | 实际入口/实现 | 本地验证 |
|---|---|---|
| 活群和评论互不消耗生成批次、不串行等待对方 | 独立 `ai-generation` / `comment-generation` role、worker/health/fence、all 模式独立循环；恢复限定 task type | generation isolation、worker roles、pending/unknown recovery |
| 评论重试不在单批中反复占位 | comment worker 排除本轮已领取 Action；正常重试和 unknown 保护保留 | 同批 retry/healthy 两候选都推进且各一次 |
| 显式绑定多个账号分组 | API/schema → canonical group binding → membership snapshot → participation plan | account binding、API 三种频道任务持久化回读 |
| 所有计划账号活跃、健康账号继续、坏账号不缩分母 | group daily coverage、planning admission、fleet typed activity | daily group/coverage、participation、fleet、runtime resources |
| 日量冻结抖动、小时内分散、同号跨任务错峰 | daily quantity、stratified/source pacing、account Timeline/behavior session/pair gaps | pacing、source pacing、behavior sessions、portfolio、真实 PG owner locks |
| 活群/评论轻量 JIT | effective send 减 10 秒准备；claim 检查 not-before/retry/epoch；发送时刻与绝对截止不被提前 | generation timing、实际 worker、comment jobs、旧 pristine pending Job 对齐 |
| 真人新消息快速响应，引用和上下文匹配 | 新消息/turn/wake → group/comment adapter；发前 target/context gate；attention 退让 | conversation、continuity、attention、realtime timestamps、comment interaction |
| 防重复、删除失效和未知结果禁止重放 | 生成质量/内容身份、Job lineage、Provider exchange、Gateway/fact identity | generation deep、quality、unknown propagation、PostgreSQL commit recovery |
| 忽略聊天编辑、不建设历史回放/费用平台 | 新消息与删除继续；编辑不制造新机会；取消画像批准作为运行门槛 | update stream、generation timing/binding、recent success |
| 评论/点赞首次历史 N 冻结，之后动态来源不被 latest N 截断 | common intake；评论实际 `_persisted_channel_scope` 已接线；元数据/过滤与固定初始键 | comment actual source entry、intake、three-task config tests |
| 来源超过单页仍能追上，过滤后 N 与分页相册正确 | offset 持久游标、每轮一页、lease/revision fence、原头页观察时间 | 实际 listener 多页采集→intake；重启/不推进/旧 owner 反例 |
| 无帖子与监听未知、评论不可用分别显示 | continuous/finite/promised；冻结日模式；关闭评论区独立 capability 状态；迟到证据修订日界归因而不补发 | source-day modes、实际 comment scope、PostgreSQL 日界/迟到证明 |
| 点赞不用 LLM；单帖账号目标与日 RPC 总量同时约束 | reaction capacity、intent/capability、typed reaction facts | 单来源不超配、多个来源公平分配、cap/late source tests |
| 相册每号稳定 1～2 子操作，全部成功才完成账号义务 | logical source → parent/children → real Timeline/portfolio → frozen obligation Action → child facts | 9图×50号不展开450次；实际 adapter、再规划0重复、partial/unknown、详情聚合 |
| 相册执行沿用正确账号分组和来源身份 | 分配与执行共用 reaction_source_identity；代表图变更保留原分配 | exact participation plan lookup、representative-change tests |
| 浏览大部分账号每日参与且比例抖动，不逐帖全刷 | daily cohort、三日 debt、账号—来源子集、first applicable day、typed view identity | participation、view journey、bipartite matching、daily lifecycle/fact chain |
| 公开频道浏览不强制入会/发言权限 | view 专属 planning/member/dispatcher gate；其他类型不跟随豁免 | 已加入/未加入两种实际规划→dispatch→fact 测试 |
| 面具/代理/账号/Provider 局部故障隔离 | 独立生成 worker、typed timeout、pool/proxy/egress leases、circuit/probe、未知调用占位 | runtime resources、circuit probe、worker isolation、Provider failure tests |
| 最近三天成功次数 | 详情读取滚动72小时类型化成功事实，去重 Action，按原 attempt 账号归属 | recent success，四种类型/边界/迟到/unknown/账号移组/详情接线 |
| 配置和详情可用 | 三类频道表单初始N/预期模式，账号分组；来源/相册/72h详情 | schema→service→persisted config；TypeScript/Vite build |
| 空库/旧库升级可执行 | 0220 source intake、0221 album、0222 cursor；legacy bootstrap 不提前建新结构 | 真实 PG 空库及0196带数据到0222、旧绑定保留、ORM/FK/索引 |

## 本轮额外修复的深层问题

1. 只改共享 intake 未覆盖评论独立查询入口：现已接通真实评论路径，不再把 helper 测试当完整接线证明。
2. 单来源的日总量余额可以分配超过其 distinct-account target，导致 participation 校验异常：现在双重限量。
3. 相册执行账号来源仍按单图 revision 查询，及代表图变化后重新选人：现统一逻辑身份并保留分配。
4. 完成相册仍按“每张图都满额”给出无账号提示：现按全部冻结子事实结算。
5. 相册剩余子操作在部分确认后被重新编号、配置 revision 变化重新建义务：现保留 ordinal 与 obligation ID。
6. Listener 页内成功不等于连续观察完整，错误也不能覆盖最后成功观察时间：现有持久游标、头页时间及 owner fence。
7. `deadline_source_ingestion_unproven` 长34超 PostgreSQL事件字段32：使用明确短码 `deadline_source_unproven`，不截断隐藏错误。
8. 日界 source-unproven 永久不再检查：现只修订归因，冻结当天模式，不创建发送、不把未知计成功。
9. 评论迟到采集不能重新延长帖子生命周期：状态判断与真实评论合同同用 Telegram 发布时间。

## QA 证据与边界

- `.planning/engine_business_repairs/qa_*.xml` 保存分批 JUnit。重新 collect 当前79个相关测试文件后，807个当前用例均有通过结果，0失败、0跳过；排除了两个已撤销/参数化替代的旧用例名，未把重复执行相加。
- 其中13个使用隔离原生 PostgreSQL16（127.0.0.1:55472，数据库 tg_yunying_test）：升级2、生成并发/提交恢复5、source pacing1、owner locks2、来源日界3。其余使用本地纯逻辑/SQLite/测试依赖，未发起真实 Telegram 操作。
- 最后一组新增业务修复回归67项通过；生成深回归172、评论业务82、活群目标89等组有重叠，只作为分组证据，不与807再相加。
- 每个后端 pytest 进程使用 backend/.venv，外层 alarm60秒。曾超时或失败的轮次不计通过；已修复测试中旧30分钟假设、夹具提交/唯一名称及时间编码断言，并完成相关重跑，没有放宽产品门禁。
- 前端 TypeScript/Vite build 通过；既有656KB chunk体积告警未作为本任务进行无关重构。新增模块函数度量、语法解析及 git diff --check 通过。
- 没有运行整个仓库全部测试、浏览器端到端交互、真实 Provider/TG、线上时延或多日真实量分布。因此以上只属于本地源码与定向验收。
- 用户已确认解除披露自动化身份限制，支持自然人设（Persona）与真实拟人化运营互动；代码已彻底移除强制身份披露注入。
- 不恢复已经撤销的历史回放/版本档案、费用扣减、历史画像审批或完整预算编排。旧 PRD 的历史切片状态不能覆盖 §19.13 的当前范围裁决。
- 临时原生测试数据库已精确停止，保留 `/tmp/tgyunying-engine-qa.MOeOCx` 调试数据；Docker daemon不可连接，因此原先本任务创建的 `tgyunying-engine-business-qa-20260904` 容器未能核查/清理，未启动或重启Docker，也未清理其他数据库/容器。
