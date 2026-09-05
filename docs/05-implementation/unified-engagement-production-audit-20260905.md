# 统一互动引擎生产审计与修补（2026-09-05）

- intake_id: intake-20260905-engagement-production-audit
- level: L3
- source: 用户要求按统一互动 PRD 检查线上并修补
- route: prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis
- baseline: clean master f869375f9a751d06d84c5ffa755337b813c9a706
- production: /data/tgyunying/releases/20260905010257_f869375f
- migration: 0223_burst_negative_outcome
- observation: 2026-09-05 09:40–09:44 Asia/Shanghai，SSH、数据库只读事务
- evidence: 本机 /tmp/tgyunying-engine-audit-20260905/ 的脱敏计数与边界快照，不含凭据/消息正文
- production_status: unproven

## Root Cause Grouping

| 编号 | 观察事实 | 判断及修补范围 |
|---|---|---|
| R1 | 23个运行中任务的 unified 配置为空、binding表0行；21个all，2个单组 | 程序已上线，存量任务未接管。须按PRD §17核对等价账户范围、重叠目标、在途身份，不能直接批量开关 |
| R2 | 发布后浏览有66个result_unknown；异常为目标username解析不存在，发生在view RPC之前 | Gateway混淆解析错误与mutation unknown。修补实际RPC边界；不追溯改写旧unknown |
| R3 | 评论Action af5465f7-913b-4562-a22f-c1c8d039949b executing/generating、lease 08:18到期，ExecutionAttempt=0 | Job被误判action_missing后cancelled，Action的Provider started已持久化。修正身份解析；历史行仅可精确纠正为unknown，禁止重调Provider |
| R4 | 浏览发布后约4000个pacing_source_not_before尝试；郑州精品大量目标无效 | 排期重领与目标不可解析分别诊断；不能把跳过算成功，不能凭名称猜新目标 |

## 当前业务证据

09:40快照存在活群、评论、浏览的发布后类型化成功事实；点赞快照暂未观察到发布后reaction fact。活群的membership_observed不计发送；浏览view_observed不等于Telegram计数器增长。全天目标尚未到截止点，不能以当前缺量直接判日目标失败。以上均为legacy Task的事实，统一引擎E4尚无样本。

## Product Handoff / Release Gate

R2以PRD §19.17为开发合同；变更限定Gateway浏览边界和针对性回归，无schema/前端修改，不恢复或重试旧任务，不改变目标/数量。真实RPC进入前失败须带false；RPC未知须保留None；成功仍一次RPC。

- local_gate: passed；三组61项no_postgres定向回归（10+32+19），语法与diff check通过；本机5432无服务，PostgreSQL验证由候选CI独立执行
- code_review: passed；浏览实际RPC边界保留unknown，评论精确身份不放宽活群/跨作用域匹配；恢复模块拆出身份职责后488行
- release_path: master -> release -> Deploy Production
- migration_impact: none
- rollback: 本切片无新数据结构，旧版本会恢复错误分类；不通过rollback消除unknown
- production_probe: 发布SHA与worker，随后自然view调用的Attempt、mutation标记及typed fact；旧unknown保持不重放
- release_status: release_passed；R2调用边界生产验证通过，R3状态纠正persisted_verified；全引擎production_unproven

## 逐任务72小时成功读模型（09:50附近只读采样）

直接调用生产 recent_task_success，已按实际确认时间、类型与Action去重；与09:40粗粒度fact_kind数量不同，不能混用。下表全部仍为legacy。未知Job只表示模型结果待定，不等于Telegram未知。

| 任务 | 类型 | Task ID前8位 | 72小时成功 | 开放Action | 未知Job |
|---|---|---|---:|---:|---:|
| 郑州楼凤 | channel_comment | 64f009db | 80 | 21 | 0 |
| 郑州楼凤 | channel_comment | 819b4b75 | 13 | 4 | 0 |
| 阿哥日记 | channel_comment | 16c8bbc2 | 11 | 3 | 0 |
| 太郎日记 | channel_like | ae990b83 | 27 | 130 | 0 |
| 成都阿楠 | channel_like | 4f5d79e1 | 35 | 197 | 0 |
| 西安焦点 | channel_like | 6c96a45b | 0 | 0 | 0 |
| 郑州楼凤 | channel_like | 3aafdd0f | 32 | 459 | 0 |
| 郑州精品 | channel_like | 8e8656e9 | 0 | 375 | 0 |
| 阿哥日记 | channel_like | d8aedaa1 | 12 | 33 | 0 |
| 太郎日记 | channel_view | 7383f82b | 1923 | 1856 | 0 |
| 成都阿楠 | channel_view | a14615dc | 0 | 0 | 0 |
| 西安焦点 | channel_view | 73d3692c | 1993 | 735 | 0 |
| 郑州精品 | channel_view | d0dd6015 | 6 | 850 | 0 |
| 阿哥日记 | channel_view | fa75ca69 | 1493 | 235 | 0 |
| 三亚 | group_ai_chat | f77ebe14 | 942 | 301 | 40 |
| 天津一品楼 | group_ai_chat | f2832260 | 1077 | 246 | 14 |
| 天津音乐 | group_ai_chat | 7fd0bbb7 | 263 | 312 | 10 |
| 成都怡红院 | group_ai_chat | b6f0ebd6 | 292 | 181 | 11 |
| 西安天上人间 | group_ai_chat | cb862a03 | 1045 | 316 | 27 |
| 郑州大学 | group_ai_chat | a52e84f2 | 64 | 165 | 15 |
| 郑州学生会 | group_ai_chat | e8152470 | 1053 | 320 | 16 |
| 郑州师范 | group_ai_chat | 0361a7ac | 1067 | 542 | 15 |
| 郑州楼凤 | group_ai_chat | 6407d98f | 1224 | 266 | 51 |

两个郑州楼凤评论任务的目标均为同一数值频道peer，构成统一接管的单写者冲突；用户已明确选择保留819b4b75，64f009db只收口旧工作；执行正式暂停前先修正其误取消Job。全部活群均存在unknown GenerationJob，须保持原身份对账，不得批量换route后重建义务。

## 其余线上边界（09:53）

- 成都阿楠浏览最近72小时成功0、开放Action0；目标来源表有12条，最新发布时间2026-08-20。当前缺少新鲜来源，不能把单纯的“running”当履约，不根据旧帖子造新需求。
- 西安焦点点赞最近72小时成功0、开放Action0；目标表有3条来源，最新发布时间2026-09-03 16:49。是否还有可用reaction需目标能力/来源期限证据，尚未证明。
- 郑州精品浏览使用固定数值peer；近期大量目标实体不可解析及账号任务abandoned。尚无新的目标身份或账号访问事实，不能擅自换目标或重置账号处置。
- 排期反查发现同一Action在50分钟内最多52次pacing_source_not_before，都是pre-Gateway等待、释放点持续后移。可证明legacy来源竞争造成反复延期；本轮两项修补不改变该排期策略，也未证明统一Timeline公平性。这一项仍是未修复/未验收边界，不能算在已解决项。

## 发布执行

代码候选：94e386e290739b7dd698c0360eab9684a22c60e1。已正常推送master与release，Actions运行33937358173，所有可选生产数据/重试输入保持false。精确历史评论纠正另有preview/CAS脚本，只允许一条已核实action_missing记录进入unknown并审计，不执行模型或Telegram调用。

候选CI：validate-release-candidate、frontend-checks、3个backend-no-postgres-checks及2个backend-postgres-checks均通过；进入镜像构建。

## 发布与独立读回结果（10:07）

- Actions 33937358173终态success；代码候选94e386e290739b7dd698c0360eab9684a22c60e1。
- production current=/data/tgyunying/releases/20260905015919_94e386e2；backend、Planner、Recovery、Listener、评论生成、三组活群生成、两组Dispatcher共10个容器均running/healthy，RELEASE_SHA全部与候选一致。
- 观察锚点10:00:41（两组Dispatcher启动后）：16条view_observed，9条原“未知错误”现在为failed+safely_not_executed；无新增channel_view remote_outcome_unknown。另有16条unknown_deadline_closed结算来自发布前Attempt，不能误算成发布后新调用未知。目标无效/账号任务abandoned仍存在，因此只关闭错误分类子问题，不宣称所有浏览目标恢复。
- 历史评论修正preview fingerprint为5efcbd2c450ba8c09f5e1e0788c1742b28376998fb6bdd6485fcf899d3cf2aeb，audit=972519；独立读回Job unknown/version5，Action pending/provider_result_unknown，过期lease为空、Telegram Attempt仍0。没有重新调用模型/Telegram，不算评论发送完成。
- 按用户明确选择保留819b4b75，暂停64f009db；操作preview fingerprint为18f429ddd557ad5f573cc8984fde860a3bbd9b82f536f2519e41999a5f4605e7。选择审计972523，正式暂停审计972524。读回旧Task paused/epoch3；新Task running/epoch2/config_revision1；旧Job仍unknown/version5。
- 观察窗尚无发布后活群消息、评论和点赞成功事实；旧成功记录不能替代此次发布后的验收。
- 统一引擎尚未接管：检查时23个running Task全部legacy；暂停重复评论后剩22个running。账号范围等价、存量open/unknown身份收口及统一route迁移仍未完成，不能写全引擎production_fixed。

本轮未修改目标地址、账号处置、业务数量或批量恢复未知；未解决项是R1完整迁移、R4反复延期，以及目标/来源能力边界。不能因本轮两个缺陷发布通过就把这些项视为已完成。

## 性能修补 Release Gate（10:40）

- PRD 合同：19.18；R4 来源预约重领竞争与统一规划 N+1。无 schema、前端、业务数量或生产配置变化。
- 反例：三个逾期预约原来同一释放点；修补后不同释放点并稳定重领。21 账号（含缺失账号）原来 66 次 SQL；批量检查与逐账号完整 evidence 结果一致，SQL 有界。
- 本地定向：排期相关 24 passed/2 PG deselected；统一参与及批量规划 24 passed；独立 PostgreSQL 16 的来源预约/过期边界 3 passed。PG 使用本机 55439、tg_yunying_test 和测试 advisory lock，真实空库迁移至 0223，通过后保留用于后续接管验证。
- 生产只读基线：100 账号规划 221 SELECT、3.539 秒；legacy all 稳定业务集合与 11 个 enabled normal 组并集均 1633，hash 相同，无遗漏成员。
- Code review：新/失效预约均在既有 source row lock 内推进尾游标；已有有效预约不移动；call_started/unknown 在进入游标前被原 guard 拒绝；批量成员关系仍复用同一判定，代理同一查询预取，不建立跨请求缓存。
- 发布路径：master -> release -> Deploy Production。应用代码可回退至 94e386e2，预约状态与库结构兼容，回退会恢复性能缺陷；不对 unknown 执行恢复。
- 生产验收待办：相同 100 账号只读规划查询数/耗时；自然运行的等待 Attempt/Gateway/成功比率；全引擎接管与四类型 E4 仍未完成。

## 性能候选生产读回与生成积压根因（11:05）

- 880dad5e952d76328033561666969df6bcd76ff0 / Actions 33939632437 终态 success；current=/data/tgyunying/releases/20260905024628_880dad5e，已独立核对 backend、planner、ai-generation、dispatcher-1 的启动与健康状态。
- 相同 100 账号生产只读探针：221 SELECT / 3.539 秒 → 6 SELECT / 0.129 秒，可用账号均为 92。该单次对照证明重复查询消除，不外推为全系统吞吐提升。来源排期仍需后续窗口核验。
- 10:55 的真实候选查询总匹配 1105、返回 60，全部 Job state=unknown / available=false。最早项为 acfecf38-78a6-48fa-99cf-ac447dd3ca26（03:55），解释了近千条到期生成工作长期排不上；不是以 worker healthy 推断模型工作正常。

