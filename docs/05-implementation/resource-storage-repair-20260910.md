# 资源存储修复交接与 Release Gate

- intake_id / approval_ref: resource-storage-repair-20260910
- level: L3
- release_mode: local_direct + manual_ops
- owner / merge_owner / release_owner: 本任务
- isolated_checkout: /private/tmp/tgyunying-resource-storage-repair-20260910
- source_base: 093930751c1d90e2e177aebbbe211264f55d1a2f
- locked_paths: 本批新增证据模型/服务/迁移/维护脚本/测试；engagement_planning_admission、account_assignment_snapshot、engagement_reaction_capacity、restore_mihomo_runtime及相关索引文档。
- 保留主工作区daily_coverage_planning、group_ai_chat与其他未提交文件，不纳入本批。

## Product → Dev

PRD已完成原需求、Redis取舍、数据归一化、在线卷保护、并发/租户/回滚、历史转换及验收合同。R3反查新增人格读取优化已resync，不改变资格或业务时效。设计complete。

## Dev → QA

共享证据采用复合租户FK、只读ORM关联，避免关联赋值改写snapshot租户。旧JSON保留混合读；无引用缺失的空结果回退。历史转换不删除业务行，并比较完整内容/身份hash。维护默认preview，实际apply必须带清单和审计身份；卷备份逐内容hash去重并验证可还原字节。

## Release Gate

- 定向业务/表示转换回归：75项通过（15.92s）。
- 卷/索引/恢复入口定向QA：25项通过（2.65s）。
- 真实PG：全迁移至0232、并发内容唯一、真实外键历史转换/租户隔离，2项通过（9.76s）。最初发现的测试seed/清理FK问题已修正为真实父子顺序，未放宽生产约束。
- 自审：规范化保留原hash/顺序/值，租户复合FK与viewonly关联防身份串改；非内容跳过人格查询，内容仍用最新失败版本；卷保护覆盖volume和bind父/子路径，root-only备份验证后精确删除。compile与diff-check通过。
- 发布：master→release→本地prepare（每pytest批次60s及前端build、三镜像）→SSH安装；禁止Actions发布。
- 迁移：additive0232；开始新引用后仅forward fix，不直接回滚旧代码。
- E4/资源：生产版本与健康、共享证据实际写入与历史hash守恒、卷实际回收、无效索引读回、CPU/可用内存/swap同口径采样，分层记录。
- status: runtime_released_resource_partial

## 生产操作

每项先重新preview并保留release/精确身份/指纹，操作后独立readback；不同结果分别报告persisted_verified、blocked或unproven。用户已要求修复本批诊断的问题；不扩大为账号重新绑定、任务恢复/重发或其他项目清理。

# 资源存储修复生产验证（2026-09-10）

## 结论

本批已发布应用优化并实际回收主机空间；整体状态为 **partial**，不能写 production_fixed。数据库历史全量归一化、无效索引删除、CPU/交换压力尚未全部解决。

## 发布与代码

- 应用运行 SHA：`e454c82a13bfe43c2986ac385b376e9a836e471a`；迁移 `0232_admission_evidence`。
- 应用修复：准入路径按租户+内容摘要共享，保留原快照身份和完整逻辑内容；仅需要账号集合时不加载大JSON；非内容任务跳过人格查询，内容任务仍使用最新版本。
- 本地直接发布曾在创建两个 Dispatcher 后因 SSH 断开而中断。保留原 deployment_unproven 记录；核对计划1145450、镜像、迁移、指纹与preparing状态后，仅启动两个已创建容器并完成原计划，未重装或重放prepare。
- 独立读回19个应用/Worker容器镜像、SHA与健康通过；合同 active_verified，激活审计1146144。前端/API、OCR真实推理、planner smoke通过。34项运行配置指纹保持不变。
- 运维脚本修订 `a5704e16`：识别Docker标准匿名卷标签。已推送master/release；该独立人工维护脚本发布在审计目录，以SHA256 `a094626de57d26f5e3a94b30c3cf187dcbd96a53434f9a3a147bc198e17c3feb`核对后执行。应用镜像仍是e454c82a，不能把a5704e16写为应用运行SHA。
- 发布前102项测试通过；标签修订另有17项定向回归通过（其中6项新增），均在60秒内。真实PG迁移、并发唯一与租户FK验证通过。

