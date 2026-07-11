# TG 运营管理平台 PRD 重构实施清单

> 日期：2026-05-31（Asia/Shanghai）
> 来源：`docs/01-product/tg-ops-platform-prd.md`、`docs/01-product/tg-ops-platform.md` 与当前代码扫描。
> 目标：把当前代码逐步重构到 PRD 确定的运营中心、任务中心、系统设置、账号中心、数据流转和前端页面设计。

---

## 0. 2026-05-23 文档升级状态

本文件是后续代码重构的执行清单。2026-05-23 的目标是先把文档面升级到同一口径；2026-05-28 补齐全站唯一目标画像口径；2026-05-31 补齐频道评论运行时异常分类和可恢复准入口径。不代表下面代码实施项已经完成；代码改造仍按 P0-P7 分批执行和验收。

| 文档面 | 本轮更新要求 | 完成判定 |
| --- | --- | --- |
| PRD | `docs/01-product/tg-ops-platform-prd.md` 的日期、更新记录、实施优先级和验收口径同步到本清单 | PRD 使用 P0-P7：基线口径、汇总读模型、账号可用性、目标画像、运营方案、运营中心、任务中心、系统设置/手册闭环 |
| 总设计 | `docs/01-product/tg-ops-platform.md` 的当前状态、导航边界、实施优先级和验收标准同步到本清单 | 总设计不再保留旧的多个 P1 优先级，明确以本清单作为代码重构执行入口 |
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

P0-A 已落地：导航文案、系统设置一级路由边界、权限新名兼容层、验证码 / 账号安全 / 资料批次 / 审计导出的后端权限规则和前端按钮权限已更新。P0-B 已落地：任务中心按 `tasks.view` / `tasks.manage` / `tasks.dispatch_control` 控制普通读取、普通动作和重置动作；停止、删除、重置、手动 drain、验证码查看 / 同步、审计导出都要求填写原因并写审计；兼容期的旧 `operation-tasks`、`operation-task-attempts`、`manual-operation-records` 和 `review-queue` 接口也必须走同一任务权限边界；旧 `campaigns` 和 `ai-drafts` 兼容接口按手动发送权限边界读取和写入；群聊救援配置保存按系统底座写操作归属 `system.manage`；当前登录用户自助修改密码必须有顶部入口和后端 `POST /api/auth/change-password` 支撑、校验当前密码并写审计；运营管理员默认模板不再授予 `tasks.dispatch_control`，系统管理员仍可通过全量权限执行调度控制。当前未发现可见前端 drain 按钮，如后续出现必须按该权限控制并要求原因。

### 4.3 基线测试

- [x] 后端跑现有回归：`PYTHONPATH=backend pytest -q`。
- [x] 前端跑构建：`cd frontend && npm run build`。
- [x] 固化失败项，区分“当前已有失败”和“本轮重构引入失败”。
- [x] 测试基础设施区分 `no_postgres` 源码 / 契约 / 数据流测试和 PostgreSQL 集成测试：前者不得在收集阶段连接远端测试库，后者仍 fail closed 且错误可见。

2026-05-23 已在 PostgreSQL 测试库跑通后端全量回归：`PYTHONPATH=backend pytest -q`，结果 `275 passed, 14 skipped, 76 warnings in 576.73s`。本轮同时通过 `python -m compileall backend/app`、`git diff --check` 和 `npm --prefix frontend run build`；Vite 仅保留既有 chunk size warning。

---

## 5. P1 汇总读模型与运营异常

### 5.1 数据模型和迁移

新增迁移，建议从 `0043_runtime_summary_models.py` 开始：

- [x] `target_runtime_summary`：目标级状态、open issue 数、失败 action 数、影响任务数、最近失败时间、摘要 JSON、更新时间。
- [x] `task_runtime_summary`：任务级 planned / success / failed / pending / oldest pending / latest failure type；latest failure 覆盖 `failed`、`retryable_failed` 和 `unknown_after_send`。
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

- [x] `tenant_learning_profiles`
- [x] `tenant_learning_sources`
- [x] `tenant_learning_samples`
- [x] `tenant_learning_quality_rules`
- [x] `tenant_learning_profile_versions`
- [x] `tenant_learning_runs`

保留规则：

- [x] 目标画像是全站唯一画像，不按目标、任务或频道拆分多份画像。
- [x] 旧 `target_id + profile_scene` 画像数据放弃，不迁移、不合并、不兼容。
- [x] 运行时采集、监听学习刷新、频道评论同步和任务生成只读写 `tenant_learning_*` 全站画像表，不再新写 `target_learning_*` 旧表。
- [x] 首次打开目标画像页时创建空版本，状态显示“未学习 / 样本不足”。

### 7.2 后端服务和接口

新增：

- [x] `GET /api/target-profile`
- [x] `PATCH /api/target-profile/settings`
- [x] `GET /api/target-profile/usage`
- [x] `GET /api/target-profile/source-candidates`
- [x] `GET /api/target-profile/sources`
- [x] `PUT /api/target-profile/sources`
- [x] `POST /api/target-profile/sources/{source_id}/sync`
- [x] `POST /api/target-profile/sources/{source_id}/pull-history`
- [x] `GET /api/target-profile/runs`
- [x] `GET /api/target-profile/runs/{run_id}`
- [x] `GET /api/target-profile/samples`
- [x] `PATCH /api/target-profile/samples/{sample_id}`
- [x] `GET /api/target-profile/quality-rules`
- [x] `PATCH /api/target-profile/quality-rules`
- [x] `POST /api/target-profile/recompute-candidates`
- [x] `POST /api/target-profile/rebuild`
- [x] `GET /api/target-profile/versions`
- [x] `POST /api/target-profile/versions/{version_id}/restore`
- [x] `POST /api/target-profile/clear`

验收：

- [x] 来源候选返回运营目标、可监听状态、监听账号覆盖、最近消息时间、推荐原因和不可自动同步原因。
- [x] 自动同步、历史拉取、候选重算和画像重建都写 `tenant_learning_runs`，失败原因和 trace_id 可见。
- [x] 学习来源保存必须后端校验 `listener_account_ids` 属于当前租户和来源目标候选覆盖，跨租户、离线、已删除或不在覆盖关系中的监听账号不得写入。
- [x] 样本质量规则支持身份过滤、文本过滤、广告模板过滤、质量评分阈值、场景权重和禁学模式。
- [x] 质量规则变更不会静默改写生效画像，必须显式重算候选并重建画像。

### 7.3 前端目标画像页面

- [x] 新增 `/target-profile` 路由和一级菜单。
- [x] 页面展示当前画像、使用范围、学习来源、同步状态、样本状态、质量规则和版本状态。
- [x] 支持选择学习来源和监听账号；不可自动同步来源必须明确标记原因。
- [x] 支持查看候选样本，并对样本采纳、降权、剔除、填写原因。
- [x] 支持历史拉取、候选重算、画像重建、版本恢复和清空画像；危险动作必须二次确认并写审计。

验收：

