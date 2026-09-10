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
- status: pending

## 生产操作

每项先重新preview并保留release/精确身份/指纹，操作后独立readback；不同结果分别报告persisted_verified、blocked或unproven。用户已要求修复本批诊断的问题；不扩大为账号重新绑定、任务恢复/重发或其他项目清理。
