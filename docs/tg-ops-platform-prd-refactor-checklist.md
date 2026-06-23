# TG 运营管理平台 PRD 重构实施清单

> 日期：2026-05-31（Asia/Shanghai）
> 来源：`docs/tg-ops-platform-prd.md`、`docs/tg-ops-platform.md` 与当前代码扫描。  
> 目标：把当前代码逐步重构到 PRD 确定的运营中心、任务中心、系统设置、账号中心、数据流转和前端页面设计。

---

## 0. 2026-05-23 文档升级状态

本文件是后续代码重构的执行清单。2026-05-23 的目标是先把文档面升级到同一口径；2026-05-28 补齐全站唯一目标画像口径；2026-05-31 补齐频道评论运行时异常分类和可恢复准入口径。不代表下面代码实施项已经完成；代码改造仍按 P0-P7 分批执行和验收。

| 文档面 | 本轮更新要求 | 完成判定 |
| --- | --- | --- |
| PRD | `docs/tg-ops-platform-prd.md` 的日期、更新记录、实施优先级和验收口径同步到本清单 | PRD 使用 P0-P7：基线口径、汇总读模型、账号可用性、目标画像、运营方案、运营中心、任务中心、系统设置/手册闭环 |
| 总设计 | `docs/tg-ops-platform.md` 的当前状态、导航边界、实施优先级和验收标准同步到本清单 | 总设计不再保留旧的多个 P1 优先级，明确以本清单作为代码重构执行入口 |
| 前端操作手册 | `frontend/src/app/views/AdminManualView.tsx` 同步运营中心、运营方案模板、任务动态向导、账号可用性、汇总延迟和系统设置边界 | 运营人员能在内置手册看到真实菜单、日常顺序、最近更新和异常处理入口 |
| 专项文档 | 账号安全、频道准入、规则中心、风控账号中心、素材库、容量升级等专项文档不在本轮重复展开 | PRD 和总设计只引用专项文档，避免同一细节多处漂移 |

文档升级验收：

- [x] PRD、总设计和本清单的实施阶段名称一致。
- [x] PRD、总设计和操作手册都明确：运营中心发现问题，任务中心承载执行详情，系统设置只维护平台底座。
- [x] PRD、总设计和本清单都明确：目标画像是全站唯一画像，AI 活跃群和频道评论 / 回复共用同一份画像。
- [x] PRD、总设计和操作手册都明确：先做汇总读模型和冷热数据边界，不先分库分表。
- [x] 操作手册不出现旧 Campaign、卡密、订阅套餐或多租户 SaaS 作为新主线。
- [x] 文档检查通过 `git diff --check`；若修改前端手册 TSX，必须通过 `npm --prefix frontend run build`。

---

## 1. 重构原则

- 先保留现有路由和 API 兼容，再逐步升级页面文案、响应字段和数据模型。
- 不先做分库分表；优先落地汇总读模型、冷热数据边界和按 ID 下钻。
- 运营中心是发现问题和处理异常的入口；任务中心是执行事实源；系统设置只维护平台底座。
- 每个新增按钮必须有接口、权限、审计和失败态；前端隐藏按钮不能代替后端权限校验。
- 旧 Campaign、旧 `operation_tasks`、旧 review 只做兼容，不进入新主线。

---

## 2. 当前代码现状

### 2.1 前端现状

| 区域 | 当前文件 | 当前状态 | PRD 差距 |
| --- | --- | --- | --- |
| 导航壳 | `frontend/src/app/AppShell.tsx`、`frontend/src/app/routes.ts` | 已有 14 个主导航；`overview` 显示为“运营中心”，路由 `/dashboard`；`/materials` 为素材中心一级入口 | 需新增“目标画像”一级菜单 `/target-profile`；首屏仍需从概览卡片继续收敛为目标工作台 + 运营方案 |
| 运营中心 | `frontend/src/app/views/OverviewView.tsx` | 展示账号、目标、任务、风险和趋势图 | 缺目标工作台、方案模板、异常聚合、关联任务失败抽屉 |
| 任务中心 | `TaskCenterView.tsx`、`TaskCenterWizardSections.tsx`、`TaskCenterDetailModal.tsx` | 已有 5 类任务创建、5 步向导、预检、详情、action 列表和准入摘要 | 任务列表仍以任务自身 stats 为主，缺 `task_runtime_summary`；缺 action attempt 独立下钻；创建字段仍有较多工程参数 |
| 账号中心 | `AccountsView.tsx`、`AccountModals.tsx`、`AccountSecurityBatchDrawer.tsx` | 已有账号列表、详情、安全批次、验证码、资料初始化 | 缺 `account_runtime_summary` 可用性读模型、stale 标记、按任务能力展示可发送 / 可监听 / 可加入 / 可评论 |
| 素材中心 | `MaterialsView.tsx`、`AISettingsView.tsx`、`AppShell.tsx` | 已从系统设置拆为一级菜单，复用现有素材列表、上传、批量上传、编辑、禁用 / 恢复、资产版本 / TG 引用版本、引用影响范围和缓存健康接口 | 仍需补 zip 导入、独立分组模型、素材详情抽屉、缓存刷新入口等增强项 |
| 系统设置 | `SystemConfigView.tsx` | 已有 TG 开发者应用、AI 供应商、AI 黑话、提示词与素材运行配置、后台账号权限、运行配置 Tab | 方向和 PRD 一致；继续补敏感动作原因、系统设置边界文案和素材运行配置细项 |
| 操作手册 | `AdminManualView.tsx` | 已同步运营中心日常入口、运营方案模板、任务创建动态向导、账号可用性、汇总延迟和系统设置边界 | 后续代码重构时继续随真实菜单、按钮和异常提示复核 |

