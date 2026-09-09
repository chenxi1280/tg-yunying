# AI活群窗口与E4报告统一 Release Gate

- intake_id: AI-GROUP-WINDOW-SHORTFALL-20260909；L3/P1。
- release_mode: github_actions；release_owner/rollback_owner: 当前Codex。
- source: codex/ai-group-window-shortfall-20260909；base fa26ddbd。
- scope: W1窗口求交、W5可信E4报告；ed199a66 + 4e3628b0 + 2dc85657及整合QA/文档。E4原提交88046287/d77a853d已逐文件审查，索引冲突保留双方；没有纳入其他任务未发布代码。
- status: release_passed；158项定向及完整Prepare/Deploy通过，独立SHA/runtime通过；10任务全量业务验收failed，新建预约线上样本unproven，禁止production_fixed。最终证据见文末。

## 本地证据

- 最终统一重跑：12个定向文件151 passed / 21.43s；包括全部上述窗口、Session、Source和E4用例，无失败/跳过。唯一warning为既有Alembic path_separator弃用提示。compileall、Ruff F（新模块/本次测试）、git diff --check通过；guard 490行，新函数均不超过50非空行，原未改reserve入口53非空行记录为既有项。

- 真实reserve/rearm/claim原代码7条红例；修复后窗口定向10 passed / 3.57s，包括group gap、Source release后的重新领取、排他end与原deadline。
- 组合回归64 passed / 14.16s：窗口10、Session/wake/legacy、原pacing合同、Source独占预约/owner重用/未知边界，以及真实PG领取锁、E4快照隔离与拒写。两个新PG测试文件修正夹具外键顺序后通过；未修改生产约束。
- E4原报告/身份/分母/scope本地63用例通过；真实PG两个snapshot用例通过。上一组合执行65通过、仅窗口PG夹具外键失败，已在上项修正复验。
- 所有后端测试通过backend/.venv启动，每进程subprocess硬超时60秒；不把中途失败计为通过。
- 代码审查：原异常/公开入口/锁顺序/状态结算保持，窗口循环严格推进至后续原窗口，无任意重试上限。新模块按责任抽取；原guard的3个历史未使用兼容别名保留，不做无关清理。
- PRD、产品总纲、结构/数据流索引、运行文档、状态板已同步。新模块静态检查与diff检查通过；完整CI结果见最终验收章节。

## 风险与边界

- 无schema/API/前端/配置变更；正常worker会处理尚未调用的旧错位预约。无直接生产数据清理，无Provider/Telegram试发脚本。
- W2共享尾游标按统一引擎§19.18保持；W3最近2小时22条容量拒绝均为修复前创建的存量Action，无新超配证据；W4准入/不可见/unknown保持原事实边界。
- rollback_plan: 无新增迁移；若新代码回归，按当前部署/0229兼容合同准备前向修复，不能无审计切回worker或重放未知发送。
- observe_window: 发布完成后独立只读观察10个running活群；比较新预约、已重验claim、原due/day/deadline和Gateway/typed消息；不把窗口外的尚未重验旧行计为新代码回归。

## 发布过程与失败修复记录

- 首次Prepare 34361033434（2ceca87a）失败且未部署：新增PG窗口测试把tenant=1提交到共享public schema，后续test_account_profile_identity_postgres发生主键冲突。顺序本地反例1 failed/1 passed（8.66s）复现。修复仅测试：窗口与E4并发回归改用独立随机schema及原测试库会话锁，用完回收该schema；不改生产代码、不删除共享测试行、不跳检查。修复后含后继账号测试的顺序回归77 passed/20.01s；Ruff/diff通过，新SHA必须重新完整Prepare。
- 第二次Prepare 34362509365（b6a3551d）仅窗口PG用例失败，422 passed：独立schema连接遗漏应用的Asia/Shanghai设置，CI默认UTC将夹具10:00入库解释为北京时间18:00。本地显式UTC复现同一断言（1 failed/3.92s），修复为复用app.database.connect_args并断言实际SHOW timezone；无生产代码变更。修复后窗口/后继账号/Session/E4顺序88 passed/21.64s，Ruff/diff通过。前一候选完整152定向也通过27.13s，但不能替代新候选完整CI；第三次继续全量Prepare。

- Prepare/Deploy、完整SHA、runtime及逐Task证据均已填写至最终验收章节；历史失败记录保留，不以最终成功覆盖失败过程。

## 首次部署与真实反查（22:41–22:50）

- 2712d4377fc5a7065ce412ac3643e6d2ce59f755：Prepare34363868460完整14项成功，Deploy34364859426于22:41:00完成。独立current=20260909143813_2712d437；backend+18worker全部完整SHA一致且healthy，API本地18090和公网健康，迁移0229_admission_gap_count。
- 22:42:02发布后更新的23条开放Session预约全部在合法窗口且deadline前；新创建预约0，不能以该快照证明新建排期的线上验收。
- E4报告发现实际关联缺陷：真实Action不含payload任务日字段，使用primary_quantity_slot_id关联ledger。发布后6条真实事实链确认该结构，旧报告全部漏为0且开放队列为空。先补PRD再修只读查询/样本身份；真实结构测试原代码5 failed/39 passed，修复后71 passed/11.49s。
- 候选代码只在独立只读诊断进程内加载，未改生产文件/worker；同22:41锚点已读到5个Task共15条真实新消息，开放队列不再漏空，全部样本ledger_matches为true。日目标仍未达标，CLI保持E4 gate failed。这是候选只读验证，不是补丁已部署；后续新候选仍须完整Prepare/Deploy。
- 最终整合定向158 passed/27.75s，含全部窗口、Source、E4及真实PG隔离/锁/快照；Ruff与diff通过。该补丁只改E4只读身份关联，不改变已上线窗口和发送行为。