- [x] 运营目标详情只展示“是否作为学习来源”和跳转入口，不出现画像版本、样本治理、重建、清空或质量规则编辑。
- [x] AI 活跃群、频道评论和频道回复读取同一画像版本；任务页只能展示当前画像状态，不能选择另一份画像。
- [x] Prompt 拼装分层传入事实、任务配置、全站画像、账号画像和规则约束；画像不能成为具体事实来源。
- [x] 推荐学习来源只能默认高亮，不能默认勾选；没有已选来源时，前端不得把推荐候选写入选中状态。

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

2026-07-11 生产核心页面性能修复已完成本地实现、QA 与产品验收；下列实现项可按本地证据勾选，但发布与真实 E4 项仍不得提前勾选：

- [x] 新增 `GET /api/tasks/page?page=&page_size=&type=&status=&q=&group_key=`，返回 `TaskListPageOut(items,total,page,page_size,summary,groups)`，第一方任务列表停止使用旧全量 `/api/tasks`。
- [x] 普通 Task 与四类账号安全系统任务使用统一轻量投影，在共同集合上稳定排序、过滤、计数和分页；顺序固定为 `priority ASC, created_at DESC, source_kind ASC, stable_id DESC`。
- [x] 任务列表 item 不返回完整 `account_config/pacing_config/failure_policy/type_config`；打开详情 / 编辑时再按 `/api/tasks/{task_id}` 读取完整配置，不得逐行补详情。
- [x] `summary={total,running,failed}` 与 `groups` 均在 `type/status/q` 后、当前 `group_key` 和分页前生成；顶层 `total` 在应用 `group_key` 后生成，不能由当前页 rows 反推全局统计或分组。
- [x] 账号安全系统任务对候选 batch IDs 一次性 SQL 聚合 items 状态和最近失败，消除按 batch 加载 items 的 N+1；普通任务目标 / 频道摘要和 runtime summary 同样批量补齐。
- [x] 任务中心分页、type/status/q/group_key、手动刷新、写后刷新和 60 秒轮询均绑定当前查询与请求序号；旧响应不得覆盖最新 rows、total、summary、groups、loading 或 error。
- [x] 新接口失败必须展示后端 detail / trace_id 并保留显式失败状态，不得静默回退旧 `/api/tasks`。
- [x] `list_tasks` 默认读取 `task_runtime_summary`，不实时 search 全量 action。
- [x] `GET /api/tasks/{task_id}` 收敛为只读首屏摘要，返回 `membership_subtask`、`task_runtime_summary`、目标、规则、账号摘要和分页入口，不再 broad-fetch 全量 action / membership / attempt。
- [x] `GET /api/tasks/{task_id}/actions` 保持分页、过滤、排序。
- [x] 新增 `GET /api/tasks/{task_id}/actions/{action_id}/attempts`。
- [x] action 返回是否已上卷 `operation_issue`。

验收：

- [x] 任务列表不加载频道消息详情和全量 action。
- [x] 生产 `/task-center` 点击详情时，首屏请求不 broad-fetch 全量 action、membership 或 attempt，详情弹窗先展示摘要，再分页加载执行计划、执行记录和准入账号。
- [x] attempt 展开只查询单个 action 的尝试记录。
- [x] `failure_type`、可读原因、原始错误入口、trace_id 可见。
- [x] 准入 action 为 `unknown_after_send` 时，任务详情准入摘要必须展示结果未知 / 等待人工确认，不得聚合成 completed 或 ready。
- [x] 准入前置汇总不得把所有 `skipped` 计为成功；`permission_denied` / 验证 / 人工处理类 skipped 必须显示为阻塞或失败，但历史 `error_code=already_joined` 必须继续计为 ready，不能让任务详情误判 membership ready 或误伤旧成功数据。
- [x] 准入 action 为 `unknown_after_send` 时，账号明细 payload 必须 `manual_required=true`，人工处理筛选不能漏掉结果未知账号。
- [x] 任务详情顶部 stats 从准入摘要回填时必须包含 `unknown_after_send_count`，不得漏掉准入结果未知数量。
- [x] 准入汇总源头必须把 `unknown_after_send` 独立计数并排除出 need_join / failed / estimated_membership_actions，避免为结果未知账号自动重建准入动作。
- [x] 运营概览、规则中心指标和转发归因报表必须把 `unknown_after_send` 作为未闭环异常计入失败/风险口径，不得把它算作普通 pending 或从失败详情中漏掉。
- [x] 风控中心展示 `runtime.unknown_after_send` 时，风控详情必须能下钻到对应 `unknown_after_send` action，不能只展示 failed/skipped 明细。
- [x] 目标运行汇总的 `failed_action_count`、`affected_task_count`、`latest_failure_at` 必须覆盖 `failed`、`retryable_failed`、`unknown_after_send`，不得只看普通 failed。
- [x] 运营异常详情的 recent actions 必须覆盖 `failed`、`retryable_failed`、`unknown_after_send`，不得让 `unknown_after_send` issue 没有可见代表 action。
- [x] 群聊准入快照 item 关联的入群 action 为 `unknown_after_send` 时，必须同步为等待人工确认并保留原始 `unknown_after_send` 失败类型，不能长期停留在 joining。
- [x] 群聊准入快照 item 关联的测试发言 action 为 `unknown_after_send` 时，必须同步为等待人工确认并保留原始 `unknown_after_send` 失败类型，不能长期停留在 testing_message。
- [x] 群聊准入快照 item 关联的删除测试消息 action 为 `unknown_after_send` 时，必须同步 `delete_status=unknown_after_send` 并保留未知结果详情，不能长期显示为 deleting。
- [x] 群聊准入快照 item 关联的救援 action 为 `unknown_after_send` 时，必须同步 `rescue_status=unknown_after_send` 并保留未知结果详情，不能长期显示为 pending。
- [x] 任务详情准入账号表必须把删除 / 救援的 `unknown_after_send` 展示为“结果未知”，不得裸露枚举或显示成“未触发”。
- [x] AI 活跃群连续权限失败计数遇到 `unknown_after_send` 必须断开，不能把未知结果前后的失败合并后提前触发救援。
- [x] 频道评论 / 回复运行时异常按“未准入可恢复、账号级不可评论、消息级不可评论、其他原始错误”分类展示；未准入必须先补关注 / 加入再重试，消息级不可评论展示“该消息无法评论”。

### 10.2 创建向导

- [x] 任务创建 / 编辑弹窗先打开，再懒加载运营目标；远程搜索使用 `q`，编辑态已选目标使用 `ids` 回显并按 ID 去重，目标读取失败在弹窗内可见。
- [x] 任务弹窗目标候选始终显式携带 `page/page_size`，不得把“懒加载”实现为延迟执行全量 `/operation-targets`。
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
- [x] AI 活跃群普通发言候选不足时，不按实际返回数量静默少建 action；任务 stats / last_error 必须展示 AI 候选不足。
- [x] 硬小时目标延迟 AI 批量补文案时，候选不足不得部分写入 `ai_generation_status=success`；当前 action 必须失败可见并记录候选不足 stats。
- [x] AI 活跃群生成预览请求 `count=N` 时，候选不足不得返回短列表；接口必须返回 AI 候选不足错误。

验收：