## 生成队列与接管前置 Release Gate

- 合同：PRD 19.19–19.20。候选 LIMIT 前排除不可领取 Job；未知继续原身份对账。配置规范化保留正式内容路由及救援字段；统一参与范围沿用租户/Task 救援号排除，暂时离线成员仍保留欠量。
- 反例：未知项占满候选窗口以及有效/缺失 generating 租约，修补前 4 failed / 4 passed，修补后通过。最初测试 fixture 缺必填列、PG 测试 ID 过长已纠正，不属于生产缺陷。
- local_gate：新增候选/未知保护/Provider 准入 31 passed；真实生成 worker 与 fact-first 52 passed；配置/绑定/资格前置两批 37+45 passed；PostgreSQL 候选公平性与 partial unique index 2 passed。每批后端测试硬超时 60 秒，PG 使用隔离的 tg_yunying_test / 55439 与 advisory lock。
- code_review：候选 SQL 与既有 _job_available 状态语义相同，排序与批次上限不变；Job/Action CAS 未改，未知行不写；内部字段无新增公开输入入口；成员快照不删减。无迁移、前端、目标地址或业务数量改动。
- release_path：master -> release -> Deploy Production；回退应用会恢复队列饥饿，数据格式兼容。发布后必须验证候选有可领取工作、Job 推进和新的活群发送事实；22 个存量 Task 接管仍为未完成。

## 生成队列生产验证（11:18）

- 1bf1098f1e1141dc034f832400e83b158c5251ad / Actions 33940727172 终态 success；独立读回 current=/data/tgyunying/releases/20260905031034_1bf1098f，三组 ai-generation 与 Dispatcher running/healthy。
- 相同候选查询由 60 unknown/0 available 变为 23 pending Job + 37 尚无开放 Job，60/60 available；新生成 Job 持续推进至 ready/reviewing/gateway_bound。滚动30分钟中出现14个发送 remote_message_observed；该窗口前段处于上一版本，后续须按11:12锚点再分任务确认。
- 到期 generation pending 仍1091，另有100个ready pending；队列首部阻塞已消除，不代表积压、内容质量或日目标已完成。

## 内容窗口残留 Release Gate

- 精确链：西安任务 cb862a03 的 coverage a611aa16-f93c-4d85-ae2b-ff3935642b81；旧 Action 3f16b7d0-16b7-4c93-a0fa-76ce2b3f90d0 已 failed/context_freshness_unproven，ExecutionAttempt 总数0、fact0；Job 239d4c43-da5d-4f91-96c5-d5c234e01134 ready/gateway_bound；窗口 ff8b8f49-dccd-4bc5-bcc1-29059cf706e7 仍 gateway_bound。后续6次 generation_contract_error 指纹准确匹配 ai_content_window_concurrent_conflict。
- PRD19.21；仅允许原已终结且零执行证据的内容绑定收口。原 Action/Job/义务不改写；已存在任何 Attempt、fact、错身份或未知继续保护。无 schema、API、前端或目标数量变更。
- 本地反例1 failed/8 passed，修补后新增与原窗口/恢复测试37 passed，隔离 PostgreSQL 2 passed。每批硬超时60秒；PG包括真实 FOR UPDATE 关联查询以及存在 before_call Attempt 时拒绝回收。
- code_review：SQL关联 tenant/task/epoch/义务/Job/窗口，并在同事务锁定；只修改窗口状态/claim/version。批量旧恢复逻辑保持原范围；新模块66行，原 runtime 491行，无超长函数。
- release_path：master -> release -> Deploy Production；应用回退可恢复旧行为，原窗口历史不回写。发布后检查新物化是否继续、是否仍产生同指纹失败；历史已经阻断的 coverage 如需恢复，须另外精确 preview/CAS，不批量重试。

## 内容窗口发布与精确恢复预览（11:46）

25a0cef8 / Actions 33941734205 终态成功，独立读回 current=20260905033245_25a0cef8，backend、生成、Planner、Dispatcher healthy。11:12–11:38 的增量事实覆盖全部 9 个活群，选定评论任务 819b4b75 于11:36确认发出一条；仍为原 legacy 路由证据。

coverage a611aa16 的完整 lineage 仅2个 failed Action、ready/failed 两个 Job，无任何 ExecutionAttempt、remote fact、Provider HTTP exchange。精确预览指纹 e355bfe50876088811e94ed18e4fa350d547a1dd18763248de8fb90d2a1a197a。拟在同一事务调用已发布窗口收口服务，并只把该 coverage 从 blocked 改为 ready、清除对应阻断；原数量、计划时间和 Task.next_run_at 不改写。脚本对准确集合、版本和哈希加锁校验，审计并读回；脚本无模型或 Telegram 调用。

西安点赞全部历史 Action/Attempt/fact/Job/ReactionObligation 均0。只读 GetFullChannelRequest 解析到 peer -1001104990279，但真实 available_reactions 为空（NoneType），当前映射 unknown。没有伪造可用 reactions，也未更换目标或发送试探点赞。

历史 coverage 运维应用成功，AuditLog=981647；独立读回 state=ready、原slot invalidated/version5，targeted_at仍04:14:29.017432、next_eligible/next_decision仍null、target1/confirmed0。没有直接生成或发送；尚待自然链路的新事实。

## 频道能力与生成时钟 Release Gate（11:58）

PRD19.22以Telegram官方TDLib对缺省ChatReactions的处理为依据；完整能力缺省映射none、陌生类型仍unknown、非法响应与网络失败显式报错，none显示频道未开放点赞表情。初始4失败/5通过，修补后能力/点赞/监听46通过。

PRD19.23复现来源延期但effective_claim_at旧导致两类候选提前入队；SQL与Job准备点改为三项发送限制的最晚值。初始4失败/3通过，修补后JIT、任务类型隔离、待处理Job、候选公平性34通过，真实PostgreSQL3通过。三组共83个不同用例，无schema/API/前端变更，不改已有Action时刻/数量/unknown，不开启外部频道反应。

接管容量只读检查另见：AccountBehaviorBudgetPolicyRevision当前0行；按现有默认值启用时，每普通号authored_message=10，总行为60。9个活群配置日目标均4200，普通账号并集1633；理论发言上限16330低于总目标37800。此处已请用户选择任务目标与历史账号上限的修订口径，未擅自削减任务数量或提高上限。

## 审核供应商临时配额恢复（12:10）

新失败指纹5d003936对应 provider_route_candidates_empty:group_semantic_review。active审核路由993cee4f revision2仅包含provider5 MiniMax-M3；该供应商11:20因Token Plan相关429被标为异常，之后没有自动健康探测。只读真实文本及结构化探测已成功（3.922秒），数据库未改；原row完整hash为0bd6637a371cb674772ea306928f4b3349adbd0d56b687dfde8b6963ee8cb860，probe结果detail hash=3e2ac0c17f294f82255dc6101b5ff202cef7ff8383783b9d675609bd38cf8ba4。精确预览只把provider5健康状态由异常恢复健康，保留模型、凭据、启用状态、路由与历史Job；加锁比较原hash后使用正式健康结果持久化服务，审计并独立读回。不以重启或时间流逝推断健康。

审计981716已应用，独立读回确认provider5健康，检查时间12:10:58.464710，原审核路由revision2未变；需另查新Job和真实发送事实。

## CI测试时钟修复（12:20）

bcd7e120的Deploy33943380817因两项来源入口测试失败而跳过部署，线上仍25a0cef8。本地复现为2失败/1通过：测试固定快照在9月4日12点、fresh_until在9月5日12点，却读取真实时钟，过期后正确返回source_ingestion_unproven。固定测试内监听、来源摄取和任务日时钟，并显式推进动态来源时刻；新增跨越快照期限后继续阻止生成的用例，生产新鲜度校验不变。来源入口/新鲜度/摄取/分页/JIT定向35项通过（7.81秒，60秒硬超时）。

用户已明确选择保留9个活群各4200条/天目标，并允许按任务目标修订账号发言上限；核算须覆盖固定账号池与共享账号池的叠加需求，不能仅以总目标除以1633设上限。

## 结构化限流修补 Release Gate（12:27）

结构化HTTP请求漏接draft已有的AiProviderRateLimited共享冷却，导致临时Token Plan限制被标记永久异常。PRD19.24补齐合同；原候选Provider显式传入RPC边界，共用现有cooldown/单probe/延期路径，不变更模型、路由或unknown保护。反例2失败/1通过，修补并同步直接调用测试后6个文件51项通过（8.98秒、60秒硬超时），包含本机真实HTTP截止、配额、admission、调用时限与Antigravity schema。无schema/API/前端变更，git diff check通过。

provider5恢复后12:10:58–12:18采样：58个新ready Job，8个活群29条remote_message_observed，三个浏览任务16条view_observed；新generation_contract_error仅5条内容窗口冲突，无新路由空错误。这是legacy自然运行恢复证据，仍不代表统一引擎或完整日目标验收。

## 账号额度接管核算（12:27）

target_account_capacity只读快照hash=2dcfa3c46aafeb1828531f20116c6593372947d47dd953eb83488d9e3b4814df。稳定分母为all1632、成都1631、pool5=304、pool14=536；9任务目标37800，15%正浮动上界43470。逐Task保守均分再按同账号叠加，上界分布37条304号、31条536号、21条791号、18条1号。按用户授权为tenant1初始策略采用authored_message=37，total仍60。当前完整准入统计不能用于缩减分母：天津音乐仅59号通过、477号目标成员未就绪，部分同时有账号/面具问题；需单独诊断，不以额度配置冒充容量或业务完成。

## 发布与账号策略读回（12:38）

8d3e5712的Deploy33944231231已成功，独立docker RELEASE_SHA读回一致；能力解析/JIT修补已随其上线。结构化限流候选0cf56254在Deploy33944918876执行中。

账号策略preview hash=6d5f91e87f2ebd327abd6e823909398edc02e5783ab784340854df3e674d321e，原策略0行、9Task/普通组/稳定账号快照均匹配。首次FOR UPDATE在Tenant处2秒锁等待退出，事务未写数据；改用FOR NO KEY UPDATE避免与普通外键引用锁冲突，重新只读预览hash不变后应用。审计984368，policy7f6fa346-ddfd-45d1-9081-462e4a0343fd revision1 active，effective_from12:38:15.454330；独立读回authored_message37、total60、其余分类/Session/pair-gap/wake值均符合PRD19.25，Task/目标/历史义务未改。策略落库不代表22个存量Task已接管。

## 类型化未执行窗口 Release Gate（12:44）

成都覆盖0b46f312、e760aa74、22ebc69d的旧Action均failed/ready，其Gateway Attempt已终结且有同一attempt_id的safely_not_executed，其他仅skipped_before_gateway。PRD19.26允许该明确未执行子集收口，仍拒绝任意unknown/成功/孤立fact/身份不匹配/未结束Attempt。新evidence模块锁定并核对全部Attempt/fact，原Action/Job/目标/时间不改。反例2失败19通过；修补后窗口/legacy对账/fencing41项通过（8.31秒）及隔离PostgreSQL4项通过（6.05秒），每批硬上限60秒。无schema/API变更；下一轮发布后再做精确覆盖恢复与自然事实验收。

## 窗口修补上线与独立读回（13:16）

6564b5c6043e20d0bd99acb4a85c240426f181c0 / Deploy33945701455终态success；独立读回current=20260905050034_6564b5c6，RELEASE_SHA一致，backend/planner/三组生成/dispatcher健康。成都0b46f312完整lineage为29 Action、2 Attempt、1精确未执行fact；preview403b13e895500567d910f24dd4b8a8165255105deedb2c489ccf82f5887e26fe，审计989661已恢复ready，旧slot invalidated/version5。独立读回target1/confirmed0、原targeted_at04:19:29.117426与空next时刻均保留。尚无新发送事实；其他两项未应用。