### 2.2 后端现状

| 区域 | 当前文件 | 当前状态 | PRD 差距 |
| --- | --- | --- | --- |
| 任务中心 API | `backend/app/api/routers/task_center.py` | 已有 5 类具体任务创建、创建并启动、预检、详情、生命周期、actions 列表 | 缺 `GET /api/tasks/{task_id}/actions/{action_id}/attempts`；任务列表未读独立 `task_runtime_summary` |
| 任务事实表 | `backend/app/models/task_center.py` | 已有 `tasks`、`actions`、`execution_attempts`、`daily_runtime_stats`、`runtime_metric_snapshots`、listener 状态、worker 心跳 | 缺 `target_runtime_summary`、`task_runtime_summary`、`account_runtime_summary`、`operation_issue` |
| 任务执行 | `backend/app/services/task_center/*` | 已有 planner / dispatcher / listener / recovery / metrics 基础，claim、lease、unknown_after_send、容量与保留策略已有部分能力 | 需要把执行结果增量写入汇总读模型，并把失败合并成运营异常 |
| 运营目标 | `backend/app/api/routers/operations.py`、`backend/app/services/operations.py` | 已有 `operation_targets`、频道消息、频道评论、目标详情、账号覆盖 | 需要支持目标级异常、目标运行摘要、方案覆盖目标 |
| 目标画像 | 现有目标学习相关代码 | 存在按目标 / 场景组织的学习痕迹 | 需要改为全站唯一 `tenant_learning_*` 模型，旧目标级画像数据放弃，不迁移、不兼容 |
| 运营中心 | `operations_center.py`、`operations_center_*` | 已有 listener / rule / metrics 汇总能力，`/api/operation-metrics/summary` 可用 | 缺 PRD 新版运营中心的目标工作台、运营方案、运营异常 API |
| 运营方案 | 当前无完整新模型；`0019_ai_notifications_operation_plans.py` 只是旧 `operation_tasks` 增强 | 当前没有 `operation_plan_templates`、`operation_plan_targets`、`operation_plan_task_links`、`operation_plan_generation_runs` | 需要新增模型、迁移、服务、API、前端页面 |
| 账号安全 | `account_security.py`、`services/account_security` | 已有安全 summary、详情、刷新、批次预检、资料预览、批次、重试、取消 | 缺账号可用性汇总接口：`/api/tg-accounts/availability/*` |
| 权限 | `permission_middleware.py`、`frontend/src/app/utils.ts` | 已有菜单 / 接口权限；部分权限名为旧口径，如 `accounts.view_codes` | 已对齐 `accounts.codes.read`、`operation_plans.manage`、`operation_issues.manage`、`tasks.dispatch_control` 和 `materials.view/upload/manage`；后续随新增按钮继续补齐 |

### 2.3 测试现状

| 测试文件 | 当前覆盖 |
| --- | --- |
| `backend/tests/test_workflow.py` | 任务、目标、规则、监听、运营中心部分汇总、旧兼容关闭 |
| `backend/tests/test_task_center_capacity_dispatch.py` | claim、lease、账号互斥、unknown_after_send、运行明细清理 |
| `backend/tests/test_channel_membership_postgres.py` | 频道关注前置 |
| `backend/tests/test_account_security.py` | 账号安全批次、资料初始化、AI 预览和兜底 |
| `backend/tests/test_operations_center_runtime.py` | 运营中心运行汇总部分能力 |
| `frontend/package.json` | 只有 `npm run build`，暂无前端测试脚本 |

---

## 3. 总体落地顺序

| 阶段 | 目标 | 是否可并行 | 阻塞关系 |
| --- | --- | --- | --- |
| P0 基线和口径收敛 | 锁定 PRD、当前行为、权限名、导航映射 | 可独立 | 所有后续阶段前置 |
| P1 汇总读模型与运营异常 | 建表、增量汇总、stale 语义、冷热边界 | 后端优先 | P2 依赖读模型 |
| P2 账号可用性中心 | `account_runtime_summary`、可用性 API、账号列表和详情展示 | 可与 P3 并行 | 依赖 P1 的账号读模型 |
| P3 目标画像中心 | 全站唯一画像、学习来源、质量规则、样本治理、版本治理 | 可与 P2 部分并行 | 依赖运营目标、监听账号和 AI 生成入口 |
| P4 运营方案中心 | 方案模型、生成预览、生成任务、关联任务调整 | 可与 P2 / P3 部分并行 | 依赖任务创建服务和权限 |
| P5 运营中心重构 | 目标工作台、异常聚合、方案模板、下钻任务失败 | 依赖 P1 / P4 | P1 和 P4 基础完成后做 |
| P6 任务中心收敛 | 列表读摘要、详情分页、attempt 下钻、向导字段简化 | 可与 P5 部分并行 | P1 摘要表和 P2 可用性 |
| P7 手册、权限、验收闭环 | 操作手册、权限矩阵、审计、回归测试、构建 | 最后收敛 | 所有功能阶段完成后 |

