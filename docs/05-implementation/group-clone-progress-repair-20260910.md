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

## 第一轮发布与生产反向检查

- Prepare 34428615955成功，Deploy 34429196287成功；生产current=`20260910022307_24704522`，后端及18worker均为24704522且healthy，应用/Nginx/公网health均ok，schema head=`0231_ai_group_emergency_history`，无本次迁移。
- 10:13:40正式Stop旧测试，epoch2→3；旧1051SourceEvent、1成功消息映射保留，waiting_binding义务取消。审计1144686。账号2入群调用前预览/指纹校验，10:14:06 joined并独立回读send权限；TgGroupAccount39077，审计1144688–1144690；账号437群主事实不变。
- 新测试未创建/零发送。正式受控Collector auth1/2398追赶时暴露回归：清除不存在的channel错误也先加Task锁。账号2六个active订阅均无channel错误，但Collector的Task锁已等待其他Planner长事务超过3分钟，阻断共享采集。取消的仅是本次验收启动的Collector进程135，未中断业务worker或数据库后台。
- 返回product，resync=true：无变化的错误投影必须零Task行锁；PRD §12.1补正后再dev/并发QA/重新发布。不得用修改游标、伪造live、跳过预检或反复重试掩盖该阻塞。
- 新增PostgreSQL反例在24704522上5.46秒内复现Task锁等待；修复后无操作投影9项、真实错误清理/暂停1项，结合既有Pause、消费并发与生命周期共32项通过（51.75秒，60秒硬超时）。Ruff F/diff检查通过。扩大Clone/共享流回归及拆分批次在本机先后触及60秒硬超时，未观察到断言失败，不能记为通过；完整回归由新候选的Prepare Production重新验收。

## 第二轮部署与双账号测试反向检查

- 候选047625b4：Prepare34430457225、Deploy34431043598全部成功。生产current=20260910025140_047625b4；后端及18worker版本一致、健康、三层health全部ok。此前Task无操作锁阻塞已解除，正式Collector两轮后账号2/437均live，并连续采集。
- 预检无阻塞/警告，目标authority v7，确认两个Telegram身份不同。10:56:57创建双账号测试46b91930-e10a-4287-ab90-773325561219，epoch1，发送池[437,2]；listener正式建立start_message_id3077722/start_pts5623033。
- 常态Planner尚未领取新任务（wake_revision1/planned_revision0/last_started空）；按已授权测试范围通过现有_plan_due_task触发该Task，保留任务/wake锁、Planner IO禁令和常态Dispatcher。这是受控验收触发，不证明常态调度延迟已解决。
- 消费13个SourceEvent后暴露原有PTS乱序处理缺陷：frontier5623078，head ingress4371为UpdateNewChannelMessage pts5623080/count1；随后ingress4374已有DifferenceMessages pts5623081/count3，另含历史消息删除更新。头部始终阻止消费，Task在source_pts_gap和正常final恢复之间切换；零义务/Action/Attempt/远端事实。
- 正式Pause新测试，保留epoch1和全部证据；受控Planner、证据等待器停止，Collector完成两轮暂停后采集并退出。多人E4仍未通过，不能宣称production_fixed。
- 返回product完成上述乱序差量证明合同与索引resync；进入dev实现ChannelDifferenceRange、实际请求PTS传递、当前订阅证明和未证明头部的正式补差。不得凭现有Common/DifferenceMessages的数字回填假证明，不手改游标或历史状态。

## 乱序PTS修复开发与QA

- 已完成真实频道请求区间持久化、当前授权/peer/epoch订阅证明，以及队首未证明缺口补差；补差只改变下一次远端读取起点，不直接重写共享游标。普通final投影后保留clone_gap_at的恢复依据，获得本页区间后正常续页。区间空items，零消息投递/业务事件，too_long和Common不产生证明。
- 新增区间正反例及Collector实际调用回归19项通过（6.76秒）；暂停PostgreSQL并发、无操作投影、生命周期、Clone/AI/评论共享消费回归54项通过（25.08秒），每批60秒硬超时。AST语法与生产代码行数限制通过。
- 再次只读线上确认新测试仍paused/epoch1、13SourceEvent、零义务/Action/Attempt/映射；原任务stopped/epoch3、旧事实未增加。多人E4等待此候选通过Prepare/部署后重新验收。
- 58f2b863完整Prepare34433089754通过；同步最新master后本地155项Clone回归通过（29.09秒）。追加审查发现首个正常slice未覆盖队首时可能重复请求第一页；返回product补齐多页连续区间累计合同，再进入dev。真实消费PTS仍按消息推进，分段证明只决定补差下一页起点；测试保持暂停等待补正版本。

- 跨页补正后区间/Collector/Ingress定向35项通过（7.08秒），Ruff F、AST行数和diff检查通过。58f2b863旧Actions部署34433650644成功，独立读回current=20260910033154_58f2b863，19容器版本一致且健康，三层health正常；多人测试仍未恢复。
- 发布规则变更：用户在“优化本地镜像部署流程”任务中已明确停用两个发布Actions并采用本地镜像发布，远端release加入7704f386，暂与master分叉。此后不再派发或重启Actions；当前跨页补正需要按新的正式本地入口和发布分支完成准备/安装，不能覆盖并行发布规则。

- 新增真实PostgreSQL跨Session验证：Collector提交两页区间后，独立consumer读取并消费队首，最终消费PTS保持502而非证明终点503；独立运行1项通过（11.07秒）。与多个既有并发文件合并的一批触及60秒硬超时，未声明该整批通过；此前完整CI及定向并发结果分别保留。
- 本地发布前提读回：Docker27.5.1/Buildx运行，支持linux/amd64；Docker credential helper未发现GHCR凭据，当前GHCR_USERNAME/GHCR_TOKEN环境为空，GitHub登录权限为repo/workflow/gist/read:org，不含write:packages。已请求用户提供现有发布凭据的配置位置，不打印或写入凭据。