西安a611aa16的新Action6732bfbd于11:48再次generation_contract_error，旧slot已invalidated；因此该义务尚未恢复履约，需按新错误继续诊断，不能复用第一次窗口故障结论。

## 入群 RPC 边界 Release Gate（13:24）

PRD19.27。实际入群RPC前的session/连接/授权/解析失败遗漏remote_mutation_started=false，生产同类Action可能进入不必要的unknown。反例9失败2通过，修补后入群、浏览边界、搜索入群及现有成员策略共122项通过（9.14秒，硬60秒超时）。变更方法42个非空行，无schema/API变化；请求后异常仍unknown，历史未知记录不写。回退应用会恢复该证据缺口，不需要回退数据。

天津音乐目标配置当前为5999/5980且已独立解析peer-1002262282745；519个成员进度仍指向旧目标485/peer-1003583171851。最新按账号查询有422个closed_unknown，新群59个可发言账号；500条历史未知样本中包含管理员限流、已提交入群申请与更早的探测失败。不能将这些多阶段/旧epoch历史行一律改为未执行；需分别以当前目标只读证据对账。成员投影目标不一致与存量全引擎接管仍未完成。

## 点赞分配与准入恢复 Release Gate

接管反查复现两个缺口：来源候选使用admissible而非稳定eligible，4个账号全部/部分丢失session后原3个分配变为0/2；相同source/cap复用epoch时始终读取首次准入快照，恢复session仍返回空集合。PRD19.28明确稳定分配与当前准入分开。反例3失败5通过，修补后容量/跨类型来源旅程17通过（5.01秒），参与轮转/相册/点赞表情/Planner并发53通过（17.06秒，包含隔离PostgreSQL），每批硬60秒。首次扩展测试未设TEST_DATABASE_URL，被测试库保护在执行前拒绝（0测试、0迁移），随后显式指定本机55439/tg_yunying_test并通过advisory lock完整执行。

代码审查：Task/source/日上限相同仍复用原epoch及allocation hash；只在物化时经原准入服务生成新的短期观察，旧快照不覆盖；Gateway执行门禁与unknown保护不变。无schema/API、目标数量或生产Task配置修改。该修补是R1接管前的本地缺口修复，尚未证明线上统一点赞履约。

天津音乐对8个最新未知账号进行真实只读权限探测，全部解析到同一peer-1002262282745且can_send=false；零入群/审批/发送操作，未把历史unknown改为成功。13:28读回确认成都0b46f312仍ready、target1/confirmed0且无新Action；西安a611aa16的新错误指纹5d003936精确对应已修补的审核路由为空，原窗口保持invalidated，并非窗口再次占用。

## 配置接管预览与评论数量裁决（13:37）

05f016bf62dc31c35cf8fed4f5dd259cbe5ed8d6 / Deploy33946724141终态success，独立current=20260905052425_05f016bf，容器RELEASE_SHA一致，backend/planner/三组生成/dispatcher健康。点赞修补adc2d6ad在Deploy33947578479，尚待终态和独立读回。

normalized_engagement_cutover_errors只读校验显示8个活群在baseline阶段即拒绝，固定错误为“AI内容路由v2必须启用两阶段生成”：既有v2=true、ai_two_stage_enabled=false。运行时tenant_fallback_flags已按任一flag开启v2质量，不能将此元数据错误误诊为没有执行审核。正在验证显式对齐质量标记后的候选，不删除策略字段或更换模型。

评论候选仍保留旧目标：819b4b75每帖30/日上限10，16c8bbc2每帖100/日上限0。用户明确选择按PRD55%～65%参与并调整目标和日上限，合同见19.29。仍须处理默认business_max80及schema1000上界、稳定分母与共享账号容量；尚未写生产配置。三个显式浏览任务规范化后仍为natural_auto但保留旧daily1000/1111字段，必须确认实际曝光规划是否读取目标，不能仅以字段保留宣布目标守恒。

## 评论稳定数量 Release Gate 与生产容量反查

点赞修补adc2d6ad4cc46ae2c16701e8578b97bd4ebeeea3 / Deploy33947578479终态success；独立current=20260905054407_adc2d6ad，RELEASE_SHA一致，backend/planner/生成/dispatcher健康。仍无Task启用统一引擎，不把该发布计为统一点赞E4。

PRD19.29已明确下一批统一评论按稳定成员冻结比例。真实规划代码的admissible过滤与membership过滤会缩小分母；全部候选短时不可用则不冻结数量。修补仅对带统一日账本的新计划保留稳定集合，将缺少membership事实的原账号义务持久保留，逐个账号取得有效事实后进入准备链；已绑定Action/unknown不更新。无统一日账本的旧计划继续收口。API和表单移除固定1000上界，保留正整数和业务上限。最初测试复现4个有效缺陷反例，另一个为新测试缺少channel的fixture错误，已修正；不能将fixture失败计入产品缺陷。

定向QA：稳定数量/原grounding计划/配置更新44项通过（10.02秒）；进一步讨论组准入、事实合同、dispatch冲突及真实PostgreSQL31项通过（10.81秒，两批有10项重叠），每批硬60秒并显式隔离测试库。前端tsc --noEmit通过。代码审查确认null membership只代表等待，不创建发送Action；恢复前仍从原binding查询有效fact，原source/账号/ordinal不变。无数据库迁移。此切片等待发布，不能替代整个评论接管验收。

13:56:51只读容量：两个评论Task稳定分母均1632/hash798acb5fb48d897a251f6b9d755d9e3c7ebbf2a324ed35564ac49663aea8aa16，每帖比例范围898～1061。819当前业务上限实际50，16c实际80；最近72小时可评论来源分别7个和1个，按自然日峰值为3个和1个。当前共享策略authored_comment10/total60。所有comment用途active Provider route均为空，两个Task的two-stage/v2/grounding均false；接管还需要真实评论质量路由与讨论组事实，不能仅改比例字段宣称完成。未改变生产评论配置、路由、来源或旧义务。

## 评论数量上线与成员查询 Release Gate（14:34）

0747e5a83205507f08d27929b90f87f642019b2a / Deploy33948911285终态success；独立current=20260905061501_0747e5a8，RELEASE_SHA一致，backend/planner/comment-generation/三组活群生成/dispatcher健康。两个评论Task仍legacy，此处仅证明修补已部署。

PRD19.30的批量成员读取保持完整tenant/peer/binding/current谓词，准入Action仍使用原dedupe identity；锁定创建路径不变。100个候选（3条真实成员事实、97个缺失）单项读取100次SELECT，批量1次且结果ID一致；ready/expired/banned/missing分类与unknown隔离均通过。定向49项通过（13.22秒），PostgreSQL7项通过（7.36秒），每批硬60秒。首次联合测试触及60秒终止，之后发现并修正新夹具的Task过期隐式查询和缺display_name，修正后的完整同组通过；不将夹具错误算产品缺陷。新服务无schema/API变化。

补充纠正：8个活群的ai_content_route_v2_enabled原值为字符串true；规范化为Boolean解释了changed key，不是原路由关闭。tenant_fallback_flags的OR只证明静态fallback关闭，不能单独证明所有Job均执行审核。实际审核须读具体Job/Provider事实。

14:13成都0b46f312仍ready，当前Task原选择器在enforce_capacity开/关均能选中账号98；首批20个更早ready coverage尚未轮到该项。其Projection仍open、QuantitySlot仍open且无ContentMixCycleSlot，旧cycle排除的假设已被否定。评论源帖只读远端预览两个目标均TimeoutError、零变更，不能据此声称Telegram无新来源。

## 评论性能上线、质量路由与西安恢复读回

f09b2b25合并另一任务的b88e8024后候选9599fb45f1c529df0232114795dce03086418f67，只有结构索引说明冲突且双方保留，合并定向7项通过（3.73秒）。Deploy33950351075终态success；独立current=20260905064720_9599fb45，backend/planner/三组活群生成/comment-generation/两组dispatcher均running healthy且RELEASE_SHA一致。

PRD19.31的comment路由初始化preview b133a67a6080c4abf1b8ea9d42a0a3da36f75288ec5ea61622fae20df7b760ef，audit1000136。独立读回comment_context_route287b75e3、comment_realize_general2f9fbdbf均revision1/provider8,7,6/hash432bae31；comment_semantic_reviewbe8975eb revision1/provider5/hash8330265c，正式独立审核校验通过。两个Task原epoch/revision/v2false和全部旧Job不变；零Provider/Telegram调用。

西安a611aa16三个Action/三个Job保持终态，无Attempt/fact或未决Provider lineage；最新错误指纹5d003936匹配已恢复的group_semantic_review。preview f72a3356062d3dd7a815d9032aff783ea2bbf2db0170e9f34c40b05b7d8b8e66；第一次apply因部署切换触发SHA保护，在写入前退出。新SHA下重新预览相同指纹，audit1002536已清除该coverage阻断。独立读回ready、target1/confirmed0、原targeted_at04:14:29.017432与空next时刻均不变，原slot仍invalidated/version5；尚无新发送事实。

## 真实源帖与8小时时间偏差（15:00）

账号8对两个评论频道、账号334对Ago的探测均在授权连接阶段10秒超时，零频道读取/零变更；不能当作来源不存在。近期真实可连接的普通账号1408通过同一正式transport完成真实只读采集，Ago用时2.035秒，郑州0.665秒。Ago peer=-1003380751215/linked discussion=-1003913489284；郑州peer=-1001648379400/linked discussion=-1001109329547。后者与活群Task6407d98f同群，后续接管须核对共享Timeline及回复归属。

真实消息133为2026-09-04T17:10:20Z（北京时间9月5日01:10:20），数据库ChannelMessage=9月4日17:10:20、SourceRevision=同值+08:00；5977/5981等另五项同样相差8小时，六个来源当前comment plan均0。Gateway保留UTC，但listener _wall与operations _normalize_snapshot_datetime直接去掉时区；本文此前基于DB自然日分组的来源峰值不能继续作为精确cap依据，需纠正后重算。

PRD19.32先闭合当前权威观测下的精确纠错，不全表加8小时。初次新测试因私有函数名写错在collection退出（夹具/接线错误）；改名后6失败1通过复现真实偏差。修补分离消息持久化模块、规范化同一真实时刻，保留旧SourceRevision并记录time correction。初步回归28项通过（5.49秒）；PostgreSQL首次新fixture漏flush Tenant导致FK失败，修正fixture后UTC会话时区/同瞬间幂等及讨论事实回归3项通过（6.01秒）。扩展来源/grounding回归仍在执行；当前尚未发布本时间修补。

## 来源时间修补 Release Gate

扩展来源/grounding回归76项通过（13.88秒）。随后将operations的单条频道快照持久化分离为operations_channel_snapshot.py，URL生成器显式传入；调度/计数与既有评论快照入口保持原行为，同一规范化函数复用。重跑来源/监听/入口/计划66项通过（14.19秒）；新增完整operations写入口与时区纠正用例12项通过（4.25秒）。各测试进程硬60秒，PG仅127.0.0.1:55439/tg_yunying_test/advisory lock。