- [x] 真人聊天后 AI 能接话。
- [x] 无人时低频暖场，不刷屏。
- [x] 重复、无锚点、幻觉风险会跳过并留痕。
- [x] 配置最少引用回复数后，action payload 和任务详情能看到 `reply_to_message_id`、引用作者、引用预览、来源和执行结果。
- [x] 引用对象不足时不静默降级为普通发言，任务详情展示可引用消息不足或引用回复规划不足。
- [x] 普通发言 AI 候选少于请求 Turn 时，本轮不创建部分 action，并展示 AI 普通发言候选不足。
- [x] 延迟 AI 批次候选少于 pending action 数时，不产生部分成功 payload，并展示 AI 普通发言候选不足。
- [x] AI 活跃群预览候选少于请求数量时，页面收到明确错误而不是短列表。

### 10.4 AI 评论引用回复规划

- [x] 频道评论新增每条频道消息最少引用回复数配置；Planner 在单条频道消息本轮补差额内拆出引用回复 action，不额外抬高评论总目标。
- [x] 频道评论引用池自动混合当前频道消息讨论区已采集评论和同任务历史成功评论；不提供真人评论 / 自己历史评论范围选择。
- [x] 频道直接评论和引用回复使用不同 Prompt；引用回复生成前必须绑定具体被回复评论，并把频道原文、评论作者、评论原文和讨论区上下文传给 AI。
- [x] 执行层使用 Telegram 原生 `reply_to`，不得用文本引用或执行时临时决定引用关系。
- [x] 频道评论 `post_comment` payload、频道消息子任务聚合、Action 明细和执行尝试展示引用关系字段。
- [x] Listener / 频道评论采集保证 `comment_message_id`、作者、内容预览、父评论 ID 和发布时间可用于引用池；缺评论 ID 的记录不进入引用池。
- [x] Metrics 增加频道评论引用回复计划数、成功数、引用对象不足数和 Telegram 回复失败数。
- [x] 频道普通评论候选不足时，不按实际返回数量静默少建 action；任务 stats / last_error 必须展示 AI 评论候选不足。
- [x] 频道评论生成预览请求 `count=N` 时，候选不足不得返回短列表；接口必须返回 AI 评论候选不足错误。

验收：

- [x] 配置每条消息最少引用回复数后，`post_comment` action 至少对应数量带 `reply_to_message_id`，并计入同一频道消息累计目标。
- [x] 任务详情展示引用评论作者、引用预览、引用来源、Telegram 远端消息 ID 和失败原因。
- [x] 引用对象不足时不静默降级为普通评论，任务详情展示可引用评论不足或引用回复规划不足。
- [x] 普通评论 AI 候选少于请求数量时，本轮不创建部分 action，并展示 AI 评论候选不足。
- [x] 频道评论预览候选少于请求数量时，页面收到明确错误而不是短列表。

---

## 11. P7 系统设置、手册和最终验收

### 11.1 素材中心和系统设置