---

## 4. P0 基线和口径收敛

### 4.1 导航和文案

- [x] 把 `frontend/src/app/AppShell.tsx` 中 `overview` 菜单文案从“运营概览”改为“运营中心”，路由保持 `/dashboard`。
- [x] 确认 `frontend/src/app/routes.ts` 新增 `/materials` 一级素材中心；不新增旧口径的 `素材库与 AI` 一级路由，AI、提示词、素材运行配置、后台账号权限继续归入 `/system-config`。
- [x] 更新 `frontend/src/app/views/AdminManualView.tsx`：菜单名统一为“运营中心”，说明 AI / 素材 / 后台权限在系统设置 Tab。

验收：

- [x] 登录后左侧菜单显示“运营中心”。
- [x] 旧 `/dashboard` 链接仍能打开同一页面。
- [x] 没有新增与 PRD 冲突的 `素材库与 AI` 一级菜单；已新增 PRD 要求的“素材中心”一级菜单。

### 4.2 权限名对齐

- [x] 梳理 `permission_middleware.py` 当前权限名和 PRD 矩阵差异。
- [x] 确认是否保留旧权限别名：`accounts.view_codes` -> `accounts.codes.read`。
- [x] 新增后端权限名和已有路由规则：`operation_plans.manage`、`operation_issues.manage`、`tasks.dispatch_control`、`accounts.security.read`、`accounts.security.batch`、`accounts.profile.batch_update`、`accounts.sensitive.read`、`audit.export`。其中运营方案 / 运营异常 API 尚未实现，先完成权限名注册。
- [x] 更新前端 `VIEW_PERMISSION` 和按钮级 `hasPermission` 使用点。

验收：

- [x] 普通运营人员看不到系统配置和危险动作按钮。
- [x] 越权直接调用后端写接口返回 403，并写审计。
- [x] 验证码、2FA、导出、手动 drain、任务删除 / 重置都要求权限和原因。

P0-A 已落地：导航文案、系统设置一级路由边界、权限新名兼容层、验证码 / 账号安全 / 资料批次 / 审计导出的后端权限规则和前端按钮权限已更新。P0-B 已落地：任务中心按 `tasks.manage` / `tasks.dispatch_control` 控制普通动作和重置动作；停止、删除、重置、手动 drain、验证码查看 / 同步、审计导出都要求填写原因并写审计；运营管理员默认模板不再授予 `tasks.dispatch_control`，系统管理员仍可通过全量权限执行调度控制。当前未发现可见前端 drain 按钮，如后续出现必须按该权限控制并要求原因。

### 4.3 基线测试

- [x] 后端跑现有回归：`PYTHONPATH=backend pytest -q`。
- [x] 前端跑构建：`cd frontend && npm run build`。
- [x] 固化失败项，区分“当前已有失败”和“本轮重构引入失败”。

2026-05-23 已在 PostgreSQL 测试库跑通后端全量回归：`PYTHONPATH=backend pytest -q`，结果 `275 passed, 14 skipped, 76 warnings in 576.73s`。本轮同时通过 `python -m compileall backend/app`、`git diff --check` 和 `npm --prefix frontend run build`；Vite 仅保留既有 chunk size warning。

---

## 5. P1 汇总读模型与运营异常

### 5.1 数据模型和迁移

新增迁移，建议从 `0043_runtime_summary_models.py` 开始：

- [x] `target_runtime_summary`：目标级状态、open issue 数、失败 action 数、影响任务数、最近失败时间、摘要 JSON、更新时间。
- [x] `task_runtime_summary`：任务级 planned / success / failed / pending / oldest pending / latest failure type。
- [x] `account_runtime_summary`：账号可发送、可监听、可加入、可评论、可修改资料、可读取验证码、剩余容量、不可用原因、下次重试时间、失败趋势。
- [x] `operation_issue`：目标、异常类型、严重级别、来源任务、代表 action、影响账号、失败码、可读原因、建议动作、状态。
- [x] 为 `tenant_id + target_id + status`、`tenant_id + task_id`、`tenant_id + account_id`、`tenant_id + issue_type + failure_type + status` 建索引。

验收：

- [x] 空库迁移成功。
- [x] 老库迁移成功，不删除 `actions`、`execution_attempts`、`runtime_metric_snapshots`。
- [x] 汇总表允许重算，不作为唯一事实源。

### 5.2 汇总服务

新增或改造：

