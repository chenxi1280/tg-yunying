# 群克隆暂停与来源连续性修复

## Intake / Product Handoff

- intake_id: clone-progress-repair-20260910
- 用户请求：排查克隆测试未推进，随后明确要求“你来修复问题”。
- level: L3；流程：prod-diagnosis → product → dev → qa → product → prod-diagnosis。
- scope: 独立 group_clone / v2_group_clone；生产测试 Task ce341c64-878e-482a-8b91-598346d5b885，tenant 1。
- merge_owner/release_owner: 当前任务；隔离分支 codex/clone-progress-fix-20260910。
- 仅锁定 Clone Collector 的 channel 状态模块、Clone source stream/runtime lifecycle、对应测试和本合同/索引；主工作区其他修改保留。

## 当前证据与根因分组

2026-09-10 09:28–09:31 北京时间，生产 e62a0d8073f0f94b631d796b3b36ff83c0b89f9d。唯一 Clone Task 为 failed/epoch2、group_clone_channel_difference_too_long。1051 个 SourceEvent，1 succeeded 和1 waiting_binding obligation，只有 1 success Action/Attempt/clone_message_observed/mapping，最后远端确认时间为 09-09 21:43:47。账号池仅437。

R1：09-09 21:44:53 有正式 Pause 审计，之后没有 Resume/Start 审计。Collector block/recover 不检查暂停状态，复现 paused → failed → running。Source consumer 的 promote 也无生命周期判定，属于同一根因。

R2：too_long 被当成可恢复分页，返回快照被正规化为差量事件，后续 final 自动清除缺口；原测试甚至期望该行为。[Telegram 官方定义](https://core.telegram.org/constructor/updates.channelDifferenceTooLong)明确说明请求 PTS 后的更新已有丢失。因此不能用较新空批次证明旧连续性恢复。

R3：单发送账号已绑定首位来源发言人，下一义务等待 sender_pool_exhausted，Sequencer 正确阻止越序。暂未证明 binding 算法缺陷；不降低既有安全回收时间、不把同 Telegram 身份的两个平台记录当两个发送人。

## Product Design Complete

design_status=complete（R1/R2），resync=true。已更新专项 PRD §6.2、§12.1、§18.2。

- 原始需求：后台不得覆盖人工暂停，来源缺口不可伪装补齐，恢复与发送证据分别验收。
- 状态/UI：Task 保持 paused；来源可单独 gap/blocked，last_error 保留可诊断原因。blocked Resume 明确报错；正常 gap 仍可恢复。
- 后端/worker：Task 行锁重新读取生命周期，再处理当前 source stream；旧 epoch、停止、删除、退役、无关 failed 不恢复。暂停消费零新 SourceEvent/Action。
- 数据：无 schema 迁移。too_long 证据存现有 source difference_cursor；原 PTS、事件、unknown、mapping 保留。共享授权其他 peer 继续采集。
- 安全/幂等：不新增发送通道、fallback、自动重放或自动激活。生产状态修正必须精确 Task/epoch/原状态/部署 SHA 与审计，独立读回。
- 回滚：代码可前向修复；旧版本会再次违反暂停合同，禁止把回滚旧代码当成暂停保护。已记录历史缺口不能因版本切换变成成功。
- QA：真实 DB 持久化的 Pause → incomplete → final/too_long → final；缓存旧 Task 并发暂停；正常 gap 恢复；其他生命周期/旧 epoch 不变；too_long 快照零 ingress，阻塞保持；Resume blocked 不清错；完整 Clone 定向回归。

## Release Gate

- status: local_passed / CI pending
- release_mode: github_actions；master → Prepare Production → release → Deploy Production。
- migration_impact: none；frontend/API shape: none；worker_impact: shared Collector channel projection and Clone consumer。
- external_platform_impact: 发布本身不触发新发送；原测试恢复另按当前 preview、正式服务、审计与读回处理。
- local_gate / CI / deployed SHA / runtime / production evidence: 待补。

## 开发审查与定向 QA

- R1/R2 初始9项反例在旧代码上全部失败；修复后新增生命周期/旧epoch/删除/持久化旧too_long/Start与Resume反例。原错误的too_long“翻页成功”测试替换为合法slice分页，继续验证正常gap→final恢复。
- channel游标/错误投影/来源状态从658行Collector拆至telegram_update_channels.py；租约、claim、远端读取和正式Ingress仍在原入口，模块均低于500行、函数低于50个非空行。
- 并发自检覆盖 Collector stale ORM 和 operator Pause：Task先锁、populate_existing读回；错误投影同样锁定当前Task，避免刷新stats覆盖暂停标记。暂停消费保留pending durable delivery，零新SourceEvent/Action。
- 允许正式Pause接收明确来源故障的failed Task，用于保留运营暂停意图；原错误与source blocked保持，无关failed不改变。
- 真实本地PostgreSQL16独立schema：新增2项Pause并发验证通过；加既有listener订阅锁回归共4项通过（7.90秒）；最后生命周期改动后新增2项复核通过（6.69秒）。数据库为tg_yunying_test，端口55456；未在生产执行测试。
- 前轮Clone+AI/评论共享流166项通过（31.06秒）；最后补入“source failed正式Pause”和实际pending delivery反例后167项通过（34.91秒，60秒硬超时）。两次新增fixture的初始状态期望/Ingress可写状态错误已纠正，不算生产缺陷。
- 静态F类（修改生产文件与新增测试）、AST语法/行数限制、git diff --check通过。既有Ingress测试的基线未使用Session导入未扩大清理。
- 产品验收：R1/R2本地合同已覆盖；R3是单账号配置容量限制，目标测试群数据库只有437可发送记录。用户于09-10 10:02明确授权“使用账号 2，完成多人测试后暂停”；使用正式Stop保存旧任务证据，再以相同来源/目标创建发送池[437,2]的新受控测试，两个不同身份均获远端事实后正式Pause。

## CI 反向检查与设计补正

- 首轮 Prepare Production 34427815869 / b593342d：PostgreSQL分片0失败，其余分片、前端及镜像构建通过。唯一失败是既有 `test_update_consumer_locks_postgres` 的Clone用例：消费端Task `FOR UPDATE` 阻挡Collector插入delivery外键所需的 `KEY SHARE`，发生 `LockNotAvailable`。
- resync=true，PRD §12.1补正：Task采用 `FOR NO KEY UPDATE`，保持Pause/状态写互斥，允许其他事务建立外键引用。不得降低并发测试断言或扩大锁超时。
- 本地真实PostgreSQL复现同一失败（14.59秒）；补正后既有AI/评论/Clone消费并发、路由rebind、Pause与生命周期回归22项全部通过（13.20秒，60秒硬超时）。新增断言验证Collector持锁时KEY SHARE可进入、生命周期排他写仍被互斥。Ruff F与diff检查通过。