- [x] 新增 `MaterialsView.tsx` 一级“素材中心”，承载素材总览、表情包、头像包、图片 / 文件、上传入口和缓存健康。
- [x] 新增 `materials.view`、`materials.upload`、`materials.manage` 权限，并从 `system.view/system.manage` 中拆出素材日常接口权限。
- [x] 素材中心支持禁用 / 恢复素材；被消息、动作、规则版本、运营方案和账号资料批次引用的素材只做状态停用，不做物理删除。
- [x] 素材列表展示资产版本、TG 引用版本和引用影响范围，后端返回 `reference_summary` / `referenced_by_count`。
- [x] 素材详情页读取基础详情、引用记录和版本记录时必须分别保留成功结果；任一接口失败不得清空其他已成功数据，失败原因必须显式展示。
- [x] 保持 `SystemConfigView.tsx` Tab：TG 开发者应用、AI 供应商、AI 黑话、提示词与素材运行配置、后台账号权限、运行配置。
- [x] 系统设置数据按 Tab 加载；首屏只取当前 Tab 必要数据，不再进入系统设置就全量读取账号、AI、提示词和素材资源。
- [x] 页面刷新失败不得静默降级为空数组、空对象或旧汇总；全局快照、运营中心、规则中心、归档中心和消息发送目标加载必须显式暴露 API 错误。
- [x] 运营中心异常处理失败必须展示后端错误 detail 或响应正文，不得只提示泛化“失败”。
- [x] 运营中心方案创建、预览、生成任务、暂停 / 恢复 / 复制 / 归档、保存、关联任务影响预览、应用关联任务和异常处理失败必须展示后端错误 detail 或响应正文，不能形成不可见 Promise rejection。
- [x] 运营中心方案创建、预览、生成任务、暂停 / 恢复 / 复制 / 归档、保存、关联任务影响预览、应用关联任务和异常处理成功后的运营中心数据刷新失败必须提示“运营中心数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 运营中心首屏加载、手动刷新和方案 / 异常动作后的运营中心数据刷新必须绑定运营中心数据请求序号；连续刷新或多个方案 / 异常动作交错完成时，旧刷新响应不得覆盖最新方案列表、目标列表、运营中心摘要、目标运行汇总、异常列表、loading 或错误提示。
- [x] 运营中心方案创建、预览、生成草稿、生成并启动、暂停 / 恢复 / 复制 / 归档、保存、关联任务影响预览和确认应用必须绑定当前方案动作 key；连续触发不同方案动作或切换影响预览抽屉时，旧动作响应不得清空当前按钮 loading、覆盖当前预览 / 影响结果或展示旧动作错误。
- [x] 运营中心方案保存必须绑定当前 plan_id + payload 签名、保存请求序号和方案编辑抽屉会话；切换方案、关闭 / 重开编辑抽屉或修改方案表单后，旧保存响应不得关闭当前抽屉、覆盖提示或触发旧 payload 成功刷新。
- [x] 运营中心应用关联任务必须绑定当前 plan_id + reason + confirm_apply 签名、应用请求序号和影响预览抽屉会话；切换影响预览方案、关闭 / 重开抽屉或修改确认原因后，旧应用响应不得覆盖当前影响结果、提示或触发旧原因成功刷新。
- [x] 消息发送列表行操作失败必须展示后端错误 detail 或响应正文；发送任务创建 / 批量创建、立即执行、重试和取消不能只依赖页面局部提示或形成未捕获 Promise。
- [x] 消息发送页按账号读取联系人和运营目标必须分别保留成功结果；联系人接口失败不得清空已成功读取的运营目标，运营目标接口失败不得清空已成功读取的联系人，失败原因必须显式展示。
- [x] 消息发送页按账号读取联系人和运营目标、以及定时刷新运营目标时必须绑定发起时的 account_id + 请求序号；快速切换发送账号或定时刷新与账号切换交错完成时，旧账号响应不得覆盖当前联系人、运营目标、loading 或错误提示。
- [x] 消息发送页发送前风控预检必须绑定请求序号和发送 payload 签名；预检返回前修改账号、目标、内容、素材或定时时间时，旧预检结果不得打开确认弹窗、覆盖当前预检结果或展示旧错误。
- [x] 消息发送页确认提交必须绑定已通过风控预检的发送 payload 签名；确认弹窗打开后修改账号、目标、内容、素材、发送方式或定时时间时，前端必须阻止提交并提示重新预检，不得用旧预检结果提交新 payload 或让确认弹窗展示内容与实际提交 payload 不一致。
- [x] 消息发送页定时刷新发送记录和基础快照失败必须在页面内展示后端错误 detail 或响应正文，不能用未处理的 `void onRefresh()` 形成不可见 Promise rejection。
- [x] 消息发送页临时素材创建成功后的基础数据刷新失败必须展示“刷新消息发送数据失败”，不得误报为“创建素材失败”，并保留本地已创建素材。
- [x] 消息发送私发任务创建、发送任务创建 / 批量创建、取消、派发、到期队列处理和重试成功后的消息发送数据刷新或账号详情刷新失败必须提示“消息发送数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 监听转发任务 AI 润色源群消息失败时必须让本轮监听转发失败并写入可见错误，不得用代码轻改写、模板文案、mock 或本地规则生成已批准 draft / message task 抵扣成功。
- [x] 监听转发任务目标群没有可用发送账号时必须让本轮监听转发失败并写入可见错误，等待账号恢复后重试，不得把 `queued=0` 标记为成功或静默吞掉源消息。
- [x] 账号健康检查、账号同步和账号 / 账号池私发任务创建失败必须展示后端错误 detail 或响应正文，不能只结束 loading 而无失败提示。
- [x] 账号删除、账号分组创建 / 移动、克隆计划创建 / 执行 / 重试、验证辅助处理、联系人同步、账号全量同步、账号资料保存 / 重试、健康检查和账号同步成功后的账号列表、账号详情、账号池详情或群详情刷新失败必须提示“账号中心数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 账号新增成功、验证码登录 / 扫码登录启动成功，以及验证码 / 2FA / 扫码检查推进登录状态成功后的账号列表或账号详情刷新失败必须提示“账号中心数据刷新失败”并说明原操作已完成，不得写进登录表单错误或误报为新增、验证码校验、2FA 校验、扫码检查失败。
- [x] 账号列表页可用性汇总读取、可用性重算和批量同步安全状态失败必须在账号页内展示后端错误 detail 或响应正文，不能只保留旧汇总或等待汇总。
- [x] 账号列表页可用性重算和批量同步安全状态成功后的账号可用性汇总刷新失败必须提示“账号中心数据刷新失败”并说明原操作已完成，不得误报为可用性重算或安全状态刷新失败，也不得静默保留旧汇总。
- [x] 账号列表页可用性汇总读取、账号列表变化触发的刷新，以及可用性重算 / 批量同步安全状态后的汇总刷新必须绑定账号可用性请求序号；连续刷新或账号列表变化与写动作交错完成时，旧汇总响应不得覆盖最新可用性 Map、loading 或错误提示。
- [x] 账号详情读取、验证码入口、账号池详情、账号分组创建 / 移动、克隆计划创建 / 执行 / 重试、联系人同步、账号全量同步、群详情读取、验证码同步、资料保存和资料同步重试失败必须展示后端错误 detail 或响应正文，不能只依赖外层 loading。
- [x] 账号中心资料保存必须绑定发起时的 account_id + profile payload + avatar file 签名和保存请求序号；头像上传或资料保存返回前切换账号详情、关闭 / 重开资料弹窗、修改昵称、TG 姓名、简介、头像对象或头像文件时，旧保存响应不得把旧表单提交到新账号、关闭当前弹窗、覆盖提示、触发旧 payload 成功刷新或清空新保存 busy。
- [x] 账号详情内联系人同步、账号全量同步和资料同步重试必须绑定发起时的 account_id + action 和请求序号；同步返回前切换账号详情或触发另一账号同步时，旧同步响应不得刷新新账号详情、覆盖当前提示、触发旧账号成功刷新或清空新同步 busy。
- [x] 账号详情内移动分组、克隆计划创建、克隆计划执行和克隆项重试必须绑定发起时的 account_id + action 和请求序号；动作返回前切换账号详情或重复触发同类动作时，旧响应不得刷新新账号详情、切换新账号详情 Tab、重开当前弹窗、覆盖当前提示或清空新动作 busy。
- [x] 账号登录弹窗中的验证码登录启动 / 重发、扫码登录启动 / 检查、验证码提交和二步密码提交必须绑定发起时的 account_id + action + 请求序号；请求返回前切换登录账号、重新选择登录方式或再次提交时，旧响应不得覆盖当前登录表单、错误提示、notice、弹窗状态、账号详情刷新或全局 busy。
- [x] 账号详情打开、验证码入口、移动分组入口和刷新详情必须绑定当前 account_id 与请求序号；账号池详情打开 / 刷新必须绑定当前 pool_id 与请求序号；群详情打开 / 刷新必须绑定当前 group_id 与请求序号。旧异步响应不得覆盖当前详情、默认 Tab、弹窗类型或全局 busy 状态。
- [x] 账号中心深链打开账号详情失败时，打开动作必须返回明确失败结果；外层 `/accounts?account_id=...` 深链不得在详情未成功打开时继续切换账号详情 Tab。
- [x] 账号详情弹窗内可用性读取 / 重算、账号安全读取 / 刷新 / 批次创建、同步目标和手动发送失败必须展示后端错误 detail 或响应正文，不能只清除局部 loading。
- [x] 账号详情弹窗内验证聊天读取、重新读取和验证回复提交必须绑定当前 account_id + verification_task_id + 弹窗会话；读取失败必须在账号详情弹窗内展示后端错误 detail 或响应正文，不能形成未捕获 Promise；关闭或切换验证任务后，旧响应不得写入当前验证聊天、清空当前回复或关闭当前弹窗。
- [x] 账号详情弹窗内同步目标和手动发送完成后，后续账号详情刷新失败必须独立提示“刷新账号详情失败”，不得误报为同步目标失败或手动发送失败。
- [x] 账号详情弹窗内可用性读取 / 重算、账号安全读取 / 刷新和账号安全批次创建必须绑定当前 account_id；旧账号异步响应不得覆盖当前账号详情 Tab 的数据、loading 或错误提示。
- [x] 账号安全批次抽屉的预检 / AI 资料预览、重抽全部和批次创建必须绑定发起时的 payload 签名与请求序号；调整账号、动作、资料策略、备用 session 策略或原因后，旧预检 / 创建响应不得覆盖当前预检、批次结果、loading、确认弹窗或步骤状态。
- [x] 账号详情授权资产和托管 2FA 面板内，授权读取、备用登录准备 / 启动 / 校验 / QR 检查、主授权切换、托管 2FA 保存 / 轮换失败必须展示后端错误 detail 或响应正文。
- [x] 账号详情授权资产和托管 2FA 面板必须绑定当前 account_id；旧账号异步响应不得覆盖当前账号授权资产、备用登录表单、托管 2FA 输入、loading、错误提示或成功提示。
- [x] 账号授权资产读取、手动刷新、备用登录完成后的授权资产刷新和切换主授权后的授权资产刷新必须绑定当前 account_id + 请求序号；同一账号连续刷新、备用登录完成刷新或切换主授权交错完成时，旧授权资产响应不得覆盖当前授权资产、loading 或错误提示。
- [x] 托管 2FA 保存和轮换必须绑定当前 account_id + action + 请求序号 + payload 签名；同一账号连续保存 / 轮换、保存与轮换交错完成，或请求返回前修改密码 / 原因时，旧响应不得清空当前密码、原因或覆盖当前错误提示；只有当前最新请求可清理 loading。
- [x] 账号授权资产备用登录弹窗必须绑定当前弹窗会话序号；同一账号内关闭或重开弹窗后，旧会话异步响应不得覆盖当前备用登录资源、login_flow、验证码输入、loading、错误提示或成功提示。
- [x] 账号授权资产备用登录启动请求必须绑定发起时的登录 payload 签名；启动返回前切换备用槽位、登录方式、开发者应用或代理时，旧启动响应不得写入当前 login_flow、验证码预填或错误提示；loading 清理必须绑定当前登录会话，避免 payload 变化后旧启动请求无法结束按钮 loading。
- [x] 账号授权资产备用登录完成和主授权切换完成后，后续账号详情刷新失败必须独立提示“刷新账号授权资产失败”，不得误报为验证码 / QR 检查失败或切换主授权失败。
- [x] 群授权、群策略保存、归档创建、归档中心新建归档、归档详情读取、归档导出和归档重跑失败必须展示后端错误 detail 或响应正文，不能只结束 loading 或形成不可见 Promise rejection。
- [x] 群管理账号覆盖和监听上下文入口打开群详情失败时必须关闭本地详情弹窗，并通过全局错误出口展示后端错误 detail 或响应正文；不能永久停留在“正在读取”空态。
- [x] 归档中心和群管理内的归档详情入口读取失败时必须关闭本地归档详情弹窗，并通过全局错误出口展示后端错误 detail 或响应正文；不能永久停留在“正在读取归档详情”的空态或旧详情态。
- [x] 归档详情读取必须绑定当前 archive_id 与请求序号；快速切换归档或关闭详情后，旧异步响应不得覆盖当前归档详情、清空当前 loading 或关闭当前归档详情弹窗。
- [x] 群授权更新、群策略保存、群入口归档创建、归档导出和归档重跑成功后的全局数据刷新失败必须提示“页面数据刷新失败”并说明原操作已完成，不得误报为授权更新、群策略保存、归档创建、归档导出或归档重跑失败。
- [x] 素材上传、批量上传、ZIP 导入、素材保存 / 禁用 / 恢复和关键词规则新增 / 保存失败必须展示后端错误 detail、响应正文或前端本地校验原因。
- [x] 素材上传、批量上传、ZIP 导入、保存、禁用、恢复、素材组保存 / 启停，以及关键词规则新增和保存成功后的素材中心数据刷新失败必须提示“素材中心数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 素材中心列表“刷新缓存”不得隐式打开素材详情抽屉；只有当前已打开同一素材详情时，刷新缓存成功后才同步刷新详情抽屉。
- [x] 素材中心素材新增 / 上传 / 批量上传 / ZIP 导入 / 保存必须绑定当前 material_id + tenant_id + payload + files 签名和保存请求序号；保存返回前切换素材、关闭 / 重开编辑弹窗、修改标题、类型、标签、内容、来源或文件列表时，旧保存响应不得关闭当前弹窗、清空当前文件、覆盖素材列表、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 素材中心素材禁用 / 恢复必须绑定当前 material_id + action 和请求序号；连续禁用、恢复不同素材或禁用与恢复交错完成时，旧操作响应不得覆盖素材列表、覆盖提示、触发旧操作成功刷新或清空新操作 busy。
- [x] 素材中心关键词规则新增 / 保存必须绑定当前 rule_id + tenant_id + payload 签名和保存请求序号；保存返回前切换关键词规则、关闭 / 重开编辑弹窗或修改关键词、匹配方式、启用状态、备注时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖提示、触发旧 payload 成功刷新或清空新保存 busy。
- [x] 素材组弹窗打开、素材组保存 / 启停后的素材组列表刷新必须绑定素材组请求序号；连续打开弹窗或多个素材组写动作交错完成时，旧素材组响应不得覆盖最新素材组列表、loading 或错误提示。
- [x] 素材中心的素材组保存必须绑定当前素材组动作 key、保存请求序号和表单 payload 签名，素材组启停必须绑定当前素材组动作 key；连续保存不同素材组、保存返回前修改素材组表单、保存与启停交错或连续启停不同素材组时，旧动作响应不得清空当前按钮 loading、覆盖当前错误提示、重置当前表单或触发旧动作成功刷新。
- [x] 风控中心代理检查、代理新增 / 编辑、代理禁用、代理告警处理和忽略失败必须展示后端错误 detail 或响应正文；确认弹窗代理禁用不能形成不可见 Promise rejection。
- [x] 风控中心代理检查、全局策略保存、代理新增 / 编辑 / 禁用、代理告警处理和忽略成功后的风控摘要刷新失败必须提示“风控中心数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 风控中心首屏加载、手动刷新和写动作后的风控摘要刷新必须绑定风控数据请求序号；连续刷新或多个代理 / 策略操作交错完成时，旧刷新响应不得覆盖最新 summary、proxy 列表、loading 或错误提示。
- [x] 风控中心代理检查、代理告警处理和告警忽略的按钮 loading、错误提示和成功刷新必须绑定当前动作 key；连续处理不同代理或不同告警时，旧动作响应不得清空当前按钮 loading、覆盖当前错误提示或触发旧动作成功刷新。
- [x] 风控中心全局策略保存和代理新增 / 编辑必须绑定发起时的表单 payload 签名与保存请求序号；保存返回前修改策略或代理表单时，旧保存响应不得关闭当前弹窗、覆盖当前错误提示或触发旧 payload 的成功刷新；loading 清理必须绑定当前保存请求序号，避免旧保存清掉新保存状态。
- [x] 系统设置后台账号 Token 流水读取、提示词模板新增和保存失败必须展示后端错误 detail 或响应正文，不能在弹窗打开或表单提交链路中形成不可见 Promise rejection。
- [x] 系统设置后台账号编辑弹窗切换用户时必须先清空旧 Token 流水，读取失败不得保留上一个后台用户的流水记录。
- [x] 系统设置后台账号编辑弹窗读取 Token 流水必须绑定 user_id + 请求序号；快速切换后台账号、打开创建用户弹窗或 Token 调整后刷新流水时，旧用户流水响应不得覆盖当前用户流水、错误提示或 busy 状态。
- [x] 系统设置打开“创建后台账号”弹窗时必须同步失效旧 Token 流水请求并清空已渲染流水，避免新建账号表单继续展示上一个后台用户的 Token 明细。
- [x] 系统设置开发者应用、运营空间配置、群聊救援配置、后台账号、Token 调整、AI 供应商、AI 配置和提示词写动作成功后的系统设置数据刷新失败必须提示“系统设置数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 系统设置开发者应用新增 / 保存必须绑定当前 app_id + payload 签名和写请求序号；保存返回前切换开发者应用、关闭 / 重开编辑弹窗或修改应用名、api_id、api_hash、账号上限、备注、启用状态时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 系统设置开发者应用启停 / 检查必须绑定当前 app_id + action 和请求序号；连续启停、检查不同应用或启停与检查交错完成时，旧操作响应不得覆盖当前提示、触发旧操作成功刷新或清空新操作 busy。
- [x] 系统设置运营空间配置保存必须绑定当前 tenant_id + payload 签名和写请求序号；保存返回前切换运营空间、关闭 / 重开编辑弹窗或修改名称、套餐、账号配额、任务配额时，旧保存响应不得关闭当前弹窗、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 系统设置群聊救援配置保存必须绑定当前 tenant_id + payload 签名和写请求序号；保存返回前切换运营空间、修改启用状态或替换救援管理员账号时，旧保存响应不得覆盖当前提示、触发旧 payload 成功刷新或清空新保存 busy。
- [x] 系统设置后台账号新增 / 保存必须绑定当前 user_id + payload 签名和写请求序号；保存返回前切换后台账号、打开新建账号弹窗或修改姓名、密码、角色、模板、订阅状态、权限、启用状态时，旧保存响应不得关闭当前弹窗、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 系统设置后台账号重置密码必须绑定当前 user_id + new_password 签名和写请求序号；重置返回前切换后台账号、打开新建账号弹窗或触发另一用户重置时，旧重置响应不得覆盖当前提示或清空新重置 busy。
- [x] 系统设置后台账号 Token 调整必须绑定当前 user_id + payload 签名和写请求序号；调整返回前切换后台账号、打开新建账号弹窗、修改调整 Token 数量或原因、触发另一用户 Token 调整时，旧调整响应不得覆盖当前提示、切回旧用户 Token 流水、触发旧 payload 成功刷新或清空新调整 busy。
- [x] 系统设置 AI 供应商新增 / 保存必须绑定当前 provider_id + payload 签名和写请求序号；保存返回前切换 AI 供应商、关闭 / 重开编辑弹窗或修改供应商名称、base_url、模型、API Key、请求头、备注、启用状态时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 系统设置 AI 供应商启停 / 检查必须绑定当前 provider_id + action 和请求序号；连续启停、检查不同供应商或启停与检查交错完成时，旧操作响应不得覆盖当前提示、触发旧操作成功刷新或清空新操作 busy。
- [x] 系统设置 AI 配置保存必须绑定当前 default_provider_id + ai_enabled + fallback_to_mock + temperature + max_tokens payload 签名和写请求序号；保存返回前切换默认供应商或修改启用状态、回退策略、温度、Token 上限时，旧保存响应不得关闭当前弹窗、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 全局 runWithLoading 必须绑定最近一次全局 busy 请求序号；多个动作并发或同一个 action key 连续触发时，旧动作结束不得清空后发动作的 busy 状态，pending action key 清理不得误删其他仍在进行的同名动作。
- [x] 系统设置提示词模板新增 / 保存必须绑定当前 template_id + tenant_id + payload 签名和写请求序号；保存返回前切换提示词模板、关闭 / 重开编辑弹窗或修改名称、类型、内容、启用状态时，旧保存响应不得关闭当前弹窗、重置当前表单、更新模板列表、覆盖提示或触发旧 payload 成功刷新，旧保存也不得清空新保存 busy。
- [x] 顶部“刷新当前数据”在系统设置页刷新当前 Tab 二段懒加载失败时必须展示后端错误 detail 或响应正文，不能形成不可见 Promise rejection。
- [x] 顶部“刷新当前数据”在非系统设置页刷新失败时必须提示“刷新当前数据失败”，不能误报为“系统设置数据读取异常”。
- [x] 系统设置当前 Tab 的二段懒加载和顶部刷新后的 Tab 刷新必须绑定 tab + 请求序号；快速切换 Tab、离开系统设置或顶部刷新与 Tab 懒加载交错完成时，旧 Tab 响应不得覆盖当前 Tab 数据、loading 或错误提示。
- [x] 全局刷新、顶部“刷新当前数据”、路由切换和写动作后的 snapshot 刷新必须绑定全局刷新请求序号；连续刷新、路由切换或写动作后二段刷新交错完成时，旧 snapshot 不得覆盖当前页面状态、busy 或错误提示。
- [x] 群聊救援配置的救援管理员账号搜索必须绑定当前搜索请求序号；旧搜索响应不得覆盖当前候选账号列表、loading 或错误提示，当前搜索失败必须在配置卡片内展示后端错误 detail 或响应正文。
- [x] 监听中心汇总刷新、自动轮询、切换监听账号和重置水位必须绑定当前汇总请求序号；旧汇总响应不得覆盖最新 summary、loading、操作状态或错误提示。
- [x] 监听中心事件明细和错误明细下钻必须绑定当前详情请求序号；旧下钻响应不得清空当前 detail loading 或覆盖当前错误提示。
- [x] 监听中心重置水位弹窗必须绑定当前弹窗会话；提交后关闭或切换到另一个监听对象时，旧重置响应不得关闭当前弹窗、覆盖当前错误提示或清理错误明细。
- [x] 运营数据页 `/api/operation-metrics/summary` 初始加载和手动刷新必须绑定当前请求序号；旧汇总响应不得覆盖最新 metrics、loading 或错误提示。
- [x] 归档中心新建归档成功后的归档列表刷新失败必须单独展示为“归档列表刷新失败”；不得复用归档目标下拉错误状态或误报“归档目标加载失败”。
- [x] 群管理入口创建任务时读取运营目标失败必须展示后端错误 detail 或响应正文，不得只提示“手动选择目标”。
- [x] 运营目标深链聚焦只能消费一次并打开一次详情，不能重复触发详情请求或重复执行只读详情流程。
- [x] 运营目标列表加载失败必须展示后端错误 detail 或响应正文，不能在 `void load()` 中形成不可见 Promise rejection。
- [x] 运营目标列表首屏加载、手动刷新、自动轮询和写动作后的目标列表刷新必须绑定列表请求序号；连续刷新、轮询与写动作交错完成时，旧列表响应不得覆盖最新目标列表、loading 或错误提示。
- [x] 运营目标全量同步必须绑定独立的同步动作请求序号；自动轮询、手动刷新或写动作后的列表刷新不得抢占全量同步的成功 / 失败提示、按钮 loading 或同步结果处理，全量同步成功后再按写动作刷新契约刷新运营目标列表。
- [x] 运营目标新增 / 保存必须绑定发起时的 target_id + payload 签名与保存请求序号；保存返回前切换编辑目标、打开新建弹窗或修改目标类型、peer、标题、username、人数、可发送状态、授权状态时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖当前错误提示或触发旧 payload 的成功刷新；loading 清理必须绑定当前保存请求序号。
- [x] 运营目标详情读取失败后不得继续自动同步目标消息；只有详情读取成功后才能触发依赖详情上下文的同步副作用。
- [x] 运营目标详情读取和成功动作后的详情刷新必须绑定当前 target_id 与请求序号；连续打开同一目标、深链重复聚焦或触发二段刷新时，旧详情响应不得覆盖当前详情、loading 或错误提示。
- [x] 运营目标详情内会直接回写详情或刷新详情的自动同步、评论同步、账号策略保存和准入重试响应必须绑定详情写回请求序号；同一目标内连续触发多个写动作时，旧写动作响应不得覆盖最新详情、清空最新 loading 或覆盖最新错误提示。
- [x] 运营目标新增 / 保存、详情自动同步、评论同步、账号策略保存、准入重试和归档创建成功后的目标列表或目标详情刷新失败必须提示“运营目标数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 运营中心异常详情读取和异常处理动作必须绑定当前 issue_id；旧异常异步响应不得覆盖当前异常抽屉的数据、loading、错误提示或处理结果。
- [x] 运营中心异常处理提交必须绑定发起时的 issue_id + action + reason 签名、提交请求序号和处理原因弹窗会话；切换处理动作、关闭 / 重开原因弹窗或修改原因后，旧提交响应不得关闭当前原因弹窗、清空当前原因、覆盖提示或触发旧原因成功刷新。
- [x] 运营目标详情读取、自动同步、评论同步、账号策略保存、准入重试和归档创建后的详情刷新必须绑定当前 target_id；旧目标异步响应不得覆盖当前详情、loading 或错误提示。
- [x] 任务中心列表刷新、轮询和外部深链聚焦任务详情失败必须展示后端错误 detail、响应正文或 trace_id，不能在 `void load()` 或 `.catch()` 中只留下固定失败文案。
- [x] 任务中心列表首屏加载、任务类型切换、自动轮询和写动作后的任务列表刷新必须绑定列表请求序号；连续刷新、任务类型切换、轮询和写动作交错完成时，旧任务列表响应不得覆盖最新任务列表、调度配置、loading 或错误提示。
- [x] 任务中心的启动 / 暂停 / 恢复 / 停止 / 重试 / 重置、准入处理、准入失败导出和删除任务按钮状态、错误提示和成功后的刷新触发必须绑定当前动作 key；连续操作不同任务或不同准入项时，旧动作响应不得清空当前按钮 loading、覆盖当前错误提示或触发旧动作成功刷新。
- [x] 任务中心创建预检和编辑页 AI 数量推荐必须绑定发起时的 task_type + payload 签名与请求序号；修改任务类型、目标、账号范围、节奏或数量字段后，旧响应不得覆盖当前预检、推荐值、warning、错误提示或 loading；创建提交不得复用旧 payload 的预检结果。
- [x] 任务中心保存任务配置必须绑定发起时的 task_id + payload 签名、保存请求序号和当前编辑弹窗会话；切换任务详情、关闭 / 重开编辑弹窗或修改任务配置表单后，旧保存响应不得关闭当前弹窗、覆盖提示或触发旧任务配置成功刷新，旧保存也不得清空新保存 loading。
- [x] 任务中心创建任务、保存任务配置、启动 / 暂停 / 恢复 / 停止 / 重试 / 重置、准入处理、删除任务和来源屏蔽成功后的任务列表或当前任务详情刷新失败必须提示“任务中心数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 任务中心写动作成功后的当前详情刷新和准入处理返回详情必须绑定当前 task_id；快速切换任务或关闭详情后，旧任务响应不得重新打开或覆盖当前详情、分页和错误提示。
- [x] 任务中心执行尝试下钻读取失败必须在执行尝试弹窗内展示后端错误 detail 或响应正文，不能清空弹窗并通过 `void openActionAttempts(action)` 形成不可见 Promise rejection。
- [x] 任务中心执行尝试下钻读取必须绑定当前 action_id；快速切换 action 或关闭执行尝试弹窗后，旧 action 异步响应或失败不得覆盖当前尝试列表、loading 或错误提示。
- [x] 任务中心详情弹窗的 Action、AI Cycle、频道消息组、转发批次、准入 item 和准入账号明细分页请求必须绑定当前 task_id；旧任务异步响应不得污染当前详情分页状态或错误提示。
- [x] 任务中心详情弹窗的执行计划和执行记录分页请求必须绑定分页请求序号；同一任务内快速切换页码或刷新详情时，旧分页响应不得覆盖最新页码、rows、total 或 loading。
- [x] 任务中心详情弹窗的 AI Cycle、频道消息组、转发批次和准入 item 子分页请求必须绑定子分页请求序号；同一任务内快速切换页码或刷新详情时，旧子分页响应不得覆盖最新页码、详情字段、total 或 loading。
- [x] 任务中心详情弹窗的准入账号明细分页和筛选请求必须绑定准入账号分页请求序号；同一任务内快速切换页码、page size 或筛选条件时，旧准入账号响应不得覆盖最新页码、账号明细、total 或 loading。
- [x] 任务中心详情主请求和写动作后的当前详情刷新必须绑定详情请求序号；同一任务内连续打开、刷新或写动作后刷新详情时，旧详情响应不得覆盖最新详情、重新触发旧分页加载或清理最新错误提示。
- [x] 任务中心详情主请求和外部深链聚焦任务详情失败时必须绑定当前 task_id；快速切换任务或关闭详情后，旧任务异步失败不得覆盖当前任务详情页错误提示。
- [x] 规则中心新建规则集、保存规则配置、复制 / 发布 / 回滚规则版本成功后的规则中心数据刷新失败必须提示“规则中心数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 规则中心首屏加载、手动刷新和规则写动作后的规则中心数据刷新必须绑定规则中心数据请求序号；连续刷新或多个规则操作交错完成时，旧刷新响应不得覆盖最新规则摘要、规则集、运营目标、转发归因报表、loading 或错误提示。
- [x] 规则中心测试器的规则测试请求必须绑定发起时的测试 payload 签名与请求序号；连续测试、切换规则版本 / 测试类型 / 媒体场景、修改样例或候选输出后，旧测试响应不得覆盖当前测试结果或错误提示；loading 清理必须绑定当前测试请求序号，避免 payload 变化后旧测试请求无法结束按钮 loading。
- [x] 规则中心新建规则集、保存规则配置、复制 / 发布 / 回滚规则版本必须绑定当前规则动作 key、写请求序号和发起时 payload 签名；修改新建表单、规则配置表单、版本操作原因或触发另一规则动作后，旧响应不得关闭当前弹窗、清空当前 loading、覆盖提示或触发旧 payload 成功刷新。
- [x] 目标画像学习来源保存、来源同步 / 历史拉取、质量规则保存、样本状态调整、画像重建 / 清空、学习开关调整和版本恢复成功后的目标画像数据刷新失败必须提示“目标画像数据刷新失败”并说明原操作已完成，不得误报为原写动作失败。
- [x] 目标画像首屏加载、手动刷新和写动作后的数据刷新必须绑定画像数据请求序号；连续刷新或多个画像操作交错完成时，旧刷新响应不得覆盖最新画像摘要、学习来源、候选、样本、运行记录、版本、质量规则表单或 loading / error。
- [x] 目标画像的学习来源保存、来源同步 / 历史拉取、质量规则保存、样本状态调整、画像重建 / 清空、学习开关调整、版本恢复和候选重算必须绑定当前动作 key、写请求序号和发起时 payload 签名；修改来源选择、质量规则表单、原因或触发另一画像动作后，旧响应不得清空当前 loading、覆盖提示或触发旧 payload 成功刷新。
- [x] 规则中心绑定任务弹窗必须绑定当前 rule_set_id；旧规则集异步响应不得覆盖当前绑定任务列表、loading 或错误提示。
- [x] 任务中心创建 / 编辑弹窗表单支撑数据失败必须展示后端错误 detail 或响应正文；预填、任务类型切换和弹窗懒加载不能形成不可见 Promise rejection。
- [x] 任务中心创建 / 编辑弹窗表单支撑数据加载、任务类型切换和默认规则集回填必须绑定表单支撑数据请求序号；快速切换任务类型、重复打开弹窗或关闭后重开时，旧任务类型响应不得清理当前 loading、覆盖当前错误提示或回填旧类型默认规则集。
- [x] 账号中心 URL 深链打开账号详情失败必须展示后端错误 detail 或响应正文，不能在 `.then()` 链里形成不可见 Promise rejection。
- [x] 登录页不保留自助注册死流程；后台账号只通过系统设置的后台账号权限管理，前端不得调用不存在的 `/auth/register`，后端不保留注册请求 schema。
- [x] 生产环境启动时必须拒绝默认 bootstrap 管理员密码 `admin123`，要求显式配置 `ADMIN_BOOTSTRAP_PASSWORD` / `ADMIN_PASSWORD`。
- [x] 登录页验证码加载和验证码校验失败必须展示后端错误 detail 或响应正文；账号密码失败保留泛化文案以避免账号枚举。
- [x] 公共 API 客户端和全局操作失败弹窗必须复用 `ApiError.message`，解析 FastAPI 字符串、数组和对象型 `detail`，让仍直接展示 `error.message` 的页面也能看到可读后端原因，而不是原始 JSON。
- [x] 审计导出、规则归因导出和准入失败清单等 blob 下载路径必须用统一 API 响应错误解析；管理端认证 401 必须触发登录态过期处理，不能抛原始 `Error(await response.text())` 或普通 `ApiError`。
- [x] 审计导出 Modal 确认动作失败必须展示“导出审计记录失败”及后端错误 detail 或响应正文，不能用 `void exportCsv(...)` 形成不可见 Promise rejection。
- [x] 登录提交失败必须区分账号密码 401 和验证码 token / 服务端 / 网络错误；验证码链路错误要刷新验证码并展示后端 detail，不能误报“账号和密码”或形成未处理 Promise。
- [x] Redis 模式下后台登录验证码 token 消费必须 fail closed；原子 Lua 消费失败时不能回退到非原子读写，也不能让同一 token 在竞态下重复使用。
- [x] 素材详情读取和缓存刷新后的详情回填必须绑定当前 material_id；旧素材异步响应不得覆盖当前详情、引用记录、版本记录、loading 或错误提示。
- [x] 素材缓存刷新成功后的素材列表刷新失败必须展示“刷新素材列表失败”及后端错误，不得形成不可见 Promise rejection 或误报为刷新素材缓存失败。
- [x] 素材运行配置保存必须区分 PATCH 保存失败和保存成功后的配置 / 健康状态刷新失败；刷新失败必须展示“缓存配置刷新失败”，不得误报为保存失败。
- [x] 素材运行配置支持填写缓存频道链接、`@username` 或 `t.me/c/...` 链接，后端解析为执行层 peer；普通管理员不需要手动知道 `-100...` 内部 ID，并兼容 `.env` 回退；缓存执行账号选择器必须支持按手机号、备注名和 TG username 搜索。
- [x] 素材运行配置保存必须绑定发起时的缓存配置 payload 签名与保存请求序号；保存返回前修改缓存频道、源媒体频道或缓存执行账号时，旧保存响应不得覆盖当前保存错误、刷新错误、成功提示或警告提示；loading 清理必须绑定当前保存请求序号。
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