- [x] `backend/app/models/runtime_summary.py` 或并入 `models/task_center.py`，但模型命名必须清晰。
- [x] `backend/app/schemas/runtime_summary.py`，定义目标、任务、账号、异常读模型响应。
- [x] `backend/app/services/runtime_summary.py`，提供 `refresh_task_summary`、`refresh_target_summary`、`refresh_account_summary`、`upsert_operation_issue`、`resolve_operation_issue`。
- [x] 在 `task_center/stats.py` 或 metrics drain 中调用汇总服务，避免每次页面打开实时扫大表。
- [x] 新增 `rebuild_runtime_summaries(session, tenant_id, scope)`，给修复任务和后台按钮使用。

验收：

- [x] action 成功 / 失败后，任务、目标、账号摘要可增量更新。
- [x] 同一目标同一 `issue_type + failure_type` 在窗口内合并为同一条 `operation_issue`。
- [x] 任务恢复成功后 issue 可进入 `resolved`，但 action 和 attempt 事实仍保留。

### 5.3 冷热数据边界

- [x] 复核 `runtime_retention.py` 当前清理逻辑，确认清理前写入 `daily_runtime_stats`。
- [x] 保证 `unknown_after_send` 和未处理人工项不被普通保留周期清理。
- [x] 在任务详情和运营中心显示汇总更新时间和 stale 状态。

---

## 6. P2 账号可用性中心

### 6.1 后端接口

新增或扩展：

- [x] `GET /api/tg-accounts/availability/summary`：账号列表页汇总能力。
- [x] `GET /api/tg-accounts/{account_id}/availability`：账号详情 Tab。
- [x] `POST /api/tg-accounts/availability/rebuild`：管理员重算。

实现建议：

- [x] 复用 `services/account_capacity.py`、账号状态、代理状态、最近 action 失败趋势。
- [x] 继续补安全快照、风控结果的专用来源（FloodWait / SlowMode 已纳入 `next_retry_at`）。
- [x] 过期汇总只做页面提示；任务预检、创建并启动、Dispatcher claim 前继续实时校验。

验收：

- [x] 在线账号但代理不可用时，`send_available=false` 且 `unavailable_reason` 明确。
- [x] FloodWait / SlowMode 有 `next_retry_at`。
- [x] 账号列表不会为了可用性实时扫描 `actions` 全表。

### 6.2 前端账号中心

- [x] `frontend/src/app/types/accounts.ts` 增加 `AccountAvailabilitySummary`。
- [x] `AccountsView.tsx` 列表增加可发送、可监听、可加入、可评论、容量、不可用原因、汇总更新时间。
- [x] `AccountModals.tsx` 增加“可用性”Tab 或补全现有详情展示。
- [x] stale 时展示“汇总可能延迟”和刷新入口。

验收：

- [x] 从运营中心账号异常跳入账号详情时默认打开对应 Tab。
- [x] 无权限用户看不到敏感状态明细。

P2 已落地账号可用性读模型 API、账号列表展示、详情可用性 Tab、重算入口、stale 提示、敏感状态按权限脱敏，以及运营中心异常跳转默认打开可用性 Tab。后端可用性已纳入账号状态、session、代理状态、容量冷却、FloodWait / SlowMode 专用 `next_retry_at`、安全快照阻断、账号安全批次待重试时间、近 24 小时失败趋势和最近风控预检结果；任务预检、创建并启动、Dispatcher claim 仍走实时校验，不依赖过期汇总。

---

## 7. P3 目标画像中心

### 7.1 数据模型和初始化

新增：

- [ ] `tenant_learning_profiles`
- [ ] `tenant_learning_sources`
- [ ] `tenant_learning_samples`
- [ ] `tenant_learning_quality_rules`
- [ ] `tenant_learning_profile_versions`
- [ ] `tenant_learning_runs`

保留规则：

- [ ] 目标画像是全站唯一画像，不按目标、任务或频道拆分多份画像。
- [ ] 旧 `target_id + profile_scene` 画像数据放弃，不迁移、不合并、不兼容。
- [ ] 首次打开目标画像页时创建空版本，状态显示“未学习 / 样本不足”。

### 7.2 后端服务和接口

新增：

- [ ] `GET /api/target-profile`
- [ ] `PATCH /api/target-profile/settings`
- [ ] `GET /api/target-profile/usage`
- [ ] `GET /api/target-profile/source-candidates`
- [ ] `GET /api/target-profile/sources`
- [ ] `PUT /api/target-profile/sources`
- [ ] `POST /api/target-profile/sources/{source_id}/sync`
- [ ] `POST /api/target-profile/sources/{source_id}/pull-history`
- [ ] `GET /api/target-profile/runs`
- [ ] `GET /api/target-profile/runs/{run_id}`
- [ ] `GET /api/target-profile/samples`
- [ ] `PATCH /api/target-profile/samples/{sample_id}`
- [ ] `GET /api/target-profile/quality-rules`
- [ ] `PATCH /api/target-profile/quality-rules`
- [ ] `POST /api/target-profile/recompute-candidates`
- [ ] `POST /api/target-profile/rebuild`
- [ ] `GET /api/target-profile/versions`
- [ ] `POST /api/target-profile/versions/{version_id}/restore`
- [ ] `POST /api/target-profile/clear`

