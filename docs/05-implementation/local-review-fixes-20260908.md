# 本地修复二次审查闭环（2026-09-08）

## Intake / Bug Batch Plan

- 用户输入：本地修复审查后“你来修复这些问题”。
- 分级：L2代码修复，影响生产规划/验证/救援；本地复现完成，生产尚未证明这些未提交改动已经生效。
- merge_owner：当前Codex任务；单写者。保留用户既有改动，不纳入无关worktree内容。
- 分组A：账号候选缺失or_；算术题截断误答、万位解析错误。
- 分组B：观察版本误作失败次数；网络错误造成任务级永久放弃。
- 分组C：五条救援候选共同争抢同一管理员资源，未实现串行吞吐。
- 原回归：98项95通过3失败（or_ NameError）；嵌套算式/小数/万位和跨日误弃均有本地复现。

## Product Handoff / Design Complete

产品合同为频道成员前置设计§17.3–17.6，本次resync已完成。算术提取完整表达式、精确计算和失败不提交；观察计数独立持久且版本CAS，成功/正常复检清零，三次失败只弃当天，Planner与执行门均能跨日重开；救援复用完成唤醒逐条补领，不把同批五个claim误称串行执行。

权限与安全：不改变账号资格、可信题目识别、Provider/Telegram路由、租户边界、任务epoch、真实节奏或unknown防重；只增加观察计数字段，不运行维护apply，不重放旧Action。前端/API不变，失败通过已有状态与精确blocker展示。

## 开发与QA

- A：真实目标群筛选回归；完整嵌套、小数、分数中间值、中文万位及非法输入；真实自动提交入口验证正确答案/错误零外呼。
- B：高观察版本首次失败不终止；三次真实失败才终止；成功读空列表、follow和surface恢复清零；CAS冲突；同日不重开，次日Planner重开与安全账本边界。
- C：连续五条同管理员救援使用真实预约和持久executor串行推进，其他慢工作不阻挡；busy候选不改期；异常/关停/跨owner资源释放回归。
- 后端测试使用backend/.venv，每批硬超时60秒；SQLite无生产连接，涉及迁移/CAS的PostgreSQL验证使用隔离测试库。

## Release Gate

- release_mode：github_actions，master -> release -> Deploy Production。
- status：release_passed；production_partial，六项真实业务验收尚未全部闭合。
- backend_tests / migration：pass，见下方；static：修改路径编译及新增模块定向检查通过。
- frontend_build：不涉及前端，沿部署既有构建。
- worker_impact：候选、文本验证、Task准入与恢复；复用持续Dispatcher。
- external_platform_impact：正确算术答案；救援仍逐Action经过原Gateway门。
- rollback：新增计数字段不删除已记录失败证据，存在数据时前向修复；不回滚生产任务状态。
- post_release：独立current/完整SHA/容器health；发布后按Task→ledger→Action→Attempt→typed远端事实核对。未出现对应真实题目或网络失败则该验收保持unproven。

## 验证结果

- 定向回归分批：算术/AI日规划与生成准入82通过；持续Dispatcher/成员策略64通过；准入恢复/账号候选/事实合同/群机器人scope/日覆盖74通过；追加账本保护后算术与准入61通过。批次有交集，不相加为独立用例总数。
- 最终迁移批次74通过（32.26秒）：旧建库、版本图、存量升级、账号批登录、Provider HTTP证据、评论质量目标及新增真实PostgreSQL观察失败/CAS/迁移回归。测试库为本次独占localhost:54688的tg_yunying_test，每个pytest进程硬超时60秒。
- 迁移反向检查发现0001/0137从当前ORM提前建入新列，已resync：legacy bootstrap排除新字段，0137准入表使用相同历史基线，真实0229负责加列；完整建库及升级回归通过。
- 修改路径compileall、新增解析/状态模块与测试的F821/F401、git diff --check通过。dispatcher既有group_clone路径存在HEAD已含的TelegramGatewayMutationIdentity未定义告警，不属于此次六项修改，未把全文件静态检查声明为通过。
- product_accepted：本地六项修复合同和定向证据通过。设计§17.2主动探测方案不在此次六项修复实现范围。


## 发布与独立读回

- 代码完整SHA：`0c5e5dc58ce5dd7aa38ecd5b63397a96e1fd9957`；master -> release均fast-forward，无维护apply或旧Action重放。
- [Prepare Production 34244923838](https://github.com/chenxi1280/tg-yunying/actions/runs/34244923838)：全部通过，含后端8个测试分片、前端检查及三个镜像。
- [Deploy Production 34245679954](https://github.com/chenxi1280/tg-yunying/actions/runs/34245679954)：success；终态时间2026-09-08 23:39:12北京时间，作为保守业务验收起点。
- 独立SSH：current=`/data/tgyunying/releases/20260908153537_0c5e5dc5`；backend+18业务worker完整RELEASE_SHA均为上述代码SHA且全部healthy；图形验证worker也healthy，使用预构建digest（该容器不提供RELEASE_SHA环境字段）。`127.0.0.1:18090/api/health`返回ok。
- 数据库只读：head=`0229_admission_gap_count`，新列integer、NOT NULL、default 0。所有查询事务READ ONLY，statement_timeout=10s，lock_timeout=1s。
- 23:44:07读回分母：10个running AI活群Task、5913条准入记录。互斥分类：ready且计数0为5503；observing且计数0为41；observation_gap_limit_reached且计数3为369。后者全部记录ChannelPrivateError、abandoned_for_day、terminal_date=2026-09-08。23:40前次读回曾有计数1为360、计数2为9，证明独立计数与当天终止已实际触发。
- 发布后新增AI正文Attempt：success 1；Gateway前跳过42（account_legacy_remote_inflight 3、account_shared_usage_unproven 2、execution_circuit_open 13、execution_circuit_probe_pending 2、pacing_source_not_before 22）。完整Task→ledger→Action→Attempt→typed fact联结得到1条remote_message_observed，Task=`caed73c4-16d6-440e-8d0d-109c6fa1f184`，对应1个Action、1个Attempt、1个日账本；仅证明该Task新增1条真实发送，不代表10个Task全量履约，也不能归因于算术修复。
- 发布后救援Attempt新增1条result_unknown/unknown_after_send；未重放、未当作救援成功。真实算术验证新增0条。
- 验收边界：独立计数及三次失败当天终止已有线上证据；跨日恢复、真实算术题完整解答和连续五条真实救援成功尚无完整线上样本，保留unproven。ChannelPrivateError及既有执行/节奏阻塞仍存在。`production_fixed`未声明。
- 本节为发布后证据文档，文档提交不改变上述已部署代码SHA。