代码自审：current权威snapshot的raw datetime与原存储值精确匹配才允许时区纠正，并核对原source revision时间；非已知偏差拒绝。append-only revision采用显式aware时刻，metadata保留原值/新值/原revision/转换版本；相同UTC/BJ时刻幂等，真实edit仍走原invalidate入口，纯时间纠正不伪报edit。原Action/unknown payload和scheduled_at读回一致；未写任何旧Plan、Job、Attempt、fact或截止字段。新消息持久化模块约234行，listener模块降为316行；无schema/API/前端状态变更。回退程序不能假设已纠正当前时间投影仍是旧UTC墙钟，若回退需保留本规范化兼容修补，不能将历史时间统一减8小时。

来源诊断另复现2失败2通过：同一时刻UTC/BJ字符串不同误报listener_lag，UTC租约被当作过期而可能发起诊断RPC。按19.32统一ISO输出和租约比较后，诊断/时间写入口16项通过（4.56秒），无数据库写入或新采集。

时间修补bd2e91b5与诊断修补f31c7445已合并远端8b73b286，合并提交22405ffd无冲突。合并后来源诊断、时间持久化与并行任务的post-login exception共26项通过（8.43秒，硬60秒）；工作区干净，git diff --check通过。候选包含双方原提交；等待本次正常发布及独立生产读回，尚不声明来源时间已在线修正。

## 频道观察候选截断根因与 Release Gate

15:35独立只读：生产仍8b73b286，Ago listener账号949/snapshot135为ready且15:34更新；819 listener账号31/snapshot0为unavailable/TelethonOperationTimeout。15:40候选反查：819当前31个候选全部unavailable，同一Task原选择器全范围有1325个当前合格账号，其中1292个未探测；已实测可读的1408在范围内却不在候选集。原10个fallback候选先按健康/ID截断，再在这固定候选中按listener状态轮转，无法到达其他合法账号。

PRD19.33。反例4失败1通过（3.32秒）：批次外未探测、ready、最旧失败均被排除，runtime _wall另有去时区问题。修补将同tenant/channel/account观察状态排序表达式传入原候选SQL，在LIMIT之前生效，仍保留原Task范围/健康/冷却/近期目标事实。27项初步回归通过（6.15秒）；扩大至账号池与来源分页63项通过（10.92秒），45与1045账号的SELECT数一致；真实隔离PostgreSQL的UTC会话排序与来源时刻修补2项通过（6.01秒）。所有测试硬60秒，PG显式55439/tg_yunying_test并使用advisory lock。

15:46生产只读对比在独立诊断进程内载入候选函数，不改变服务进程或数据库：原选择41，候选选择此前未探测的17；两侧均12次SELECT。两次原耗时0.3092/0.4192秒，候选0.2049/0.1860秒，仅是该样本，不视为全链P95。零Provider/Telegram调用、零数据变更。代码审查及compileall/diff check通过；账号选择函数49非空行、账号池文件495行。无schema/API/Task目标变化，等待发布与正式采集证据。

## 来源时间上线与真实采集读回

cfc563363818e7f16cce01593214ec68f6fe7621 / Deploy33952867854终态success。独立current=20260905074149_cfc56336，backend/planner/三组生成/comment-generation/双dispatcher/listener/recovery均running healthy且RELEASE_SHA一致，容器API健康status=ok。15:57读回：正常listener在15:50:58将Ago133/134当前发布时间纠正为9月5日01:10:20/21，并分别追加bd1bc0b6/0e538a9c的timestamp_corrected来源；current采集账号958/snapshot14为ready。819仍unavailable，其四个样本仍旧时间；候选排序47894f88尚未发布。该证据证明Ago时间纠正，不是统一评论发送E4。

## 存量 Action 合同 Release Gate

PRD19.34。3项真实反例均失败（2.83秒）：当前Task切成unified会把绑定前Action和同旧义务的新Action重解释；反向改回legacy又会跳过新统一Action资源门禁。修补只读首次正式binding边界和四类原数量owner/父计划时间，校验tenant/task/epoch/account，不写任何旧业务记录。初步19项通过（5.52秒），扩展47项通过（8.75秒）。

最终单元组50项通过（9.83秒），独立PostgreSQL UTC会话/等于生效边界/标记回退/unknown不动1项通过（5.75秒），每个进程硬60秒。中间同进程混跑结果50通过1失败（13.55秒），失败为既有recovery的caplog日志断言：PG迁移初始化调用fileConfig影响已有logger；按CI的no-postgres/PG分进程方式重跑后两组通过。未改恢复代码或放宽日志断言。compileall/diff check及代码自审通过；无schema/API/配置修改。完整R1仍需要旧来源入口、生成配置归属和跨legacy行为额度，不能单靠本切片开启全部Task。

## 发布回归中的连续容量周期断言修补（16:34）

6f79e32e / Deploy33954574213终态failure，唯一失败为no-postgres分片1的评论日容量测试（1860通过、1失败、388.59秒）；其余前后端和两组PostgreSQL检查通过，build/deploy均未执行。错误断言要求2条预约必须恰有2个容量周期。独立UTC环境真实规划复现：预约分别排在2030-08-02 19:26:00和08-04 00:02:26，连续容量日历必须保留中间未使用的08-03，故3个周期正确，未发生超发。不同随机业务身份改变自然排期，单次通过不能排除此缺陷。

修正测试为2条Action/义务/预约、2个实际占用周期、每条预约处于所属周期且额度为1、全部日历周期首尾连续；补充跨空闲日的确定性容量日历回归。未改变生产代码、自然排期、额度或已有PRD合同。完整评论合同与新回归29项通过（7.90秒，UTC，硬60秒），diff check通过。诊断用临时测试已删除，复现证据保留于本地临时审计目录。

## 705fe390 上线与存量接管反查

Deploy33955707992终态success；17:03独立current=20260905084503_705fe390，backend、planner、三组AI生成、评论生成、双dispatcher、listener、recovery均healthy且SHA一致，宿主发布端口API健康ok。此前4核/7.5GB主机曾仅330MB可用、load39/44、I/O wait65%～86%；发布后采样load3.28/5.77、可用570MB。磁盘仅使用50%，本轮没有清理镜像、停止代理或改变进程并发；资源改善是发布后采样，未据此宣称内存根因已修复。

16:46旧合同当天类型化成功事实：活群4468、评论37、点赞32、浏览2282。16:53反查两个评论Task各4个open无Action且无grounding Plan的旧义务；点赞当前epoch共164个open无Action；当天浏览共9799个open无Action；9个活群仍各自持有4200的legacy日目标，未绑定新参与计划。它们不能因批量切flag被改算或丢失。完整未决查询还发现closed_unknown甚至skipped的旧Action仍对应result_unknown，不能只按Action状态排除未知占用。17:10另一已授权发布将运行SHA推进至3027f1e0；此为独立ABC post-login修补，当前分支接入前核对并保留该提交。

## 利用当前可读性选择频道观察者的 Release Gate

705正常collector已使819从原首批轮换到账号50，但16:58仍TelethonOperationTimeout，来源时间尚未纠正；Ago958仍有正常采集事实且旧时间纠正revision未重写。补充PRD19.33：本目标未探测候选内部优先使用同tenant、原账号范围内其他频道当前ready/fresh的观察者；不改变本目标已有失败/等待，不借用其他Task范围，不将另一频道可读视为本频道采集成功。

真实反例2失败10通过（4.45秒），分别覆盖SQL候选截断和最终候选合并。修补后21项通过（5.54秒），扩展监听、账号池、来源时间共77项通过（12.53秒）；独立PostgreSQL UTC会话的本频道ready、其他频道fresh与已过期3项通过（5.81秒）。各进程硬60秒。SQL数量随45/1045账号保持一致，compileall、diff check和代码自审通过；无schema/API或生产任务配置修改。

17:10生产repeatable-read只读对比（运行3027f1e0，候选函数仅载入独立诊断进程）：原逻辑选择89，新逻辑选择当前已验证可读的940，958/1103/1402也进入原Task范围的候选。两侧均12次SELECT；原0.1584/0.1319秒，新0.1533/0.1828秒，仅为两次样本，不是全链P95。零数据库写入、零Provider/Telegram调用。等待正常服务发布和真实来源采集读回，R1仍未启用。

## 观察账号修补上线与正常来源采集

9e3b2d01合并并行ABC修补3027f1e0为6c59cab99b142b5b4da8da4905c91819cb0b9c5e，合并QA21项通过（6.93秒）。Deploy33957633761终态success；17:41独立current=20260905092725_6c59cab9，10个核心容器均running healthy、SHA一致，宿主API健康ok。主机load4.79/4.46、可用495MB，仍只作为该时刻资源采样。

正常listener在17:30:19已更新819来源5977/5981/5984的真实发布时间投影；5977和5984追加timestamp_corrected，5981有真实edit因此按edited追加。17:41当前观察者437为ready/snapshot18，Ago966为ready/snapshot15；两者均持续有新观察时刻。Ago先前两条时间纠正revision及历史revision不变，819的5973样本尚未纠正。这个结果证明来源采集恢复，不是统一评论发送验收。unified Task和binding仍均为0。

## 已开始生成的政策归属 Release Gate

PRD19.35。原实现以Task当前config revision读取政策后覆写已绑定旧window的Job hash，违反new_preparation范围。初次10条失败中存在政策active唯一约束和来源信息不足的测试夹具问题，先修正夹具；再在独立进程载入未修改HEAD原实现，10条业务反例全部失败（1.72秒），覆盖group/comment、当前binding缺失、混合批次、上下文替换和原scope/hash不一致。

修补批量读取原window/binding/policy，已开始Job保留原preparation政策、route和config revision；新Job读取当前修订。强引用原slot避免SQLAlchemy弱引用回收后逐Job重新查询，2与25条批次均仅1次window与1次policy读取。原16项binding测试与新增12项合计28项通过（6.58秒）；扩大生成及Provider unknown回归118项通过（14.20秒）。独立PostgreSQL原revision读取、仅两次SELECT和unknown/gateway_bound不改写1项通过（5.41秒）。每个测试进程硬60秒，隔离55439/tg_yunying_test/advisory lock；代码函数及文件限界、compile和diff check纳入审查。

17:49只读全量历史诊断中，正在generating的5个与pending中17个已有窗口Job均引用一致；ready历史中2062个、unknown中45个存在旧引用不一致，不自动覆盖。17:59进一步限定为当前pending/generating或被未完成Action引用的483个已有窗口Job，在独立只读诊断进程调用候选政策读取：9个活群全部matched，逐Task查询耗时0.0112～0.0923秒。零数据库写入、Provider或Telegram调用。该预检不替代上线或真实生成结果；legacy无窗口Job、旧来源入口与共享行为预算仍属于完整R1的剩余边界。

## 73388cd1 发布与生成后真实消息

73388cd156ff4f09e6e330ee539b1c30f05a609d已推送master/release；Deploy33959643795终态success，全部前后端分片、PostgreSQL与镜像构建通过。首次部署读回时current仍指向6c59、两个dispatcher为starting，未据此称部署完成；随后独立current=20260905101250_73388cd1，10个核心容器healthy/SHA一致、APIok。移除无调用方的可选config revision参数后，最终28项定向回归6.21秒通过，compile与diff check通过。

以18:20:40为新版全部worker验证后的采样起点，18:23:21只读观察到8个活群20条remote_message_observed，以及3个浏览任务9条view_observed；safely_not_executed另列，不当成功。其中新Job65d0a365和660e700a均在采样起点之后创建，分别经Attemptfd631a39/b727ef7f得到Telegram消息3015462/3015455；Job、原window plan和政策binding的hash一致。此证据证明新版真实生成发送链继续运行，不证明配置修订后的生产接管或22个Task已统一履约。

## 剩余已证明未执行窗口恢复与原任务日反查

西安a611aa16已于15:16:51通过原Action5a373a70、Attemptfb23a9f0和remote_message_observed获得消息349642；18:10独立覆盖读回confirmed1/target1，原targeted_at04:14:29不变。这是该精确恢复项的远端履约证明，不扩展为全西安日目标完成。

