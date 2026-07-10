# Findings and Decisions

## Requirements

- 用户要求检查并修复线上各页面打开缓慢、超时问题，明确举例任务编辑页。
- 已批准完整修复方向：共享运营目标链路与任务列表都进入本轮范围。
- 必须按项目生产问题流程完成产品设计、开发、QA、产品验收和生产复核。

## Production Evidence

- `https://tgyunying.telema.cn/task-center` 可进入真实登录态。
- `/api/operation-targets` 在一次完整 UI 请求中返回 3,810 条、1,914,409 字符，耗时 17,288 ms，HTTP 200。
- 前端 `apiWithMeta` 默认 15,000 ms 后调用 `AbortController.abort()`；实测该请求发生一次 abort。
- 任务编辑在打开弹窗前等待 `ensureTaskFormData()`，其中 `ensureTargets()` 调用无参数 `/operation-targets`。
- `/api/tasks` 返回 67 个任务时约 207,484 decoded bytes；曾返回 nginx 502，成功请求约 3.43 秒。
- 静态页和健康接口当时可在约 0.7-1.6 秒返回，说明不是整站静态资源不可达。

## Code Findings

- `backend/app/services/operations.py:filter_operation_targets` 先全量读取目标，再读取关联群，再把关联群的全部 `TgGroupAccount` ORM 行加载到 Python 分组和计数。
- 任务表单目标下拉实际使用 id、title、auth_status、can_send、linked_group_id、available_send_account_count 和 listener_account_count。
- `OperationTargetsView`、`OverviewView`、`RulesCenterView`、`ArchivesView`、`TaskCenterView` 和部分 AppShell 深链均调用共享 `/operation-targets`。
- 任务中心前端表格只显示 10 条，但后端 `/tasks` 返回全部 67 条后由 AntD 本地分页。
- `/api/tasks` 已有详情子资源的 `X-Total-Count/X-Page/X-Page-Size` 分页约定，但列表路由尚未复用。
- `list_tasks()` 会先读取全部 Task，再构建全部目标/频道搜索上下文，并附加最多 50 条账号安全系统批次；分页必须同时覆盖两类任务或明确排序边界。
- 任务列表当前本地搜索和“按目标群聊 + 关联频道”分组依赖完整 `type_config`；直接截断为当前页会让分组数量和本地搜索不完整，设计必须改成服务端筛选/分组或保留轻量全量分组元数据。
- PRD 已规定核心页面只加载当前页面必要数据，详情按 ID 下钻，请求序号不得让旧响应覆盖新状态。

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| 保留旧 `/operation-targets` 兼容行为，仅对新查询参数启用有界响应或新增有界契约 | 避免未迁移消费者和同步动作立即破坏 |
| 统计使用 SQL `GROUP BY`/条件计数 | 页面只需要计数和布尔能力，无需物化每个关系对象 |
| 运营目标管理使用服务端分页和搜索 | 3,810 条已经超出合理首屏全量边界 |
| 表单目标选择采用有界搜索并显式包含已选 ID | 兼顾性能和编辑旧任务回显 |
| 任务列表返回摘要分页，详情仍走现有详情接口 | 保持任务中心职责和既有下钻模型 |
| 保留 15 秒统一超时 | 超时是可见失败边界，不是根因 |
| 任务列表不能只加 offset/limit 后继续复用当前本地分组 | 会造成跨页分组数量、筛选和搜索语义错误 |

## Product/Release Classification

- Product 结论为 L2 / P1 / standard_team：多个生产核心页面受影响，但健康检查和部分页面仍可用。
- 影响生产，必须经过 Release Gate。
- 若生产连续出现核心页面完全不可进入或数据写操作受阻，升级 L3。
- Product Design Complete：`design_status=complete`、`dev_handoff_ready=true`、无阻塞输入、无迁移、无 worker 影响。
- Product Handoff：`handoff-2026-07-10-production-page-performance-dev`，next=`dev`。

## Independent Prod-Diagnosis Review

- `confirmed`：运营目标无界读取和全量关联行物化；7 个直接前端消费者；运营目标页面每分钟全量轮询。
- `confirmed`：任务列表无分页、宽 `TaskOut`，账号安全系统批次最多形成 50 次 item 查询 N+1。
- `unproven`：任务列表 502 的唯一直接原因；仍需发布后 nginx/backend/DB 同窗口证据排除 upstream reset、容器/OOM 和连接池等待。
- 隐藏放大路径：单账号目标同步返回全租户目标，sync-all 在账号循环内重复全量聚合；实现时至少要让第一方写路径不再依赖未分页完整响应。
- 任务列表分页必须提供全局 summary/facet，否则统计卡和快捷分组会退化为当前页口径。
- 运营目标分页必须支持 `ids`、`linked_group_id`、`account_id` 和 capability 过滤，否则非首页已选值和深链会静默丢失。

## Resources

- `frontend/src/shared/api/client.ts`
- `frontend/src/app/views/TaskCenterView.tsx`
- `frontend/src/app/views/TaskCenterWizardSections.tsx`
- `frontend/src/app/views/OperationTargetsView.tsx`
- `frontend/src/app/views/OverviewView.tsx`
- `frontend/src/app/views/RulesCenterView.tsx`
- `frontend/src/app/views/ArchivesView.tsx`
- `backend/app/api/routers/operations.py`
- `backend/app/services/operations.py`
- `backend/app/api/routers/task_center.py`
- `backend/app/services/task_center/`
- `docs/01-product/tg-ops-platform-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`

## Local Baseline

- 前端 `npm run build` 通过，存在原有 Vite chunk-size warning。
- no-postgres 数据流/测试设施基线 24 个测试通过。
- PostgreSQL 集成基线初次因 Docker daemon 未运行而 blocked；启动 Docker 后，compose 拉取 `postgres:16` 遇到 registry EOF，随后复用本机已有 `postgres:16-alpine` 镜像启动独立 `tg_yunying_test` 数据库。
- PostgreSQL 相关基线最终 22 个测试通过，耗时 1.14 秒。

## Browser Findings

- 任务列表真实页面显示 67 条，客户端当前页只展示 10 条。
- 任务详情可打开；点击“编辑任务”时必须先完成表单支撑数据加载。
- 通过临时、已恢复的页面诊断包装记录到 `/operation-targets` 完整响应规模和耗时；未读取或导出浏览器 token、cookie、localStorage。