- [x] 运营目标服务端分页 / 搜索；Overview 当前目标页 + `target_ids` 摘要；Rules / Archives 分页懒加载；MessageSending 按账号远程查询；AppShell `linked_group_id` 定点查。
- [x] 任务中心服务端分页 / 统计 / 分组与 60 秒当前查询轮询；任务创建 / 编辑弹窗 2 秒内可操作，目标远程搜索与 ids 回显正常。
- [x] `cd frontend && npm run build`
- [x] 运营中心首屏人工截图检查。
- [x] 任务创建 5 类路径人工走查。
- [x] 账号中心可用性和安全批次人工走查。

后端验收：

- [x] 运营目标分页头、组合过滤、旧无新增参数兼容、当前页 SQL 条件计数、无 `TgGroupAccount` 全量 ORM 物化、runtime-summary `target_ids` 与租户隔离测试通过。
- [x] `/api/tasks/page` 跨普通 / 系统任务稳定分页、total/summary/groups、列表无完整四类 config、系统 batch items 无 N+1 与租户隔离测试通过。
- [x] 当前生产规模下两个有界列表各自小于 2 秒、单页小于 100 KB；任务编辑 2 秒内可操作；连续刷新零 502。生产 E4：两个列表各 30 次串行全部 200，任务 p95/p99 446/451ms、目标 p95/p99 339/346ms；10 路并发最慢 1.699s/830ms；编辑弹窗 427ms。
- [x] `PYTHONPATH=backend pytest -q`
- [x] PostgreSQL 空库迁移验证。
- [x] 1000 账号容量脚本重新生成报告。