成都e760aa74与22ebc69d分别完整核对21/35条Action和Job、1/3条Attempt及各1条safely_not_executed事实；所有旧Action终态、无Provider未决。preview分别9f772dee9903321f89b7833e810acc2b54db39e3605c9c2fb5acb678392c0a0b和e1e9e79b3d5b45f968c326cc8ab0f243a502d3062ada9727612c788e31912232。旧SHA apply在写前被deployed_sha_changed保护拒绝，零写；待73388正常发布完成后重新预览同指纹，分别audit1018182/1018186精确恢复。18:26独立读回两条coverage ready、target1/confirmed0、原targeted_at及空next时刻不变，原slot invalidated/version5/claim空；全部旧Action/Job/Attempt/fact逐行hash与preview一致。仍需新的真实消息事实，禁止重复apply。此前成都0b46已在18:06形成新ready内容Action32380c61，18:10等待原pacing_source_not_before，仍无成功事实。

18:30原任务日只读反查（legacy_budget_day_inventory_733.jsonl）：今天观察到的37条评论成功中29条的原pacing日为9月4日、8条为9月5日；32条点赞成功的原pacing日分布在8月21日至9月4日；浏览2479条成功中1条归9月4日、2478条归9月5日；活群5007条成功均归9月5日。所查询今天事实均有TaskDayLedger或原pacing_due_at，不需要用实际call日期重写原任务日。后续共享行为预算必须区别原履约归属与真实调用占用，不能把今天观察到的成功全部当今天数量完成，也不能把unknown与unknown_deadline_closed两种事实双计为两次行为。目前仍未写legacy预算桥接、Task配置successor或全量统一接管。


### 19:26 R1 存量占用预览与账号索引 Release Gate

- Intake沿用本审计L3；用户关于819入口、活群原目标和评论55%～65%的裁决不变。PRD19.36已补产品交接：显式账号清单、原任务日/真实调用日分列、原Attempt去重、已预算占位排重、Gateway证据优先级、unknown与transport ACK分离、缺失证据显式issue。仅闭合占用预览，正式配置与旧资源准入仍未接管。
- 实现 `engagement_legacy_occupancy.py` 和 `preview_engagement_legacy_occupancy.py`，只读事务、不flush调用方待写状态、不读取消息/错误正文。审查将journal查询合入一次SELECT，避免逐Attempt查询和大IN参数表；多journal仍只投影一个Attempt。早期非终态snapshot false不能释放在途，conflict journal不能作为确定未执行；缺原履约日不撤销独立transport ACK。
- 19:06/73388只读初版预览：1633普通号，10932原Attempt，403项original_task_day_unproven；单账号2SELECT/4.9435秒、整批2SELECT/9.275秒。这是占用与证据清单，不是10932次成功，2782物理占用待核对也不表示已证明仍有2782个远端请求活着。Artifact `legacy_occupancy_candidate_probe_733_retry.jsonl`。后续已改为一次SELECT，最终性能需上线后另测。
- 轻量只读EXPLAIN显示单账号仍走 `ix_execution_attempts_unfinished` 的BitmapOr，估算候选122146，再过滤账号。新增0224部分索引 `ix_execution_attempts_account_usage(tenant_id,account_id,gateway_call_started_at)`，覆盖Gateway已进入或success/result_unknown；PostgreSQL CONCURRENTLY创建，重试只重建本命名invalid残留，降级只移除此索引。ORM索引归共享资源metadata；bootstrap副本排除提前创建。七处head断言同步0224，无新业务表、数据回填、Task配置或外发。
- QA：相关单元/迁移/归属/资源回归56 passed 12.68s；最终transport与journal审查后26 passed 5.94s。独立PG时区/0224合法索引/0196存量升级6 passed16.52s；受影响PG组合14通过、1项旧0192逆迁移因前置Provider测试保留的无历史binding而被0219保护拒绝，未放宽保护；该逆迁移独立新测试进程1 passed6.61s。初次从仓库根运行的1个测试因相对alembic.ini路径失败，按CI backend工作目录重跑通过。所有进程硬60秒。compile/diff检查、新文件500行及函数50行检查通过。
- 线上访问：一次应用诊断在SQL前遭容器DNS临时失败；宿主DNS0.16秒成功，后续真实SQL已成功。宿主可用内存345400kB、load39.97，19:04 vmstat I/O wait51%～55%；19:13 backend只有uvicorn，无遗留诊断Python进程。重型EXPLAIN子进程一次120秒超时，无SQL成功证据；改用带远端30秒alarm、标记argv且只加载psycopg的只读探针后EXPLAIN成功0.008秒。没有修改DNS、重启/关闭服务或用这些访问现象推断数据库整体故障；整机内存压力仍未修复。
- 发布候选待本节提交后形成；先检查原master/release仍73388、当前无进行中Deploy，再按master→release→Deploy Production发布。业务状态保持unproven；本索引/预览发布不能替代22个legacy任务正式接管或四类型E4。

### 21:08 发布失败定位与评论、去重内存修补 Release Gate

9e335fb79f3b5a488ef03f5fa0d3d6a36cf131cc对应Deploy33963567986已于20:10:47终态failure。全部CI分片和三镜像成功；生产日志两次在共享backend pull发生TLS handshake timeout，最后一次登录也报相同错误，未出现Stage A fence或migration。第二轮Docker统计/全局清理从19:48:15持续至20:06:47才开始pull，形成18分32秒额外前置耗时。不能把此次失败记为0224迁移失败或声称新索引已经上线。

20:30只读host进程与Docker磁盘metadata独立确认current仍20260905101250_73388cd1，发布进程为0；backend和18个worker的release SHA均73388、对应PID存在。这一层不是fresh health或E4。主机可用内存282692kB、load约47～52，磁盘18.34GB可用；20:33dockerd PID1084/start_ticks1221为RSS22380kB/Swap132440kB，本地/_ping曾5秒超时。正常发布脚本之外未执行全局清理、停止其他项目或更改DNS。

按Docker官方诊断接口，对上述精确PID/start_ticks复核后发送USR1，仅生成堆栈。首次Python探针外层超时无回执；后续轻量只读未见stack文件，不能算执行成功。最终shell内建操作取得明确preview/sent回执，20:51:17生成687035字节堆栈；20:59读回499个goroutine。只输出方法名与等待状态，不输出调用参数。此堆栈尚未证明daemon死锁；同次Docker/_ping已返回200/2.284秒，仅证明该采样可响应，未重启daemon。最初host健康脚本使用Python3.7的capture_output参数，被主机Python3.6拒绝；已修正临时探针，不能把这个本地工具兼容错误算成服务不健康。

本批按既有L3 Intake分为三个明确根因：

- 评论合法配置被拒：PRD19.37先补交接；Schema/运行校验对两类关闭、两权重0、计划预算0仍强制兜底，原实现3条反例失败、10条历史保护通过。共用纯配置规则并同步前端，完整关闭的新准备不创建兜底Policy/Pool/cursor或读素材；先保留原Pool/Policy归属，旧revision缺Policy仍报错，unknown不变。
- 去重结构长期常驻：PRD19.38先补交接。原65536项LRU在12000条不同48字中文实验中保留127.53MiB，首次/重复扫描1.0185/0.2814秒；原实现对象释放、候选重复准备两个反例失败，43条判定测试通过。修补为每次扫描只保留一次候选结构，历史逐项释放；最终实现同数据两次0.8790/0.8914秒，保留0.0030/0.0031MiB、峰值0.0292/0.0326MiB，命中结果一致。这是本地实验，尚未证明生产各worker真实RSS降幅；明确保留重复扫描CPU取舍，未减少历史/阈值或数据库事实。
- 发布前置全局清理：发布专项PRD§9闭合，删除compose-up的system df与自动container/builder/image prune。磁盘不足仍由真实pull错误暴露，后续精确清理独立处理；不延长超时、不重放业务、不跳过fence/迁移/验收。真实shell使用隔离Docker边界替身验证backend/frontend pull任一失败均原错误非零退出，未调用清理或worker stop。

QA：评论、生成、数量与发布边界105 passed/9.67s；去重数学判定、对象释放、完整扫描、归一化、批次、账号/tenant/查询范围103 passed/7.06s；独立PostgreSQL原窗口及未知记录保护2 passed/9.59s。内存修补后再次执行关联生成/评论路径73 passed/5.70s。各后端进程硬60秒，隔离55439/tg_yunying_test与advisory lock。前端完整build通过，实际表单validator的明确0、残留预算/权重、legacy emergency与缺值5例通过；语法、compile、diff检查通过。

正式发布前重新fetch：HEAD/master/release仍9e335fb7，工作区只有本批明确路径，无正在运行的Deploy。新候选将包含尚未上线的0224索引/占用读取和本批三项修补，一次冻结后正常master→release→Deploy Production。正式Task接管、共享legacy预算、配置successor、评论目标/cap/grounding仍未apply，四类型统一E4保持unproven。发布后必须分别验证current/SHA/health、0224有效索引与实际查询耗时、worker RSS/主机响应以及真实生成发送链。

### 21:29 d6 候选完整CI的合同测试遗漏修正

d6d637aa51251ca4040ebcb82b757e2157fd647d已按master/release冻结并触发Deploy33968252586。候选、前端、两个PostgreSQL分片和两个no-PostgreSQL分片通过；no-PostgreSQL shard1为1 failed/1901 passed，唯一失败是原`test_deploy_prunes_only_dangling_images_before_pull`仍要求脚本包含`docker image prune -f`。本批删除自动清理时漏同步了这条旧合同测试；未恢复已取消的全局清理，也未跳过该门槛。

按已经通过设计自检的发布PRD§9，将原测试更新为正常发布不包含全局system df、container/builder/image prune。继续验证完整CI依赖、三镜像、先pull后fence、OCR重启策略收口与运行身份，以及真实shell在backend/frontend拉取错误时非零退出、不进入清理或stop。三份部署定向测试14 passed/4.18s（硬60秒）。该修正仅测试与本审计，应用/迁移实现不变。33968252586终态failure且images/deploy均skipped；d6和0224仍未有生产上线证据，须形成新的不可变候选再完成全套CI与部署。


### 22:12 921 发布终态与主机压力证据

92109309ce2311fc7744370ceb3eecfecc5d71aa / Deploy33969123685：完整CI和三镜像通过，部署于21:59:45终态failure。三次都在docker login ghcr.io发生TLS handshake timeout，未出现pull、Stage A fence或migration；本次已经不再运行全局Docker清理。21:58:41 current仍73388，19容器磁盘metadata均旧SHA，authorization-abc-sweep的PID已不存在，其余18个PID存在；这是磁盘metadata层，不能称全容器healthy。22:01发布进程快照为空。22:03附近独立runtime读取current仍73388、APIok，Dockerinspect未在探针总时间内返回，仍未验证新版本。

22:10修正探针读取了实际/proc数据：总内存7653896kB，可用256248kB，Swap占用约581MiB，load57.39/60.21/62.28；2.003秒CPU计数中I/O等待698/799约87.4%，pgmajfault1503，pswpin1381页、pswpout2779页。三个AI-generation RSS分别424588/409828/423684kB，两个dispatcher为499596/485120kB。最初探针假定旧内核存在allocstall总字段导致KeyError，已改为读取实际allocstall分项；没有据失败探针推断零压力。继续区分GHCR网络和资源拥塞，未重启daemon、手工停止业务worker、重复dispatch或改Task。


