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

- Prepare/Deploy run及候选完整SHA：
- current/backend/18worker完整SHA、健康、API与迁移：
- 逐Task新typed事实、当前日目标/coverage、窗口错位数量及首个阻断：
- 最终状态：production_unproven，禁止提前写production_fixed。
