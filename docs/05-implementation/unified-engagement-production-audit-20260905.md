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