2026-05-23 PostgreSQL 验收说明：`backend/tests/conftest.py` 会在测试开始前 `DROP SCHEMA public CASCADE` 并重建 schema，随后应用启动按 `AUTO_MIGRATE_ON_START=true` 自动迁移到当前模型，已覆盖空库迁移和全量后端回归。1000 账号容量报告已通过 `backend/scripts/run_capacity_benchmark.py` 重新生成。旧库漂移场景已通过 `0046_repair_admin_tables` 修复缺失的 `app_users` / `user_token_ledgers`，当前开发库升级到 `0046_repair_admin_tables` 后确认 `actions`、`execution_attempts`、`runtime_metric_snapshots` 仍存在；运营中心首屏已用 Playwright 登录态检查，目标工作台、目标异常、素材中心菜单和趋势区可见，控制台 error/warning 为 0。

2026-05-23 前端与任务路径验收说明：任务创建 5 类路径通过 `test_task_center_group_ai_chat_creates_and_dispatches_actions`、`test_task_center_group_relay_auto_executes_and_dedupes`、`test_task_center_channel_view_like_comment_execute` 覆盖 AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论创建、启动和 drain 执行。账号中心通过 Playwright 登录态访问 `/accounts`，确认可用性重算、资料初始化、设置二步密码、清理登录设备入口可见，选择账号后能打开“批量清理登录设备”安全批次抽屉，控制台 error 和 404 均为 0。

2026-06-25 全代码审查补充：局部页面错误格式化函数不得重新解析 `ApiError.body` 并只处理字符串型 `detail`。运营目标、运营中心、消息发送、目标画像、任务中心和登录验证码链路必须复用公共 `ApiError.message`，确保 FastAPI `detail` 为字符串、Pydantic 数组或对象型 `message` / `failure_detail` / `trace_id` 时都展示可读错误，而不是把原始 JSON 作为主要错误文案。

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
