# Production Page Performance Fix Plan

## Goal

修复生产环境多个核心页面因运营目标和任务列表全量加载而超时或返回 502 的问题，在不放宽前端 15 秒超时的前提下恢复任务编辑及共享页面的有界加载。

## Current Phase

Complete

## Scope

- 共享运营目标读取：运营中心、运营目标、规则中心、归档中心、任务创建/编辑。
- 任务中心列表：首屏、筛选、分页和轮询。
- PRD、专项设计、项目结构索引、数据流索引和生产验收口径。
- 本地红绿测试、构建、代码审查和真实生产发布后验证。

## Phases

### Phase 1: Production Diagnosis

- [x] 在真实登录态复现任务编辑超时。
- [x] 记录接口耗时、响应规模、前端取消时点和共享消费者。
- [x] 区分运营目标共享接口与任务列表独立热点。
- **Status:** complete

### Phase 2: Product Design Complete

- [x] 完成 Intake Card、L2 分级和 Product Handoff。
- [x] 补充专项设计与 PRD/索引数据加载契约。
- [x] 完成设计覆盖、失败路径、并发、兼容、发布和回滚自检。
- **Status:** complete

### Phase 3: Implementation Plan and Baseline

- [x] 写入可执行实施计划并自检。
- [x] 建立隔离 worktree 和依赖环境。
- [x] 运行相关基线测试与前端构建。
- **Status:** complete

### Phase 4: Backend TDD

- [x] 先写运营目标聚合/分页红测并确认按预期失败。
- [x] 实现数据库聚合和有界运营目标接口。
- [x] 先写任务列表分页/摘要红测并确认按预期失败。
- [x] 实现有界任务列表接口并保持详情下钻。
- **Status:** complete

### Phase 5: Frontend TDD and Integration

- [x] 先写任务编辑、运营目标、运营中心、规则、归档分页/按需加载红测。
- [x] 更新前端 API 类型和页面数据流。
- [x] 保持请求序号、错误可见性和当前选中值回显。
- **Status:** complete

### Phase 6: QA and Review

- [x] 逐任务完成规格审查和代码质量审查。
- [x] 运行定向测试、后端测试、前端构建和 diff 检查。
- [x] 完成最终跨模块审查和本地 Release Gate。
- **Status:** complete

### Phase 7: Release and Production Verification

- [x] 按 `master -> release -> GitHub Actions Deploy Production` 发布。
- [x] 真实登录态验证任务编辑、核心页面、接口耗时和连续刷新。
- [x] 按 `pass / blocked / unproven` 汇报生产状态。
- **Status:** complete

### Phase 8: Review Remediation

- [x] 先补任务列表分页前全量 ORM 物化的回归测试，并确认红测。
- [x] 将任务候选索引改为数据库侧轻量投影，分页后才加载完整任务对象。
- [x] 先补消息发送切换账号清理旧运营目标的回归测试，并确认红测。
- [x] 切换账号时立即清理旧运营目标选择与缓存，避免异步时间窗口。
- [x] 先补已选目标批量水合固定并发度的回归测试，并确认红测。
- [x] 将无界 `Promise.all` 改为固定并发批次加载，不改变可选目标数量语义。
- [x] 运行定向测试、全量相关测试、前端构建、静态检查和 diff 审查。
- **Status:** complete

## Success Criteria