验收：

- [ ] 来源候选返回运营目标、可监听状态、监听账号覆盖、最近消息时间、推荐原因和不可自动同步原因。
- [ ] 自动同步、历史拉取、候选重算和画像重建都写 `tenant_learning_runs`，失败原因和 trace_id 可见。
- [ ] 样本质量规则支持身份过滤、文本过滤、广告模板过滤、质量评分阈值、场景权重和禁学模式。
- [ ] 质量规则变更不会静默改写生效画像，必须显式重算候选并重建画像。

### 7.3 前端目标画像页面

- [ ] 新增 `/target-profile` 路由和一级菜单。
- [ ] 页面展示当前画像、使用范围、学习来源、同步状态、样本状态、质量规则和版本状态。
- [ ] 支持选择学习来源和监听账号；不可自动同步来源必须明确标记原因。
- [ ] 支持查看候选样本，并对样本采纳、降权、剔除、填写原因。
- [ ] 支持历史拉取、候选重算、画像重建、版本恢复和清空画像；危险动作必须二次确认并写审计。

验收：

- [ ] 运营目标详情只展示“是否作为学习来源”和跳转入口，不出现画像版本、样本治理、重建、清空或质量规则编辑。
- [ ] AI 活跃群、频道评论和频道回复读取同一画像版本；任务页只能展示当前画像状态，不能选择另一份画像。
- [ ] Prompt 拼装分层传入事实、任务配置、全站画像、账号画像和规则约束；画像不能成为具体事实来源。

---

## 8. P4 运营方案中心

### 8.1 数据模型和迁移

新增：

- [x] `operation_plan_templates`
- [x] `operation_plan_targets`
- [x] `operation_plan_task_links`
- [x] `operation_plan_generation_runs`

保留规则：

- [x] 不复用旧 `operation_tasks` 做新方案主表。
- [x] 方案只生成或调整 `tasks`，不直接生成 `actions`。
- [x] 每次生成预览、生成任务、应用关联任务都写 `operation_plan_generation_runs` 和审计。

### 8.2 后端服务和接口

新增：

- [x] `backend/app/models/operation_plans.py`
- [x] `backend/app/schemas/operation_plans.py`
- [x] `backend/app/services/operation_plans.py`
- [x] `backend/app/api/routers/operation_plans.py`

接口：

- [x] `GET /api/operation-plans`
- [x] `POST /api/operation-plans`
- [x] `GET /api/operation-plans/{plan_id}`
- [x] `PATCH /api/operation-plans/{plan_id}`
- [x] `POST /api/operation-plans/{plan_id}/generate-preview`
- [x] `POST /api/operation-plans/{plan_id}/generate-tasks`
- [x] `POST /api/operation-plans/{plan_id}/apply-to-linked-tasks`
- [x] `POST /api/operation-plans/{plan_id}/pause`
- [x] `POST /api/operation-plans/{plan_id}/resume`
- [x] `POST /api/operation-plans/{plan_id}/copy`
- [x] `POST /api/operation-plans/{plan_id}/archive`
- [x] `GET /api/operation-plans/{plan_id}/runs`

验收：

- [x] 方案保存不创建 task。
- [x] 生成预览返回预计任务、目标、账号容量、准入动作、阻塞原因。
- [x] 生成任务成功后写 `tasks` 和 `operation_plan_task_links`。
- [x] 调整运行中任务前必须返回影响预览，确认后才更新任务配置。
- [x] 暂停、恢复、复制和归档方案必须写运行记录和审计；复制方案不复制既有关联任务。

### 8.3 前端运营方案区域

- [x] 在 `OverviewView.tsx` 或拆新组件 `OperationCenterView.tsx`，增加下半部分“运营方案 / 策略模板”。
- [x] 新增方案卡片、方案编辑抽屉、生成预览抽屉、影响预览确认。
- [x] 方案卡片展示运行状态、最近效果、关联任务、最近异常。
- [x] 支持从目标生成方案，并带入目标类型和推荐任务类型。
- [x] 方案卡片支持暂停 / 恢复、复制和归档生命周期操作。

验收：

- [x] 运营人员可以从运营中心完成“新建方案 -> 生成预览 -> 生成并启动任务”。
- [x] 任务中心详情能看到来源方案或关联方案 ID。

P4 已落地方案主表、目标绑定、任务关联和生成运行记录；运营中心可按目标快捷创建默认方案、编辑方案、生成预览、生成草稿、生成并启动任务、暂停 / 恢复、复制、归档，并在应用到关联任务前展示影响预览、要求填写原因和二次确认。生成预览已返回预计任务、目标、账号容量、准入动作和阻塞原因，并在运营中心用抽屉展示。

---

## 9. P5 运营中心重构

### 9.1 后端运营中心 API

新增或扩展：