22:20:55完成一次有保护的运行调度修正：vm.swappiness 0→60，boot04062f92/current733/SwapTotal4194300kB匹配；ops审计`/data/tgyunying/shared/logs/ops-swappiness-20260905-1417.jsonl`先记录preview再applied，/etc/sysctl.conf SHA64708410…及其他三个VM参数未变。22:21:53独立读回运行60、持久仍0；暂未持久化。此时短时I/O wait仍约75%，22:26APIok但Dockerinspect超时，未称恢复。

22:28反查发现已有Docker memory cgroup保留0：生成2为容器6a88752d…/PID2617817/start23828998，dispatcher1为67e59812…/PID2618837，主机60不会覆盖它们。生成2HostConfig未显式设置MemorySwappiness，三个memory限额均9223372036854771712；可证明原容器创建时的继承口径与新的主机运行值分离。PRD19.39补resync，准备只对生成2原cgroup将0→60，通过boot/containerID/name/release/PIDstart/cgroup/HostConfighash/原值/limits完整比较后执行，并独立审计读回。不通过重启、手工修改Docker磁盘metadata、改内存硬限或修改其他项目容器达成。


### 容器回收设置修正及正式配置 Release Gate

22:30:21生成2精确cgroup 0→60成功，独立读回PID/start/release/HostConfighash/三限额不变，审计`ops-generation-cgroup-swap-20260905-1428.jsonl`。22:33:28负载35.24/50.07/57.33、可用332008kB，生成2开始换出匿名页但PID不变；22:34:46 Docker一次list在5.073秒成功，该副本healthy、APIok；两个dispatcher已可明确观察为unhealthy。

22:37:23精确预览剩余18个本项目容器，均为原733 SHA和cgroup0；22:40:17～19逐容器CAS纠正为60，审计`ops-runtime-cgroup-swap-20260905-1437.jsonl`，18/18取得applied回执。22:41:43独立读回18/18全部精确匹配（仅swappiness不同）；当时load16.75/25.04/41.32，可用520716kB，2秒I/O等待122/795约15.3%，page-in12772kB、swap-out0。相较前述87.4%仅描述两个真实采样，不作为全天P95。

23:37:56最新完整Dockerinspect已正常返回：current和十核心容器仍733，APIok，八个healthy而两个dispatcher unhealthy，后者仍需独立定位。主机load5.75/3.65/4.46、可用743396kB，swap约1.59GiB；原容器StartedAt不变。控制面和资源响应有改善，但两个dispatcher及业务事实未验收，不能称production_fixed或统一接管。

本批正式代码仅给Compose中共用backend镜像的19个服务显式添加mem_swappiness=60，通过命名标量统一维护，避免重建继承旧0。新旧YAML结构比较证明恰好19个字段增加、其余映射完全一致；22个发布/拉取失败/fence/OCR gate测试4.00秒通过，后端硬60秒。PRD19.39、运行文档与结构索引同步；无新数据库迁移或Task配置。候选继承921中尚未上线的0224索引、评论零兜底和去重内存修补。master/release远端均921，三个近期Deploy均terminal failure，无活跃发布；待形成新不可变候选走全套CI。


### 2026-09-06 00:33 调度回收与日界缺口修补 Release Gate

16d4f2ed / Deploy33975796261已终态failure：唯一失败PG shard0的test_view_planner_does_not_append_after_latest_future_action，1 failed/358 passed/7 skipped/1 xfailed；其余CI通过，镜像和部署未执行。生产仍733。23:51:58两dispatcher均因RSS停止主循环；副本1已drain_blocked等待另一个的rolling租约，副本2draining且local reservation计数6，Gateway open，active operations/owned Action/unfinished Attempt均0。后续只读快照仍为原instance和原6项，heartbeat时间已滞后，不能把查询成功写成worker健康。没有清理计数、未知结果或直接重启进程。

按既有L3 Intake/Root Cause Grouping更新PRD19.40—19.42及Dispatcher专项5.1/5.5后进入dev：

- 自动回收先争租约再停止claim；竞争失败保留active和下批工作，胜者将原租约带入drain，续租失效继续阻塞，竞争期间SIGTERM保留人工停止。原实现5项反例全部失败；修补后worker loop/heartbeat/role/lifecycle共52 passed/6.52s。Redis租约实现按职责从491行生命周期模块移至独立文件，键/token/TTL/renew/release/successor协议不变。
- 日界是业务缺口而非只需固定测试时间：真实curve在23:50排出23:50与次日00:00，legacy fit误将等于deadline视为合法，后续半开预约筛掉第二项。固定12:50通过、23:50原测试失败；修补相等判断后6个真实PG planner/quiet-hour用例23.31s通过，保留原2目标和严格日界，不移动冻结排期。
- claim之后数据库异常、读取到不存在/终态Action、finalization抛错会留下本地登记，6个异常反例在原实现失败。批次作用域从claim前登记新对象，在所有已启动future返回后finally精确释放仍属于本批次的对象；不清其他线程/批次或同id successor，不改durable业务记录。36个异常/claim/Gateway原子性用例6.16s通过；合并租约丢失、角色drain、节奏与termination回归100 passed/8 deselected/9.08s。额外真实数据库unknown保护先纠正测试中fence状态名unknown为实际remote_unknown，最终34个runtime/termination回归6.08s通过：本地登记已释放，durable unknown预算、remote_unknown lease/fence与cancellation_unconfirmed均保留。

所有后端进程硬60秒；PG只使用55439/tg_yunying_test且有advisory lock。新增/拆分模块与测试均小于500行、函数不超过50非空行，diff检查通过。结构和数据流索引同步。新候选将继承16d及其祖先尚未上线的内存/Compose/0224索引/零兜底修补。最新fetch证明master/release均16d，五次近期Deploy均终态，无活跃发布；冻结候选后按master→release→完整Actions检查发布。不得把本地清理或排期测试说成线上6项已解决、dispatcher已健康或统一接管已完成。

发布验收继续独立检查current/全部worker SHA与health、0224索引有效性及读查询耗时、swap/RSS/控制面耗时、新dispatcher本地心跳/生命周期/新Attempt和各类型远端事实；未知历史保留。R1共享legacy资源、配置successor和四类统一Task接管仍未实现/未apply，完整目标保持unproven。


### 00:42 并行目标提交 resync 与最终验收输入

冻结前发现主干已由其他工作推进到2079dead088d92c98e17315476e6986aa575042e，release仍16d。该提交只新增2000目标脚本及诊断workflow的无条件执行；已独立读代码和run33976702434回执，本任务用户随后明确“每群每天2000条”，PRD19.43据此覆盖后续4200验收输入。保留该并行提交和已经修改的正确配置，只移除诊断入口的无条件再执行；workflow字节级恢复16d版，YAML解析和真实shell bash -n通过。未修改一次性脚本、未重跑生产目标更新。

00:40:19 DB只读RR独立确认：九群Task两处配置均2000，2026-09-06九条daily target的configured/effective/planned均2000；2026-09-05九条仍4200、冻结人数和原原因保持历史值，没有误改前一天。target脚本新增的target_update_bound政策binding计数为0。九群仍fact_first_v3、无engagement_contract_version，2000持久化不是统一引擎接管或当日履约。最后补齐来源筛选时钟后原浏览PG文件5 passed/20.59s，避免今后真实日期漂移污染固定日界回归。

最终候选以2079为父提交，包含本批明确路径及诊断入口修正；此前Release Gate的“master/release均16d”仅表示发现并行提交前的快照。所有运行中的本地测试已结束、工作区其余用户文件保持不动；发布必须以新commit和新的完整CI run为证据。

### 2026-09-06 01:24 发布终态及R1/Provider修补进展

- Deploy33978775792（14d0941493c45d1b3ada72a954564df19dcf9fce）已终态failure；全部CI和3镜像通过。三次安装分别已激活dispatch-rebuild-v3，末尾Antigravity双模型探测均在gemini-3.6-flash-medium返回HTTP202 unknown/antigravity_quota_limited，并记录slot rollback complete。外层把该错误按SSH重试完整安装三次，引发两轮额外stop/fence；没有把unknown回放为业务成功。原log在本次/tmp审计目录的deploy_33978775792_failed.log。
- 01:09:48独立读回current=/data/tgyunying/releases/20260905165256_14d09414；19/19 backend-image容器SHA一致、running/healthy、HostConfig.MemorySwappiness=60，APIok，available1042600kB，swap占用402944kB，load1.98/2.91/3.64。01:13 DB的dispatch scope已active。不能把这些证据改称整条发布成功；Antigravity回滚后的独立runtime SHA/模型可用性仍待验证。
- 01:02索引0224已真实安装且valid。Sep5同口径单账号只读占用查询1SELECT/0.0341s/9行；1633账号1SELECT/16.415s/11041行，仍403缺原日期、2782物理占用未证明。单号EXPLAIN实际采用新索引，全量耗时未改善；未将unknown或缺日期行改写，也未把该只读查询称正式共享准入。
- 00:59:52到01:04:54期间没有新四类typed业务事实；该窗口处于安装重试和fence之中，需以最后一次稳定runtime后的新anchor重验。01:13读取01:05后Provider记录：3.6有50条provider_result_unknown、474条probe_in_flight，健康仍是9月3日旧值；MiniMax-M2.5有10次生成success。这些是Provider结果，不是Telegram完成。
- R1资源身份子项按统一PRD19.44完成本地实现：显式legacy_cutover首binding快照、原日期及移组归属，新旧动作共用完整lease/reservation/fence；历史Attempt不回填，缺原证据明确失败。12反例原实现全部失败；修补56关联回归、增加原Attempt/跨epoch/first-binding successor后68回归通过，独立PG2项通过7.82s。容量函数10项AST逐项等价移出超长模块；新/改模块均500行以内。尚未创建生产cutover快照或激活统一Task，历史调用与实际调用日共享占用、配置/来源接管仍待完成。
- 发布脚本单次安装修订见发布PRD§10：真实release.sh边界替身复现业务1/SSH255/等待124均重复安装3次；修补后与SSH/Antigravity/release gate共30项通过14.62s。保留连接前置及上传重试，保留模型检查失败，不吞错。
- Provider quota修订见Antigravity专项§18和统一PRD19.45：HTTP202保留typed quota code，draft/structured独立记录该Provider不可用，当前unknown仍不继续候选，后续独立工作只用原已配健康路由。原6反例失败/2普通unknown对照通过；修补39项通过3.68s。追加健康提交失败必须保留原unknown异常链的两反例后，quota、unknown传播、原Provider lineage和HTTP exchange共62项通过19.55s。本段代码后来提交为f58f4f21109f3750d6564ea910ac04a322770a2e，尚未push或部署为14d的一部分。

### 01:30 稳定窗口及共享周额度核实

- 01:24:41独立runtime再次确认19/19 healthy、current/SHA仍14d，APIok、available988524kB、swap占用626548kB、load3.66/2.51/2.91。两个dispatcher分别RSS264548/266200kB，未发生新OOM或容器restart。此样本距其最近一次启动约21分钟，仍不是全天容量验收。
- 以01:09:48为稳定锚点，01:24:41取得2条view_observed，活群有43条remote_outcome_unknown和9条safely_not_executed。01:27按具体action_type重新核对：活群未知均为ensure_target_membership，另有invite_group_account失败；不能把这些计入send_message未知或成功。新GenerationJob到消息的成功链仍为空。两次探针先遇到不存在的Action.reason_code列，修正为实际status/action_type后只读成功；该工具查询错误不作为业务故障。
- 生产slot-01服务独立读回active，bridge current仍73388，确认此前release末尾执行的是bridge回滚、应用14d并未回滚。用服务原OS用户和当前root-owned bridge目录启动原生CLI 1.1.22，/usage读到Gemini Flash/Pro共用周额度剩余0%，约120h53m后刷新，5小时窗口因周额度耗尽而disabled。未调用模型、未改登录账号或请求账本；初次在/tmp遇到trust提示后退出，最终只信任现有bridge代码目录。
- 现有MiniMax-M3独立结构化连通性调用通过：2.216秒、266 tokens、指定JSON字段匹配、DB写入0、Telegram调用0。该结果仅证明基础调用可用，不代表八条生成路由的业务质量或四类Telegram履约已通过。临时将六条活群生成路由与两条评论生成路由的后续新工作改为该模型已向用户征求选择；未修改路由或旧unknown。其余既定修补不依赖该选择。

