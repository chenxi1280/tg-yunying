# AI 活群预关注频道任务创建修复

## Intake Card

- message_id: `2026-08-23-ai-group-prejoin-form-001`
- intake_id: `intake-2026-08-23-ai-group-prejoin-form`
- level: `L2`
- lane: `task-center/ai-group/prejoin-config`
- 原始需求：恢复 AI 活群任务创建时直接配置“需要关注的频道地址”，运行时先关注频道，再入群并处理机器人验证；同时检查其他同类配置断层，不扩展数据安全范围。
- 成功口径：创建、编辑、详情回读、后端独立字段持久化和现有运行时消费链全部连通；相同断层完成横向审计；本地 QA、发布及线上静态读回有明确证据。

## Product Design Complete

- 用户输入：0～3 个公开 `https://t.me/<username>`、`@username` 或公开 username。
- 服务端合同：归一化成 username 引用、去重；拒绝私密邀请链接、消息地址、非 Telegram 域名和超量输入。
- 持久化：只写 `tasks.group_ai_prejoin_channel_ids` 独立字段，不写 `type_config` 或历史 `required_channel_refs`。
- 流程：配置频道全部关注成功后，继续既有入群、可信群管提示和机器人验证状态机。
- 前端状态：创建、编辑回填、编辑保存、复核页、详情页均显示同一字段。
- 数据安全：本次不新增产品侧权限、脱敏或安全能力；沿用现有任务权限边界。
- design_status: `complete`

## 实现与防回归

- 后端 schema、创建服务、通用设置更新、专用 AI 配置更新、详情读模型已接入独立字段。
- 前端任务向导、create/update payload、编辑回填、复核和详情已接入。
- 后端覆盖输入归一化、非法输入、创建持久化、两条更新路径和 revision。
- 前端源码契约覆盖创建、编辑、回读和类型声明，防止该字段再次只剩运行时而从页面消失。

## 横向审计

- `Task` 表所有任务类型专属独立字段中，仅 `group_ai_prejoin_channel_ids` 属于“运行时直接消费、创建配置应暴露”的字段；本次已闭环。
- 其余任务类型专属创建配置均走 `type_config`，已有对应 create schema、前端 payload 和现有源码契约测试；未发现第二个同类“数据库/运行时存在但创建页与 API 同时缺失”的断层。
- 数据安全项按用户范围未纳入本轮横向扩展。

## QA / Release Gate

- backend_targeted: `186 passed, 2 deselected`（相关 `no_postgres` 组合回归）；独立字段定向用例 `6 passed`。
- frontend_contract: 新增预关注频道端到端源码契约 `1 passed`，并包含在上述 186 项中。
- frontend_build: `tsc --noEmit + vite build` 通过，3170 modules transformed；仅保留既有大 chunk 警告。
- static_check: 改动涉及的 schema、details 和配置归一化测试 Ruff 通过；`service.py` 与全量前端契约文件仍有既存未使用 import/变量及历史 E402，不属于本次改动。
- diff_check: `git diff --check` 与改动后端模块 `compileall` 通过。
- migration_impact: `none`，复用既有 JSON 字段。
- worker_impact: `none`，复用既有 `ensure_prejoin_channels` 执行链。
- release_mode: `github_actions`
- rollback_plan: 回滚本次提交并按正常 `master -> release` 发布；已有任务字段和远端关注事实不删除。
- candidate_sha: `513e9937c7c3e6ef9d484f31eef138d18997641f`
- release_sha: `b46497b9133289c158f7175bc133fcc82f9dba7e`
- actions_run: `32587573326`，frontend、两组 backend、三镜像 build、SSH deploy 与 active shared dispatch verify 全部成功。
- deployed_sha: `b46497b9133289c158f7175bc133fcc82f9dba7e`；生产 backend、workers 和 image verification worker 均以该镜像运行且 healthy，release current 指向 `202608***1737***_b46497b9`。
- production_probe: 公网 `/api/health` 返回 `ok`；当前 `/task-center` 加载 `TaskCenterView-DZZdRfTV.js`，其中“需要关注的频道地址”1 次、`group_ai_prejoin_channel_ids` 13 次、“预关注频道”3 次；线上 OpenAPI 的 `GroupAIChatTaskCreate`、`TaskSettingsUpdate`、`TaskOut` 均包含该字段。
- status: `production_surface_fixed`
- unproven: 未新建真实生产任务，因而没有新增任务行持久化和 Telegram 实际关注频道证据；本次修复的“创建页字段缺失”已由生产 bundle 与 API 合同读回闭环。
