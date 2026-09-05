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