### 2026-09-06 02:05 M3路由及九群原绑定恢复

- 用户已明确选择“改用 MiniMax M3”。01:37:00.370361按预览aa9c32c0…完成8条生成purpose唯一M3、2条独立审核purpose唯一M2.5的新revision，审计1027918。M2.5真实结构化探针成功后恢复健康；Gemini7/8按原生CLI共享周额度0的事实标异常。01:40:16独立读回新10路由正确、旧10 route/item保持精确原值（仅route retired）、抽样5旧unknown整行hash不变。详细回执：minimax_m3_cutover_apply.json / minimax_m3_cutover_readback_utc.json。
- 切换后M3规划、正文及M2.5审核均有真实成功调用，但8个group任务仍two_stage=false/缺失。进一步证明全部9个Task的allowed_routes/attestation_ids被旧配置覆盖；当前正式binding仍保存四群general、五群原授权，10份有效原授权均截止2027-01-01。天津音乐原binding的旧摘要与原字段重算不符，其余8份相符。按PRD19.47恢复当前正式范围，未延长或扩大授权。
- 9任务统一reviewed配置快照4a9b0e6d80b234e94b6e4a0da4b11881d4a68d6a4ff8065e3a696a906d97b84f。最初整批FOR UPDATE因运行事务行锁退出，fresh preview证明零写、同hash；逐Task分开应用。02:01:44第1项audit1028383；第二项首次因生产SessionLocal的autoflush=False暴露授权PK尚未flush而校验失败，整项回滚。以相同会话配置本地复现后显式flush，再通过原正式激活。02:03:36—37完成audit1028422—1028428；6407当时被锁而零写，fresh单项preview/CAS后02:04:49完成audit1028454。没有重启/暂停worker、终止DB事务或修改旧Job。
- 02:05:28独立readback逐项确认9/9配置与预期hash一致、正式激活校验通过、两阶段true、每群日目标2000、账号配置/epoch/状态不变；9份旧binding与10份旧授权整行不变，新授权仅id/revision/摘要变化、原attested_at/expiry保留。抽样5旧unknown仍全部整行hash相同。回执group_generation_repair_final_readback.json；此为persisted_verified，四类统一引擎及最终数量/质量E4仍未验收。
- `_bind_fact_first_provider`非法回写Task配置的反例已复现，候选删除该副作用，只影响请求内默认Provider解析；生产14d尚未包含此代码。配置恢复脚本新增已正确Task拒绝重复revision/授权旋转的反例，避免再次运行产生无必要修改。
- 当前bridge仍73388cd1；02:04实机4个runtime源文件SHA256与本候选全部一致、root:root/0644，服务MainPID3058744自01:07:46运行。只读health明确degraded/quota_limited、CLI1.1.22、inflight=false、confirmed_models=[]；不代表Gemini恢复。重复安装未变化bridge的发布修订尚在设计，未执行新发布。

- 本轮最终组合55项通过12.60秒（含生产autoflush=False、重复运行拒绝、原unknown lineage和原window policy保留）；git diff --check通过。02:07:50首次新binding工作样本已出现M3规划34次成功、正文64次成功，M2.5审核32成功/1结构化输出失败；大量Job进入quality_wait_shortfall，尚待逐原因反查，不能将Provider调用成功当成质量合格。新binding且新route链取得1条真实send_message/remote_message_observed（f77ebe14），同观察窗全部主业务为活群16成功/2确定未执行、浏览9成功/1确定未执行；评论与点赞未出现本窗新事实。数据见post_group_generation_repair_facts.jsonl。

### 2026-09-06 02:19 保留未变更bridge的发布修补

- 发布专项§11完成设计及本地实现：安装/检查共用四文件清单；当前受保护runtime与候选内容一致且enabled slots全部active时，只进行认证GET并保留symlink/进程/ledger。输出保留实际provider health/degraded/quota状态，明确model_probe_performed=false；变化或inactive仍执行原完整安装、双模型探针和失败回滚。
- 真实旧restart脚本在隔离环境中使用相同文件与active slot，仍发生runtime-installed及systemctl restart；反例回执antigravity_preserve_original_behavior.json。新脚本与既有deploy、Docker gateway及单次安装回归共39项通过20.97秒，bash -n、git diff --check通过。观测失败原错误退出且不转成安装；没有新增模型或跳过发生代码变化时的schema验证。
- 候选只读checker已在真实服务上以host Python3.11 -E -s执行：认证GET成功、bridge2/CLI1.1.22、degraded/quota_limited=true、confirmed_models=[]、无模型调用。回执antigravity_preserve_readonly_probe.json中的application_release_sha参数dc68仅是本地QA基线，生产应用仍14d、bridge仍733；尚未执行这批新代码的发布。
- 新生成拒绝分类的首次手写SQL用了不存在的Action错误列，改为result JSON后宽JOIN达到12秒statement_timeout，均已明确失败且无写入。改用必要GenerationJob标量后02:17:22成功：28个coverage和1个quantity的quality shortfall尚无evaluator内容，23个为lexical sensory_intent_missing，10个通过语义审核后仍被后续门禁拒绝，另有小量上下文失效及明确语义拒绝。精确后续拒绝原因和原生业务质量仍待反查，不据此下调审核或宣布数量通过。回执post_group_generation_quality_reasons_nested.json。


### 2026-09-06 02:35 新发布及普通路由质量误拒绝修补

- ac50b2afb244c8204cae7117256f829c521a0c61已顺序快进远端master/release，精确读回两ref一致；只dispatch一次Deploy33983952007，候选闸已通过。02:33快照前端与两个PG分片成功，三个no-postgres分片仍执行，尚无新部署证明。原部署33978775792失败及生产14d/bridge733证据继续保留。
- 02:24:58通过tenant/task/type/executed_at索引缩小Action后以Job主键关联，查得29个sensory_intent_missing、23个semantic_review_schema_invalid、16个realizer_length_band_mismatch、11个语义pass后的adult_content_length_out_of_range；同一观测有6条send_message成功。02:29再按冻结route/mode分组，其中8个general/general槽被成人长度误拒绝，另4个成人槽的同码独立保留。回执group_quality_final_rejections.jsonl及group_quality_final_rejections_by_mode.json，不把该样本当整个目标完成率。
- 02:26真实M2.5与生产semantic Prompt对普通产品讨论进行零DB写入/零Telegram调用的解析验证，6.954秒/841 tokens、真实解析通过。该结果不解释生产全部schema失败；将后续失败的根容器、已知字段类型、枚举有效性及容器数量记录到schema_validation，不保存正文或任意字段名、不宽松解包。
- PRD19.48/19.49与质量专项先更新后dev。真实过滤反例和结构/用量反例在旧实现7失败、成人/legacy对照2通过（2.01秒；先修正测试用普通str而非实际GeneratedContent的夹具错误）。修改后普通槽不继承Task成人长度，成人和legacy门不变，解析失败拒绝不变；原过滤/解析按职责移出超长文件。最终普通/混合缓存/重复、共享group/comment两阶段、基础质量、词频与Provider unknown共100 passed/2.95秒，所有后端进程硬60秒。首次回归仅测试预期duplicate_message与实际duplicate_risk不符，纠正断言后通过，未改重复判断。
- 本地新切片尚未包含在已冻结ac50发布；不追加或取消当前run。成人mode的历史长度合同交集、其他审核失败和R1正式共享资源/配置/来源接管仍未完成，不更改已失败或unknown历史。

### 2026-09-06 02:45 ac50独立生产验收与并行发布合并

- Deploy33983952007已终态success。02:40:04独立读回current为`20260905183345_ac50b2af`，19/19容器SHA均ac50b2afb244c8204cae7117256f829c521a0c61、running/healthy、MemorySwappiness=60、无OOM/restart，APIok；MemAvailable899708kB、swap占用307984kB、load1.89/3.25/2.60。回执`ac50_runtime_release_verify.jsonl`。这是单时刻运行证据，不等于全天性能验收。
- 发布日志仅一条Installing release，末尾明确`preserved_unchanged`、model_probe_performed=false及原quota_limited/degraded。独立systemd读取正确unit `tgyunying-antigravity-slot-01.service`证明MainPID仍3058744、active/running、启动仍01:07:46，bridge current仍73388。初次探针对不存在的provider@slot-01名称返回inactive，纠正为部署脚本生成的真实unit后核对；该探针名称错误不作为服务故障。
- 以02:37:56发布完成探针为业务观察锚，02:41:17主业务取得7条view_observed、3条safely_not_executed、1条view remote_outcome_unknown；新binding的活群生成17次规划、34次正文及18次独立审核成功，但新消息typed fact仍空，大量quality shortfall。评论/点赞本窗无新typed成功。回执`ac50_post_release_facts.jsonl`；unknown保持原状态。
- 预备发布adb2普通质量修补时，发现远端master/release已由独立授权任务推进到b375b9bf，Deploy33984749491正在运行。完整保留该7文件提交，合并为a515edca；仅两索引末尾追加冲突，逐项保留双方文字，diff检查通过。原100项质量QA的代码未改变；不取消、覆盖或并发触发当前发布。后续候选无新迁移，变化只影响生成后的过滤与失败证据，需在当前run终态后再以不可变SHA走完整CI和部署；旧bridge四文件未改，仍执行保留观测分支。

### 2026-09-06 03:12 共享账号用量与调用成本本地验收

- b375发布已终态success，02:57:15独立确认19/19服务同SHA且healthy、APIok，available839480kB、swap占用314588kB。之后仅将已验收的质量候选8f85409c99108b0215b202dcbc049af085f0f7ff顺序快进master/release并独立读回，一次dispatch Deploy33985760102（03:00:05）；本地新共享准入代码未进入该候选。不得修改运行中候选或将b375健康当作8f已部署。
- PRD19.50先补设计后实现：原日账本加旧调用与实际调用日跨账本占用独立校验；单账号物理lease不再被pool筛选遗漏；unowned和reserve共用账号锁，call-start排除自己的预约并重验日界。旧日期/身份不转移，读取不补历史三件套。发前余额不足沿原延期分支释放本Attempt且零Gateway。补充生产autoflush=False反例，避免populate_existing覆盖未flush计数；同Attempt禁止二次call-start。初始反例8失败/3对照通过，其中一项先纠正缺参与计划的夹具，随后独立复现真实额度缺口；autoflush和重复标记2项单独红测后修补。
- PRD19.51反查并复现已知调用失败被退回预算（1失败/1未执行对照通过，2.79秒），修补只确认原调用成本，保留Attempt failed与fence业务failed、原ledger/date。两次结算仅扣一次，unknown到明确不存在的原语义保留。没有回填生产历史。
- 最终定向92 passed/15.36秒，包含成本修补及Dispatcher异常helper等价提取后的回归；真实PG10 passed/11.27秒，覆盖UTC日界、跨日成本及pg_blocking_pids确认的unowned与发前预约竞争。PG夹具先补显式flush，再按真实非级联FK顺序清理本测试tenant，未改变生产实现来通过夹具。全部后端硬60秒，仅55439/tg_yunying_test及advisory lock；新模块小于500行、修改函数不超过50非空行、编译和diff检查通过。原12k行Dispatcher只修改相关入口并抽出通用异常体，AST证明原异常行为完全一致。
- 02:59:51在b375中以临时Python进程只读加载候选两reader源码试算，生产文件、数据库、Gateway均无修改；每账号2SELECT。三个当天活跃普通账号的耗时为0.7236/0.0804/0.0932秒，分别明确保留2/2/3条历史物理未证明占用，第三个另有original_task_day_unproven；原日/实际日用量逐类输出。回执shared_usage_candidate_readonly_probe.jsonl保留源码hash，不是候选已经安装或统一Task已激活的证据。
- 后续历史物理证据反查：03:08有25条非success Attempt对应recorded/true journal且带remote_message_id，但仅16条可匹配原request/target指纹，当前算法重算result/evidence hash均不符。此为未闭合线索，尚须核对历史结构版本与独立journal owner；不能据ID存在释放物理占用或改写unknown。完整R1仍需pool/proxy历史物理证明、规划期组合预算、配置/来源单写者与正式初始cutover。