## 实际空间回收

- 删除177个未被任何容器/绑定路径引用的匿名Mihomo卷：源文件5,451,450,612 bytes（5.08GiB）。
- 删除前逐文件备份、解压后SHA256校验；相同内容去重为6个blob，共9,347,136 bytes。审计与备份保留在服务器 `/data/tgyunying/audits/resource-storage-repair-20260910/volumes-apply/`。
- 独立读回177个卷均不存在，58个受保护运行容器的运行/挂载未改变。96个geo文件与在线基线不一致的卷保留，未扩大删除范围。
- 发布验证后删除上一版本未被容器引用的backend/OCR镜像，保留当前及共用前端镜像；上传归档清理完成，image-cleanup=cleanup_passed。未执行全局prune。
- 系统盘：15:42使用27,123,632KiB/70%，16:21使用21,809,092KiB/56%；净减少5,314,540KiB（5.07GiB），可用约16.40GiB。净变化包含部署归档/镜像/备份及同期运行写入，不能与卷源字节简单相加。

## 数据库与内存边界

- 历史清单33371条；已提交并逐批完整hash/身份校验900条（9批，末审计1146174）。维护进程转换100条约55秒，容器内存升至1.118GiB，因此中断精确维护进程，当前未完成事务回滚，保留已验证900条。未继续高负载全量转换。
- 16:21独立读回：33483个快照，1012个共享引用、32471个legacy，604条共享证据，缺失引用0。由历史转换900条与线上新写入112条组成；线上新写入已使用新路径。
- 普通VACUUM(ANALYZE)完成，耗时0.82秒；未执行VACUUM FULL。
- 数据库21,104,181,775→21,163,737,615 bytes，约增加56.8MiB；快照物理表未缩小。不能声称本轮已回收数据库磁盘，已更新旧JSON空间仅可能复用。
- 无效索引`ix_actions_hard_hourly_history_scheduled_ccnew`原OID7771154，181.45MiB，valid=false/ready=true。正式索引OID7771192保持valid/ready=true。精确受审计的DROP INDEX CONCURRENTLY触发2秒lock_timeout，未完成，独立读回原索引仍在。
- 后续只读采样发现PID107271为超过20分钟的idle-in-transaction，backend_xmin仍持有；未终止未归属业务会话。该长事务是索引清理/数据库回收的当前风险，未以延长锁等待或重启业务掩盖。
- 主机可用内存666→1759MiB，但swap1346→2027MiB，短时CPU仍有满核突发，且重启/维护影响样本。不能声明持续CPU或内存压力已全部修复。PolarDB独立云实例内存指标未取得，应用主机free不能代表数据库内存。
- 最近10分钟planner、双Dispatcher、backend日志中未观察到traceback、缺失证据、不可变字段或完整性错误。本资源验证不代表Telegram业务履约。

## Redis结论

本轮不将Task/Action/Attempt、远端发送防重、准入权威证据或GB级冷历史迁到同机Redis。Redis适合有明确失效版本、可重建的展示聚合或纯函数结果，但本批没有证明此类缓存是根因；当前主机仍有交换压力，直接迁移会把磁盘问题变成内存和一致性问题。已优先消除重复存储和无用大字段读取。

## 证据位置

本地：`/private/tmp/tgyunying-resource-evidence-20260910/`，含server-before/after、database-before/after、volumes-readback、vacuum、完整compact审计日志、release-e454c82a的准备/测试/中断/恢复/独立读回证据。原主工作区及其他Agent未提交代码保持不动。
