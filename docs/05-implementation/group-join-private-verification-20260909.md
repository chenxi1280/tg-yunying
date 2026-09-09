# 入群审批私聊验证修复 Release Gate

- intake_id: intake-20260909-content-screen-and-admission
- level: L3
- release_mode: github_actions
- release_owner / rollback_owner: 当前任务执行者
- status: local_gate_passed_pending_candidate_ci
- branch: codex/content-screen-and-admission-20260909
- base: 8ade5020c1a3172fb4c5f6dfd2666dedd61bcdea
- 原工作区e5cf7fc8及未跟踪诊断文档保持，本切片使用独立worktree。

## 问题与修复

实际审批申请被当作普通权限失败，真实管理员bot在私聊发题的路径没有接入；已有算法只读群内验证或提前转人工。新路径捕获Telegram待审批异常，在申请前冻结管理员bot和私聊游标，处理新发的同目标数学callback题，只有独立成员读回通过后进入原权限与C2。超时、未知协议、错群、非管理员、回执丢失均保留不可重放状态，不伪造成功、不重发申请。

用户澄清业务是正常活群及通用引擎测试。第三方历史内容命中不能单独判定业务用途；已撤回整群隔离/任务暂停实现，生产未应用这类变更。保留实际广告文本的来源/上下文/学习/发送/API正文筛查，规则有语义和媒体覆盖边界。

## Product Design Complete与代码审查

已按真实账号515收到的私聊协议更新群管准入PRD、频道成员前置设计、内容筛查专项、主PRD与两个索引。原账号、租户、稳定群peer、管理员bot、申请前游标、题目时效、单次callback、成员及发言事实、失败和回滚路径闭合；本次仅支持已实测的二整数数学选择题，不增加任意URL/私聊命令或频道关注动作。

自审修正：待审批不进入旧拉人/解禁/重复加入组合；callback ACK不能标joined；成功成员仍需发言检查；响应丢失不重放；默认关闭Gateway私聊自动执行，仅正式Task开关开启时传入；普通非审批群不读私聊；确定性内容规则移除“出台”“包夜”等单独可表示正常业务的歧义词。

## 本地验证

- 新私聊协议、RPC边界、内容筛查与上下文/频道来源定向测试：最终与以下PostgreSQL回归合并运行：136通过，耗时28.11秒；语法和git diff --check通过。
- PostgreSQL群成员准入23通过；目标权限、unknown数据流、成员transport、频道成员与只读成员证据44通过。
- PostgreSQL为本地临时独立实例，测试库按项目合同命名tg_yunying_test；未连接生产测试。每个pytest进程硬超时60秒。
- migration_impact: none；frontend_build: not_required（没有前端改动）；worker_impact: 新group成员申请私聊验证与内容筛查；external_platform_impact: 原Task开关授权的单题callback。
- rollback_plan: 应用可回到此前兼容版本；新增证据为JSON字段，无迁移。既有unknown/待审批不能通过回滚重试或清除。

## 实际测试证据与未完成项

- t01ces当前无审批和机器人，未用于审批成功验收。
- zzxshxc账号515在13:30收到申请已提交及管理员bot私聊数学选择题；原Gateway将其记为失败/群无权限/remote_mutation_started=null。成员读回为false。
- 人工核对期间该60秒题目过期；没有点击、没有重发。原申请与审计保留。
- 账号518经只读GetParticipant确认已是成员；其统一引擎代理路径另有连接失败，不能用直连诊断成功证明该路径恢复。
- 尚无新版本Task/Action/Attempt→callback→成员→发言E4，不得声明production_fixed。
- candidate SHA、CI、部署、独立SHA/runtime和线上验收在对应步骤完成后追加。

- 最终本地Gate：136通过、0失败（现有Alembic配置弃用提示1条）；本切片无前端/迁移变更。