### 2026-09-06 03:18 质量发布独立读回与旧Gateway返回证明

- Deploy33985760102已终态success，单次安装且Antigravity输出preserved_unchanged、model_probe_performed=false，保留733独立runtime和原quota/degraded。03:12:36处于安装末尾时19容器已经8f但current仍b375，首次matched=false如实保留；03:15:19独立再次读回current=20260905190914_8f85409c、19/19同8f且healthy、APIok、无OOM/重启，全部swappiness60，MemAvailable821900kB、swap占用286428kB。回执8f_runtime_release_verify_final.jsonl及完整部署日志。
- 03:10:33之后至03:16:37，远端事实有11次view_observed、5次safely_not_executed、一条group remote_message_observed和32条group remote_outcome_unknown。唯一新群消息对应03:12:50新Job、03:14:30原Attempt、正式binding与policy均匹配，config_revision=3；不能据此证明九群2000/日或完整质量达成。评论/点赞此区间无新完成事实。未知调用聚合29条群无权限、3条FloodWait另单独核对；不重放。首次schema聚合因JSON无equality operator失败，显式转jsonb后成功，不将失败探针计通过。
- 新审核失败形状已实际落盘：2个dict及列表项数5/2各1例，首项的全部7个已知审核字段缺失，其中一个列表属于general；保留schema_invalid，尚未判明是Provider输出还是路由/解析问题。回执8f_post_release_facts_fixed.jsonl，不含生成/审核正文。
- 修正历史SQL重复字段别名后03:12实测25/25 journal owner一致；25/25精确符合3dfa060d之前四字段result/evidence hash，记录时间8月1～24日；16/25匹配原冻结request/request hash/target hash。PRD19.52先闭合两种已发布格式的验证，再实现只读物理返回投影，非空typed字段不能按旧格式忽略，业务unknown/日期/预算不改变。
- 真实写入口构造回执的红测2失败、12对照通过/4.17秒；修补后与旧占用、共享准入、termination联合66 passed/11.27秒，真实PG11 passed/11.09秒，均硬60秒、55439测试库和advisory lock。新代码/测试文件小于500行且函数不超过50非空行，编译及diff检查通过。
- 03:16:22在8f临时只读进程中加载本地候选与3c5旧reader，24账号共57份预算投影完全相同，仅16份物理在途由true转false；38份仍未证明结束。源码hash与只读回执gateway_return_candidate_readonly_probe.jsonl留存，未安装候选、未写生产记录、未创建ACK或激活Task。其他R1项继续未通过。

### 2026-09-06 03:26 MiniMax最终内容协议修补与发布候选

- 对03:18记录补充动作类型后，03:20:12事实为20次view_message/view_observed、11次view_message/safely_not_executed、1次send_message/remote_message_observed、43次ensure_target_membership/remote_outcome_unknown。此前Task级聚合中的group unknown全部属于入群准备，不能称为正文发送未知；更正已同步用户。权限、冻结和FloodWait的补偿探针结果继续与正文分开。回执8f_post_release_action_typed_facts.jsonl。
- M2.5的03:23:03及03:23:59两个普通内容真实探针均返回content内完整think段，分别7.225/6.819秒、875/930tokens，原解析恰好取到最终pass/fail，两例不证明已复现全部线上schema错误。代码中首JSON提取遇到推理中的数组/对象可复现误取；测试4失败/1对照通过/2.06秒。PRD19.53按官方reasoning_split协议闭合后仅在MiniMax请求增加这一参数，不关闭M2.5思考或改变审核标准、原请求身份和失败处理。
- 使用本地候选实际_chat_payload方法，在生产临时Python进程做零DB/Telegram写普通内容探针：03:25:21，M2.5一次HTTP/6.625秒/760tokens，reasoning_details存在且content无think、原审核parser pass；M3另一次HTTP/2.519秒/212tokens，原thinking disabled保留、content无think且请求schema通过。回执minimax_reasoning_split_candidate_probe.jsonl中的M3 http_calls=2是整个探针累积值，不是M3调用重试。未保存正文/推理，仅结构、hash和用量。
- 新协议、schema证据、结构化路由和AiGateway相关82项分两组全部通过：56 no_postgres/4.71秒，另26项/10.78秒；后者仅55439/tg_yunying_test与既有advisory lock，全部硬60秒。修改函数34非空行、测试文件度量/编译、diff检查通过。原共享资源与返回证明的独立QA继续保留；此候选包含本地3c5/e96，未执行正式Task接管或数据回填。
- Release Gate：生产当前8f已独立验证；MiniMax协议和共享资源候选经过设计、反查、代码自审、定向/PG及真实只读/模型探针。发布仅走master→release→Deploy Production，默认诊断/APPLY选项全部关闭；发布后必须再验SHA、runtime、新schema、四类型实际Action与E4，完整R1及日数量仍未接受。

### 2026-09-06 03:44 组合容量性能及恢复结果语义修补

- Deploy33987178618、固定候选88030496已终态failure；两个PG、其余两个no-PG、前端及candidate检查通过，no-PG(2)为1 failed/1935 passed/426.28秒，镜像与部署均skipped。唯一失败为test_held_unknown_does_not_starve_following_terminal_lease的旧released预算断言。反查同时发现Recovery与Dispatcher用bool(call-start)代替原typed mutation；不能只更新断言后重跑。日志deploy_33987178618_failed.log保留，下载曾TLS超时，随后终态日志成功读取。
- PRD19.55先补设计，恢复读取原journal，验证原owner/epoch/request/时间/双hash；无journal仅接受有after_call_at的严格boolean结果。确定失败true确认成本、false释放，called failed缺证据保持原资源并明确报错。16个真实代码反例失败、4个对照通过/5.56秒；修改后与原runtime、共享使用、Gateway返回及组合分配共110 passed/18.83秒。数字0反例最初因Python False==0使ORM未判脏，显式flag_modified保证数据库确为JSON数字后通过；未放宽类型判断。
- 真实PG共18 passed/11.55秒，含恢复true/false原journal、非boolean保持、两个Session的policy锁竞争、原跨日/移组及原Gateway返回。第一次3项仅caplog为空：Alembic测试启动fileConfig禁用了迁移前导入的logger；测试局部恢复该logger后验证原错误确实输出，未修改生产日志配置。全部backend/.venv、硬60秒、显式55439/tg_yunying_test与既有advisory lock，无生产DB写。
- PRD19.54先设计后实现四次有界容量读取，保留原确定性分配/hash及ORM身份映射。旧查询1/32/128候选为8/225/897，三个查询数反例失败、对照通过；纯列初稿另复现3个autoflush=False可见性失败，恢复ORM/load_only后通过。真实PG测试库旧880与新代码用相同任务/策略/候选对比结果完全相同：32账号3次中位0.061832→0.003323秒，128账号3次中位0.247448→0.006723秒，1,632账号单次3.205618→0.045668秒、11,425→4条SELECT。原源码hash与回执portfolio_postgres_benchmark.jsonl留存；这是测试库容量读阶段结果，不是生产吞吐或日完成量。
- 03:43:27独立只读生产仍8f85409c：9 group（two_stage=9）、2 comment（two_stage/grounding=0）、6 like、5 view全部running且unified=0；正式binding、lease、budget reservation、fence均0，TaskConfigSuccessorRevision表未存在。03:13之后主业务72次view_observed、8次send_message/remote_message_observed跨4群、46次view safely_not_executed；121次ensure_target_membership remote_outcome_unknown及11次unknown_deadline_closed单独列示，不能算发送。评论/点赞无本窗新完成事实。回执current_engine_inventory_0343.jsonl；完整正式接管、四类目标与质量仍未验收。
- 发布前补查所有调用结算/恢复入口的旧测试，原身份、Telegram termination、调度抗卡死与Gateway原子性36 passed/7.42秒；与110单元、18PG分组均硬60秒。AST逐函数确认portfolio仅_allocate_request改变，其余保留函数及六个抽出helper与旧880完全等价；新增/调整模块与测试文件行数、函数长度、编译和diff检查通过。本候选无migration或生产配置apply，后续固定SHA必须重新通过全量CI、独立部署读回及业务质量样本，原失败run不重跑。

### 2026-09-06 04:03 01d生产读回与历史结果证明修补

- 固定01d95307c4292e22bb239d0bee104577c9c59814的Deploy33988077680已终态success，全部CI、三镜像及部署通过。03:59:32独立读取current=20260905195505_01d95307，19/19服务同SHA、running/healthy、swappiness60、无OOM/重启，APIok；available931476kB、swap占用290864kB。回执01d_runtime_release_verify_initial.jsonl。组合容量的11,425→4查询优化及原恢复结果修补已进入该版本，不以单点健康证明业务完成。
- 从新dispatcher启动后的03:56:30到03:59:53：3条view_observed、1条view safely_not_executed、1条send_message/remote_message_observed，以及8条ensure_target_membership/remote_outcome_unknown。新群消息对应03:59:12创建Job、03:59:43调用，原policy/binding与config_revision=3匹配；审核schema异常样本为空仅代表该短窗口。评论、点赞本窗无新完成事实。回执01d_post_release_action_typed_facts_initial.jsonl，完整R1和日目标仍未接受。
- PRD19.56先闭合旧reader的JSON类型及journal优先级，再扩展到原结果证明：unknown journal不能被旧false快照覆盖；非boolean不由数据库CAST伪装；false快照完成时间必须不早于调用；定态journal复用原请求/时间/两种hash验证，false与remote id/fact矛盾时保留占用。首轮5失败/3对照通过，进一步原时间/回执反例6失败/8对照通过；在夹具使用正式空字符串结果格式与双hash后重复红测仍6失败，确认不是夹具差异掩盖缺陷。
- 修补及原共享准入、恢复、Gateway返回等91 passed/17.31秒；真实PostgreSQL28 passed/8.14秒，包含原写入口生成的有效false回执与损坏请求/hash/时间/remote fact对照。末尾为控制复杂度拆出journal投影helper，相关52 passed/10.81秒。全部backend/.venv、硬60秒、显式55439/tg_yunying_test与advisory lock；新/改模块和测试小于500行、函数不超过50非空行，编译与diff检查通过。
- 04:01:55在01d临时只读进程加载候选源码，32账号87份投影全部相同、46份旧物理占用仍保留，9份original_task_day_unproven未抹除。回执legacy_strict_candidate_readonly_sample.jsonl记录源码hash。全1,512账号对照先遇到数据库操作错误，未获得完整对照；该次stderr输出只留尾部，原因不能据此确认，后续需单独检查查询计划与开销。抽样的先旧后新耗时有缓存影响，不用作性能收益结论。此代码尚为本地候选，无生产DB写、历史ACK或正式Task接管。
