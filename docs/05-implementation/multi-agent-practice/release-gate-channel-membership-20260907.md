# 2026-09-07 频道强制关注与来源预约修复 Release Gate

- intake_id: channel-membership-review-five-fixes-20260907
- level: L2
- release_mode: github_actions
- release_owner: Codex（当前用户已授权提交和部署）
- rollback_owner: 同本次发布负责人
- status: local_passed / pending_ci_and_production
- candidate: 包含本文件的 master 提交，发布前冻结完整 SHA；master/release 必须相同。
- base_sha: 3ed0e062953873cc8b9123b6da0f2bcd38d3abf6
- production_before: /data/tgyunying/releases/20260906155215_3ed0e062

## 上线范围

频道点赞/浏览先关注；规划和 Gateway 前检查成员关系。关注排程窗口内抖动与最小间隔、1~6 小时配置链路、频道 Gateway 两并发和账号 60 秒冷却。移除来源节奏强制压缩，保持稳定预约。交付显式 tenant/state、只读快照、漂移拒绝与审计回读的失效预约回收工具。

相关产品合同为 `channel-membership-precondition-design.md` §14；同步 hourly pacing PRD、结构与数据流索引。根目录未跟踪的 `scripts/` 临时诊断脚本不属于发布产品代码，保留原文件。

## 本地与 CI 闸门

- backend_tests: 前序 171 个独立定向用例通过（166 no-PostgreSQL、5 PostgreSQL）。发布前独立并发测试与普通 PostgreSQL 用例混跑 14 项通过；复用测试会话 advisory lock，避免完整 CI 重复取锁。
- frontend_build: TypeScript + Vite 已通过，代码未再发生功能变更。
- static: 编译与 git diff --check 必须通过；新增模块不超过 500 行，函数不超过 50 行。
- ci_or_build: Deploy Production 冻结候选验证、3 个 no-PostgreSQL 分片、2 个 PostgreSQL 分片、前端检查和镜像构建全部成功后才部署。
- migration_impact: 无新增迁移；当前线上 0226_task_retirement 保持。
- worker_impact: 按正式脚本更新 backend、planner、dispatcher 等既有角色，Gateway 准入新增数据库锁及未结束 Attempt 计数；不另行启动 drain 或 retry。
- external_platform_impact: 正常运行任务中的未关注账号将进入关注前置动作；不修改任务账号范围、目标、日数量或结果事实，不重放 unknown。
- rollback_plan: 保留旧不可变 release；禁止回滚业务数据、降级 0226 或恢复已退役任务。默认向前修复；应用回退前单独核验旧版本兼容和业务影响。

## 发布与线上验证

1. 提交 master 并推送，release 仅快进至同一 SHA。
2. 仅以 release ref 手动触发 Deploy Production，所有额外生产变更开关保持默认 false。
3. 独立读回 current 路径、各发布角色 RELEASE_SHA、health、迁移版本和公网健康。
4. 以此次发布完成时间作为观察锚点，对当前运行点赞/浏览任务读取 Task -> 义务/Action -> Attempt/Gateway -> reaction_observed/view_observed；关注关系与成员准入 Attempt 分别核对。
5. observe_window: 发布后首次扫描，并按实际关注排程观察；2 小时是首次尝试排程窗口，不是 Telegram 审批或全部任务完成承诺。无 typed fact 时保留 production_unproven。
6. 恢复脚本不随部署自动 apply；本次部署授权不包含生产预约清理。

## 首轮 CI 回流与修复

- 首轮候选 `2d6ce01363cac1b2d22691cbf85cc1a5aef54550`，Actions run `34111422118`：候选校验与前端成功，五个后端分片暴露 116 个旧测试前置/断言冲突；镜像与生产部署未执行。
- 根因分组：频道级授权不能替代账号关注；已加入的成员前置审计记录不能计入主互动；未关注主动作现在延后并创建准入，不能期待直接成功或永久失败；PG 场景清理遗漏频道关系外键。
- 修复仅更新测试夹具、主动作类型查询和上述预期，保留生产强制关注与原业务断言；补齐未关注主动作 Attempt/Gateway/远端事实为零的验证。
- 定向复测覆盖首轮失败场景，另对共享夹具影响的完整模块回归 92 项通过；接口/PG 竞态/未关注事实链组合 16 项通过。新的最终候选仍须通过完整 Actions 质量门后部署。