- [x] `GET /api/overview` 返回新版运营中心摘要：目标异常数、运行中任务、失败 action、受影响账号、最近更新时间。
- [x] `GET /api/operation-issues`：默认按目标聚合，支持过滤 `target_id`、`issue_type`、`severity`、`status`、`failure_type`。
- [x] `GET /api/operation-issues/{issue_id}`：异常详情、关联任务失败、账号影响、来源明细、建议动作。
- [x] `POST /api/operation-issues/{issue_id}/claim`
- [x] `POST /api/operation-issues/{issue_id}/acknowledge`
- [x] `POST /api/operation-issues/{issue_id}/resolve`
- [x] `POST /api/operation-issues/{issue_id}/ignore`

验收：

- [x] 打开旧 `/api/overview` 仍会保留历史 24h 趋势扫描；新版 `/api/operation-center/overview` 不扫描 `actions` / `execution_attempts` 全表。
- [x] 点开目标异常能看到关联任务失败，再跳任务中心详情。
- [x] 忽略和解决异常必须填写原因，并写审计。
- [x] 异常返回 `handling_mode`、`return_to`、`claimed_by / claimed_at`、`affected_task_count / affected_account_count`；来源和影响账号分别进入 `operation_issue_sources` / `operation_issue_accounts`。

### 9.2 前端运营中心页面

- [x] 把 `OverviewView.tsx` 从图表概览改为“上半部分目标工作台，下半部分运营方案”。
- [x] 目标工作台读取 `target_runtime_summary + operation_targets`。
- [x] 异常抽屉展示 `operation_issue`、关联任务、影响账号、建议动作。
- [x] 支持跳转任务详情、账号详情、目标详情、规则中心、风控中心。
- [x] 保留趋势图作为次级区域，不再占据首屏核心。
- [x] 下半部分已接入运营方案 / 策略模板列表、快捷创建、预览和生成任务入口。

验收：

- [x] 运营人员不进任务中心也能看到失败目标。
- [x] 任务中心仍保留完整 action / attempt 事实。
- [x] 汇总延迟时页面展示最近更新时间。

---

## 10. P6 任务中心收敛

### 10.1 任务列表和详情

- [x] `list_tasks` 默认读取 `task_runtime_summary`，不实时 search 全量 action。
- [ ] `GET /api/tasks/{task_id}` 收敛为只读首屏摘要，返回 `membership_subtask`、`task_runtime_summary`、目标、规则、账号摘要和分页入口，不再 broad-fetch 全量 action / membership / attempt。
- [x] `GET /api/tasks/{task_id}/actions` 保持分页、过滤、排序。
- [x] 新增 `GET /api/tasks/{task_id}/actions/{action_id}/attempts`。
- [x] action 返回是否已上卷 `operation_issue`。

验收：

- [x] 任务列表不加载频道消息详情和全量 action。
- [ ] 生产 `/task-center` 点击详情时，首屏请求不 broad-fetch 全量 action、membership 或 attempt，详情弹窗先展示摘要，再分页加载执行计划、执行记录和准入账号。
- [x] attempt 展开只查询单个 action 的尝试记录。
- [x] `failure_type`、可读原因、原始错误入口、trace_id 可见。
- [ ] 频道评论 / 回复运行时异常按“未准入可恢复、账号级不可评论、消息级不可评论、其他原始错误”分类展示；未准入必须先补关注 / 加入再重试，消息级不可评论展示“该消息无法评论”。

### 10.2 创建向导

- [x] 保持 5 步：基础信息、目标来源、任务配置、账号与节奏、预检确认。
- [x] 每类任务只展示必要字段，把抖动、静默账号数、爬坡、上下文过期等放入高级折叠。
- [x] `target_input` 只允许创建链路使用；编辑任务不允许隐式创建目标。
- [x] 预检确认页展示已满足、可准备、不可准备、预计准入动作、预计耗时、阻塞 / warning。
- [x] AI 活跃群创建和编辑配置展示“每轮最少引用回复数”，默认 0，位置紧跟“每轮计划发言数”，不放入高级折叠。
- [x] AI 评论创建和编辑配置展示“每条消息最少引用回复数”，默认 0，位置紧跟“预计每条评论 / 回复”，不放入高级折叠。
- [x] 引用回复数量字段前端校验必须阻止非法提交：AI 活跃群不得大于当前每轮计划数，AI 评论不得大于每条评论 / 回复目标；不得静默改小用户输入。
- [x] 预检确认页展示引用回复摘要和引用不足 warning；不展示引用来源选择器或具体消息多选框。

验收：

- [x] 5 类任务字段差异清楚。
- [x] 创建并启动时后端重新预检。
- [x] 未加入 / 未关注账号不被提前排除，而是进入可准备容量。
- [x] 创建、编辑、预检确认和详情页均按 PRD 展示引用回复配置与结果。

### 10.3 AI 活跃群质量