- 当前生产规模下，任务编辑支撑数据请求目标小于 2 秒，且不触发 15 秒取消。
- 运营目标管理首屏和搜索均为服务端有界加载，不再返回 3,810 条全量列表。
- 运营中心、规则中心、归档中心不再因全量运营目标接口阻塞首屏。
- 任务列表首屏为服务端分页摘要，不再发送约 207 KB 的 67 条全量任务载荷。
- 连续刷新不出现 502，旧响应不得覆盖新状态，错误继续显式展示。
- 不增加 silent fallback、mock success 或仅提高 timeout 的规避逻辑。

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| 按用户授权执行完整范围 | 用户在完整方案说明后明确要求进行修复 |
| 使用隔离分支 `codex/fix-production-page-performance` | 当前主检出位于干净 `master`，避免直接开发污染 |
| 不调整前端默认 15 秒超时 | 生产实测说明根因是无界查询和大载荷，放宽超时只会掩盖问题 |
| 后端统计下推数据库并让列表有界 | 减少 ORM 对象物化、Python 聚合、序列化和网络载荷 |
| 任务列表与运营目标热点分别验收 | 两者共享“无界读取”模式，但属于两个独立 API 热点 |

## Errors Encountered

| Error | Attempt | Resolution |
|-------|---------|------------|
| 直接页面 `fetch('/api/operation-targets')` 返回 401 | 1 | 未读取 token；改为在已登录 UI 请求链中只记录响应统计 |
| 初次用含“编辑任务”的 dialog 判断编辑弹窗，误匹配详情弹窗 | 1 | 改为等待唯一按钮“保存并重新规划” |
| Chrome 新标签首次未导航即调用 CDP | 1 | 先显式导航到 HTTPS 页面再启用只读诊断 |
| macOS 环境缺少 GNU `timeout` 命令 | 1 | 改用 Python `subprocess.run(..., timeout=60)` 执行后端测试 |
| 后端基线测试在收集阶段阻断：PostgreSQL test database reset failed | 1 | 启动 Docker Desktop，复用本地 `postgres:16-alpine` 镜像标记为 `postgres:16`，启动隔离测试库后 22 条集成基线通过 |
| 首次更新基线错误日志的补丁上下文未匹配 | 1 | 读取准确表格行后拆成小补丁更新 |
| Docker PostgreSQL 镜像拉取在 registry manifest 阶段返回 EOF | 1 | 不重复 compose 拉取；先检查本地镜像和可用替代测试数据库来源 |
| zsh 中变量名 `status` 为只读变量 | 1 | 改用 `db_health` 变量后重新检查容器健康 |
| 本地真实浏览器无法读取响应中已有的 `X-Total-Count` | 1 | 补 CORS expose-headers 红绿测试并显式暴露三个分页头，浏览器复测通过 |
| AppShell peer 兜底目标被 TypeScript 推断为不可空 | 1 | 显式标注 `OperationTarget | undefined` 后生产构建通过 |
| macOS 环境缺少 GNU `timeout` 命令（本轮审查复测） | 1 | 使用 Python `subprocess.run(..., timeout=60)`，35 条定向测试通过 |
| JSON 路径投影探针使用了重复 `backend/` 前缀 | 1 | 在 `backend` 工作目录改用 `.venv/bin/python`，探针通过 |
| 账号安全批次定向测试 PostgreSQL reset 失败 | 1 | 先完成无数据库回归与代码检查，再恢复既有隔离测试库重跑 |
| 安全批次统计抽取补丁上下文不匹配 | 1 | 读取准确函数区域后拆分创建模块与删除旧函数，结构检查通过 |
| zsh 未匹配 `backend/.env*` glob | 1 | 后续直接检查明确文件路径，避免未引用 glob |
| `.env` 测试库旧地址不可用且本地容器凭据不同 | 1 | 不修改环境文件；测试子进程从 healthy 容器读取现有连接参数，集成测试通过 |
| 全量 no-postgres 被 `.env` asyncpg URL 污染 | 1 | 测试子进程显式设置 SQLite URL 后重跑，避免安装无关驱动或修改环境文件 |
| 静态工具配置搜索再次触发 zsh 空 glob | 1 | 已取得函数长度结果；最终验证只使用明确路径，不再使用 glob |

## Evidence Rules

- 本地测试通过不等于生产恢复。
- 发布成功不等于生产接口耗时达标。
- 只有真实生产登录态和接口时序证据可进入 `production_fixed`。
