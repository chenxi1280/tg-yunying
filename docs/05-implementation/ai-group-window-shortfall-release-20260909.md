# AI活群窗口与E4报告统一 Release Gate

- intake_id: AI-GROUP-WINDOW-SHORTFALL-20260909；L3/P1。
- release_mode: github_actions；release_owner/rollback_owner: 当前Codex。
- source: codex/ai-group-window-shortfall-20260909；base fa26ddbd。
- scope: W1窗口求交、W5可信E4报告；ed199a66 + 4e3628b0 + 2dc85657及整合QA/文档。E4原提交88046287/d77a853d已逐文件审查，索引冲突保留双方；没有纳入其他任务未发布代码。
- status: local_gate_passed，线上待正式Prepare/Deploy验证。

## 本地证据

- 最终统一重跑：12个定向文件151 passed / 21.43s；包括全部上述窗口、Session、Source和E4用例，无失败/跳过。唯一warning为既有Alembic path_separator弃用提示。compileall、Ruff F（新模块/本次测试）、git diff --check通过；guard 490行，新函数均不超过50非空行，原未改reserve入口53非空行记录为既有项。

- 真实reserve/rearm/claim原代码7条红例；修复后窗口定向10 passed / 3.57s，包括group gap、Source release后的重新领取、排他end与原deadline。
- 组合回归64 passed / 14.16s：窗口10、Session/wake/legacy、原pacing合同、Source独占预约/owner重用/未知边界，以及真实PG领取锁、E4快照隔离与拒写。两个新PG测试文件修正夹具外键顺序后通过；未修改生产约束。
- E4原报告/身份/分母/scope本地63用例通过；真实PG两个snapshot用例通过。上一组合执行65通过、仅窗口PG夹具外键失败，已在上项修正复验。
- 所有后端测试通过backend/.venv启动，每进程subprocess硬超时60秒；不把中途失败计为通过。
- 代码审查：原异常/公开入口/锁顺序/状态结算保持，窗口循环严格推进至后续原窗口，无任意重试上限。新模块按责任抽取；原guard的3个历史未使用兼容别名保留，不做无关清理。
- PRD、产品总纲、结构/数据流索引、运行文档、状态板已同步。新模块静态检查与diff检查通过；完整CI结果待填写。

## 风险与边界

- 无schema/API/前端/配置变更；正常worker会处理尚未调用的旧错位预约。无直接生产数据清理，无Provider/Telegram试发脚本。
- W2共享尾游标按统一引擎§19.18保持；W3最近2小时22条容量拒绝均为修复前创建的存量Action，无新超配证据；W4准入/不可见/unknown保持原事实边界。
- rollback_plan: 无新增迁移；若新代码回归，按当前部署/0229兼容合同准备前向修复，不能无审计切回worker或重放未知发送。
- observe_window: 发布完成后独立只读观察10个running活群；比较新预约、已重验claim、原due/day/deadline和Gateway/typed消息；不把窗口外的尚未重验旧行计为新代码回归。

## 发布后填写

- 首次Prepare 34361033434（2ceca87a）失败且未部署：新增PG窗口测试把tenant=1提交到共享public schema，后续test_account_profile_identity_postgres发生主键冲突。顺序本地反例1 failed/1 passed（8.66s）复现。修复仅测试：窗口与E4并发回归改用独立随机schema及原测试库会话锁，用完回收该schema；不改生产代码、不删除共享测试行、不跳检查。修复后含后继账号测试的顺序回归77 passed/20.01s；Ruff/diff通过，新SHA必须重新完整Prepare。
- 第二次Prepare 34362509365（b6a3551d）仅窗口PG用例失败，422 passed：独立schema连接遗漏应用的Asia/Shanghai设置，CI默认UTC将夹具10:00入库解释为北京时间18:00。本地显式UTC复现同一断言（1 failed/3.92s），修复为复用app.database.connect_args并断言实际SHOW timezone；无生产代码变更。修复后窗口/后继账号/Session/E4顺序88 passed/21.64s，Ruff/diff通过。前一候选完整152定向也通过27.13s，但不能替代新候选完整CI；第三次继续全量Prepare。

- Prepare/Deploy run及候选完整SHA：
- current/backend/18worker完整SHA、健康、API与迁移：
- 逐Task新typed事实、当前日目标/coverage、窗口错位数量及首个阻断：
- 最终状态：production_unproven，禁止提前写production_fixed。

## 首次部署与真实反查（22:41–22:50）

- 2712d4377fc5a7065ce412ac3643e6d2ce59f755：Prepare34363868460完整14项成功，Deploy34364859426于22:41:00完成。独立current=20260909143813_2712d437；backend+18worker全部完整SHA一致且healthy，API本地18090和公网健康，迁移0229_admission_gap_count。
- 22:42:02发布后更新的23条开放Session预约全部在合法窗口且deadline前；新创建预约0，不能以该快照证明新建排期的线上验收。
- E4报告发现实际关联缺陷：真实Action不含payload任务日字段，使用primary_quantity_slot_id关联ledger。发布后6条真实事实链确认该结构，旧报告全部漏为0且开放队列为空。先补PRD再修只读查询/样本身份；真实结构测试原代码5 failed/39 passed，修复后71 passed/11.49s。
- 候选代码只在独立只读诊断进程内加载，未改生产文件/worker；同22:41锚点已读到5个Task共15条真实新消息，开放队列不再漏空，全部样本ledger_matches为true。日目标仍未达标，CLI保持E4 gate failed。这是候选只读验证，不是补丁已部署；后续新候选仍须完整Prepare/Deploy。
- 最终整合定向158 passed/27.75s，含全部窗口、Source、E4及真实PG隔离/锁/快照；Ruff与diff通过。该补丁只改E4只读身份关联，不改变已上线窗口和发送行为。