- [x] 在 `GroupAIChatConfig` 或运行配置中补接话优先、空闲暖场、事实锚点、语义重复窗口、低置信沉默字段。
- [x] 改造 `task_center/ai_generator.py` 和 `executors/group_ai_chat.py`：真人上下文优先，空闲才低频暖场。
- [x] 输出记录必须包含模式、事实锚点、语义簇、重复风险、幻觉风险、沉默原因。
- [x] 没有上下文和素材锚点时不编造“上次体验”“位置确认”“回访”“准点”等事实。
- [x] AI 活跃群新增每轮最少引用回复数配置；Planner 在本轮 Turn 数内拆出引用回复 Turn，不额外抬高总 Turn。
- [x] AI 活跃群引用池自动混合当前目标群可回复上下文消息和同任务历史成功发送消息；不提供真人消息 / 自己历史消息范围选择。
- [x] AI 活跃群普通发言和引用回复使用不同 Prompt；引用回复生成前必须绑定具体被回复消息，并把作者、原文和当前上下文传给 AI。
- [x] AI 活跃群 `send_message` payload、详情页 AI Turn、Action 明细和执行尝试展示引用关系字段。
- [x] Dispatcher 对 AI 活跃群引用回复只执行 payload 中的 `reply_to_message_id`，不得执行时临时选择引用对象或静默普通发送。
- [x] Listener 采集群上下文时保证远端消息 ID、作者、内容预览和发送时间可用于引用池；缺远端 ID 的上下文不进入引用池。
- [x] Metrics 增加 AI 活跃群引用回复计划数、成功数、引用对象不足数和 Telegram 回复失败数。

验收：

- [x] 真人聊天后 AI 能接话。
- [x] 无人时低频暖场，不刷屏。
- [x] 重复、无锚点、幻觉风险会跳过并留痕。
- [x] 配置最少引用回复数后，action payload 和任务详情能看到 `reply_to_message_id`、引用作者、引用预览、来源和执行结果。
- [x] 引用对象不足时不静默降级为普通发言，任务详情展示可引用消息不足或引用回复规划不足。

### 10.4 AI 评论引用回复规划

- [x] 频道评论新增每条频道消息最少引用回复数配置；Planner 在单条频道消息本轮补差额内拆出引用回复 action，不额外抬高评论总目标。
- [x] 频道评论引用池自动混合当前频道消息讨论区已采集评论和同任务历史成功评论；不提供真人评论 / 自己历史评论范围选择。
- [x] 频道直接评论和引用回复使用不同 Prompt；引用回复生成前必须绑定具体被回复评论，并把频道原文、评论作者、评论原文和讨论区上下文传给 AI。
- [x] 执行层使用 Telegram 原生 `reply_to`，不得用文本引用或执行时临时决定引用关系。
- [x] 频道评论 `post_comment` payload、频道消息子任务聚合、Action 明细和执行尝试展示引用关系字段。
- [x] Listener / 频道评论采集保证 `comment_message_id`、作者、内容预览、父评论 ID 和发布时间可用于引用池；缺评论 ID 的记录不进入引用池。
- [x] Metrics 增加频道评论引用回复计划数、成功数、引用对象不足数和 Telegram 回复失败数。

验收：

- [x] 配置每条消息最少引用回复数后，`post_comment` action 至少对应数量带 `reply_to_message_id`，并计入同一频道消息累计目标。
- [x] 任务详情展示引用评论作者、引用预览、引用来源、Telegram 远端消息 ID 和失败原因。
- [x] 引用对象不足时不静默降级为普通评论，任务详情展示可引用评论不足或引用回复规划不足。

---

## 11. P7 系统设置、手册和最终验收

### 11.1 素材中心和系统设置

- [x] 新增 `MaterialsView.tsx` 一级“素材中心”，承载素材总览、表情包、头像包、图片 / 文件、上传入口和缓存健康。
- [x] 新增 `materials.view`、`materials.upload`、`materials.manage` 权限，并从 `system.view/system.manage` 中拆出素材日常接口权限。
- [x] 素材中心支持禁用 / 恢复素材；被消息、动作、规则版本、运营方案和账号资料批次引用的素材只做状态停用，不做物理删除。
- [x] 素材列表展示资产版本、TG 引用版本和引用影响范围，后端返回 `reference_summary` / `referenced_by_count`。
- [x] 保持 `SystemConfigView.tsx` Tab：TG 开发者应用、AI 供应商、AI 黑话、提示词与素材运行配置、后台账号权限、运行配置。
- [x] 系统设置数据按 Tab 加载；首屏只取当前 Tab 必要数据，不再进入系统设置就全量读取账号、AI、提示词和素材资源。
- [ ] 素材运行配置支持填写缓存频道链接、`@username` 或 `t.me/c/...` 链接，后端解析为执行层 peer；普通管理员不需要手动知道 `-100...` 内部 ID，并兼容 `.env` 回退。
- [x] 系统设置只保存底座配置，不出现群活跃方案、频道互动节奏、目标异常处理。
- [x] 业务页“去配置”跳转到系统设置对应 Tab，不直接写底座配置。

### 11.2 操作手册

