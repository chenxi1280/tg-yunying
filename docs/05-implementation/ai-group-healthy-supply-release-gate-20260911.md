# AI活群健康供给修复 Release Gate

## 范围与冻结

基线 origin/master=origin/release=e78472ac1f80be6b3d4c50b8375b0497ced6b402。独立工作树 codex/ai-group-fulfillment-20260911，保留主工作树及并行任务。候选以最终Git提交/prepare manifest为准；仅本地linux/amd64直传，不用Actions。未改账号授权、Session、迁移或正文目标。

## 七个切片的当前证据

| 切片 | 结论及处理 |
| --- | --- |
| 健康候选 | 当前查询在LIMIT前过滤assignment/义务open/时段，扫描可跨页；10Task只读各返回20行NULL next_eligible候选。空值不是缺陷，不补时间字段。 |
| Planner | typed资源冲突按既有retry延期，不误记运行合同错误；增加按Task/阶段/SQL调用点计时。178–241秒旧轮次尚未证明已改善，部署后读回。慢actions采样含retention查询，不把所有DB耗时归因Planner。 |
| 时间/共享占用 | 现有窗口求交回归；拒绝具体为original_task_day_unproven。样本追至8月原调用，部分仍remote_inflight；不以日期、失败状态或租约过期释放。 |
| C2 | 天津音乐/郑州大学等有post_send_intercepted，成都有observation_gap_limit_reached；这些不是成功准入。保留原状态，未伪造can_send或重放探测。 |
| 生成 | 3Task同步预关注后rev2缺当前policy binding，错误task_ai_content_policy_binding_missing；修复两个配置API原事务，并提供受审计的原authority successor。西安/楼凤当前证明引用错scope，但原binding仍有效，显式restore_original_references恢复。 |
| 存量 | 仅3Task策略绑定准备preview/hash/apply/readback；不重置旧window/Job/Action/未知结果或过期义务。未取得完整未调用证明的历史占用继续unproven。 |
| 点赞 | 消息编辑后旧source revision 50人误迁新revision 48人，修复必须精确source_identity；PG验证保留旧epoch且新数量遵守新需求。 |

## 验证和发布准入

后端编译、diff check；定向PG修复测试、配置更新生命周期、Planner隔离/并发、reaction、时间窗口/共享占用、coverage义务/扫描、准入观察恢复及应急回归，每进程60秒，专用tg_yunying_test。冻结源prepare重跑记录全部测试命令/结果、前端构建、三镜像ID/平台与包hash。当前已有61项配置/Planner回归、45项reaction/pacing/shared回归通过；最终依据制品日志。

部署前仅读取当前.image.env并同值传入支持的URL/OCR/worker变量，证据只存键名和hash。安装只派发一次，runtime与配置指纹读回通过后精确清理前一次成功镜像。随后3Task绑定精确apply及独立readback。

## 业务验收

所有10个运行group_ai_chat Task，逐Task最新ledger原due_target、typed confirmed、独立账号覆盖、发布后新消息与正常/应急质量分别记录；另核对原channel_like失败Task。运行健康和绑定落库不等于履约完成。历史缺口不会因发布归零，真实群管拦截、原调用未知、旧原窗耗尽分别列blocked/failed/unproven。

当前状态：本地修复验证中，制品未构建，未安装，未执行生产apply，业务E4未通过。后续实际结果保留在制品证据目录，不回写历史事实。