## 最终统一部署与独立验收（23:08–23:13）

- candidate/deployed SHA: `839c7b19c419592e9c41b7b8e3a66c80511f300c`。正式路径master→release→GitHub Actions；[Prepare34366995665](https://github.com/chenxi1280/tg-yunying/actions/runs/34366995665)完整14个作业成功；[Deploy34367948474](https://github.com/chenxi1280/tg-yunying/actions/runs/34367948474)3个作业成功，完成于2026-09-09 23:08:38+08:00。额外数据维护、Planner drain、准入重试等选项全部未启用。
- 独立SSH读回：current=/data/tgyunying/releases/20260909150604_839c7b19；backend及18worker全部完整SHA一致、running/healthy；本地18090与公网/api/health均ok，alembic=0229_admission_gap_count(head)。release_status=release_passed。
- 23:11:22只读窗口快照以W1首次上线22:41为锚点：146条更新预约、其中138条reserved/bound，窗口/deadline越界0；新建预约0，所以旧预约正常更新的一致性通过，新建预约的生产证明仍unproven。测试已经覆盖新建/重建/claim，不能替代未观测到的新建生产样本。
- 23:11正式已部署CLI以最终发布23:08:38为锚点：4个Task共6条严格同身份的新Gateway/typed消息事实；10个Task全部仍有daily due和coverage缺口，CLI按合同退出1（业务Gate失败，并非查询失败）。全部开放Action样本ledger_matches=true，旧报告漏空已修正。
- 23:13再次使用正式CLI、单独以W1首次发布22:41为锚点：西安5、三亚6、郑州楼凤9、郑州学生会10、郑州师范7，共37条严格新消息事实；其余5个Task为0。该37条包含最终发布后的消息，不能与上项6条相加；也不能代表10任务达标。
- 22:30:03→23:11:22逐Task比较：10个Task的name/status/epoch/type_config hash/configured target/effective target均无变化，全部running/epoch2；账号资格投影可随真实状态自然变化，不将其描述为本发布改目标。

### 23:11逐Task矩阵

服务层10项均pass；下表“确认/到时应完成”是原群日数量投影，“覆盖确认/完整必达”是coverage投影；两种口径不混同逐条typed事实。新消息列仅统计23:08:38之后的新执行。队列原因是当前状态分组，不把正常来源等待统称算法故障，也不把分组行数相加当独立账号。

| Task | ID前缀 | 日确认/到时应完成 | 覆盖确认/完整必达 | 最终发布后新typed消息 | 当前主要未闭合边界 |
| --- | --- | --- | --- | --- | --- |
| 郑州大学 | 11f3591a | 1/1951 | 1/1833 | 0 | 生成端41个Provider未知；覆盖未知279、待准入7 |
| 天津音乐 | 1c20106a | 0/2250 | 0/536 | 0 | 待准入64、覆盖未知79；仅1个ready |
| 美美备用 | 4a5d721a | 15/1110 | 8/563 | 0 | 待准入413；来源等待115 |
| 西安天上人间 | 5063e30f | 90/1881 | 91/1833 | 1 | 来源等待255；覆盖未知370 |
| 三亚 | 562662d2 | 194/2107 | 195/1833 | 1 | Provider未知82、覆盖未知241 |
| 郑州楼凤 | 894d6924 | 146/2038 | 152/1833 | 2 | 来源等待109、待准入130、覆盖未知67 |
| 郑州学生会 | 8d64449d | 167/2234 | 168/1833 | 2 | 来源等待110、覆盖未知454 |
| 天津一品楼 | 8d6cfb4d | 12/1676 | 10/304 | 0 | Provider未知22；覆盖未知3 |
| 郑州师范 | caed73c4 | 160/1862 | 160/1833 | 0 | 来源等待196、待准入364、覆盖未知14 |
| 成都怡红院 | e5882928 | 94/2088 | 86/1833 | 0 | 上下文新鲜度未证实86；覆盖未知252 |

- 日数量合计879/19197，缺口18318；10项均为daily fulfillment failed。已开始有新消息只证明对应事实链，不能标记production_fixed。
- W2来源排期仍遵守现行共享尾游标/间隔；W3没有新分配超配反例；W4外部准入、Provider未知、不可见和远端unknown保留。未对历史未知做重放、删除或降级确认，也未放宽活动窗或降低目标来通过验收。
- 最终结论：implementation/targeted QA/full CI/deployment/runtime通过；E4报告已按真实规范槽身份正确读出消息及队列；全量业务验收failed，新建预约线上样本unproven。当前10任务低履约不能声明已全部修复。
- 脱敏只读快照留存于本机/tmp/tgyunying-task-progress-release.hp1Fnx/final839c-production-evidence-2313.json；含完整Task ID、两锚点统计、runtime/合同对比输入，不含消息正文、凭证或完整失败文本。