- [x] 更新 `AdminManualView.tsx` 最近更新功能。
- [x] 补“运营中心日常处理顺序”：看目标异常 -> 展开关联任务 -> 处理账号 / 目标 / 规则 -> 回到运营中心确认。
- [x] 补“运营方案模板”：保存、预览、生成任务、调整关联任务。
- [x] 补“汇总延迟”：看到 stale 时如何刷新和下钻。
- [x] 补“账号可用性”：可发送、可监听、可加入、可评论的含义。

### 11.3 测试和构建

后端新增测试：

- [x] `test_runtime_summary_models.py`
- [x] `test_operation_issues.py`
- [x] `test_operation_plans.py`
- [x] `test_account_availability.py`
- [x] `test_task_action_attempts_api.py`

前端验收：

- [x] `cd frontend && npm run build`
- [x] 运营中心首屏人工截图检查。
- [x] 任务创建 5 类路径人工走查。
- [x] 账号中心可用性和安全批次人工走查。

后端验收：

- [x] `PYTHONPATH=backend pytest -q`
- [x] PostgreSQL 空库迁移验证。
- [x] 1000 账号容量脚本重新生成报告。

2026-05-23 PostgreSQL 验收说明：`backend/tests/conftest.py` 会在测试开始前 `DROP SCHEMA public CASCADE` 并重建 schema，随后应用启动按 `AUTO_MIGRATE_ON_START=true` 自动迁移到当前模型，已覆盖空库迁移和全量后端回归。1000 账号容量报告已通过 `backend/scripts/run_capacity_benchmark.py` 重新生成。旧库漂移场景已通过 `0046_repair_admin_tables` 修复缺失的 `app_users` / `user_token_ledgers`，当前开发库升级到 `0046_repair_admin_tables` 后确认 `actions`、`execution_attempts`、`runtime_metric_snapshots` 仍存在；运营中心首屏已用 Playwright 登录态检查，目标工作台、目标异常、素材中心菜单和趋势区可见，控制台 error/warning 为 0。

2026-05-23 前端与任务路径验收说明：任务创建 5 类路径通过 `test_task_center_group_ai_chat_creates_and_dispatches_actions`、`test_task_center_group_relay_auto_executes_and_dedupes`、`test_task_center_channel_view_like_comment_execute` 覆盖 AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论创建、启动和 drain 执行。账号中心通过 Playwright 登录态访问 `/accounts`，确认可用性重算、资料初始化、设置二步密码、清理登录设备入口可见，选择账号后能打开“批量清理登录设备”安全批次抽屉，控制台 error 和 404 均为 0。

---

## 11. 风险和切分建议

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| 一次性改运营中心、任务中心和数据模型 | 回归面过大 | P1 先建读模型，P5 再改页面 |
| 目标画像混回运营目标详情 | 用户误解成每个群 / 频道各有一份画像 | P3 独立一级页面，运营目标详情只展示来源状态和跳转入口 |
| 权限名直接替换 | 老用户按钮丢失 | 先做权限别名和迁移，再切新名 |
| operation issue 误删失败事实 | 任务详情无法追溯 | issue 只做聚合状态，不删除 action / attempt |
| 方案调整覆盖运行中任务 | 线上任务行为突变 | 必须影响预览 + 二次确认 + 审计 |
| 账号可用性汇总过期 | 错误调度账号 | 页面可读汇总，预检和 claim 实时重算 |
| AI 暖场刷屏或幻觉 | 群体验变差 | 默认真人接话优先、空闲低频、无锚点沉默 |

---

## 12. 推荐执行批次

### 批次 A：不改变业务行为的基础收敛

- [x] 导航文案、手册口径、权限名兼容。
- [x] 新增汇总表迁移和只读 rebuild，不接入页面主流程。
- [x] 增加后端测试。

### 批次 B：账号和任务读模型接入

- [x] 任务列表读 `task_runtime_summary`。
- [x] 账号列表读 `account_runtime_summary`。
- [x] action attempt 单独接口。
- [x] stale 状态前端展示。

### 批次 C：运营异常和运营中心改版

- [x] Metrics / Recovery 写 `operation_issue`。
- [x] 运营中心目标工作台。
- [x] 异常详情和关联任务失败。

### 批次 D：运营方案模板

- [x] 方案模型和接口。
- [x] 方案生成预览。
- [x] 生成任务和关联任务调整。
- [x] 运营中心下半部分方案区。

### 批次 E：AI 活跃群质量和最终体验

- [x] 真人接话 / 空闲暖场判断。
- [x] 语义去重和事实锚点。
- [x] 质量字段留痕。
- [x] 前端任务创建字段再瘦身。

---

## 13. 当前不做

- 不做分库分表。
- 不把 AI 供应商、AI 黑话和提示词维护拆成一级菜单；素材中心已按 PRD 拆为一级菜单。
- 不把任务中心改成运营人员唯一入口。
- 不把旧 `operation_tasks` 继续扩成新任务主线。
- 不在编辑任务时通过 `target_input` 隐式创建新目标。
- 不让运营方案直接生成 action。
