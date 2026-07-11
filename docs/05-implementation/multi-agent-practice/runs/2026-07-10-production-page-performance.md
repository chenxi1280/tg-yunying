# 2026-07-10 生产核心页面性能修复运行记录

## Intake Card

- `intake_id`: `intake-2026-07-10-production-page-performance`
- `bug_id`: `bug-2026-07-10-production-page-performance`
- 用户原话：线上各个页面打开缓慢，很多打开超时，例如任务编辑页面；要求查明原因并修复。
- 分级：`L2 / P1 / standard_team`。
- Release Gate：required。
- 当前 owner：prod-diagnosis（Release Gate 与生产 E4 已通过，记录日志取证边界后终态交付）。

## 生产只读诊断

| 证据 | 结果 | 判定 |
| --- | --- | --- |
| `GET /api/operation-targets` | 3,810 条，约 1.91 MB，17.288 秒；前端默认 15 秒 abort | 根因 confirmed：无界读取直接阻塞任务编辑支撑数据 |
| `GET /api/tasks` | 67 条，约 207 KB，成功样本约 3.43 秒；另观察到 502 | 无界列表与系统批次 N+1 confirmed；502 唯一直接 upstream 原因 unproven |
| static / health | 样本约 0.7–1.6 秒 | 不支持整站静态资源统一故障 |
| SSH / 服务日志 | 当前凭证 publickey denied | nginx、容器、DB 分段耗时与 502 直接原因未取证 |

代码复核确认 `filter_operation_targets` 先取全部目标和群，再物化全部匹配 `TgGroupAccount` 行并在 Python 聚合；任务列表同时返回普通 Task 与最多 50 个系统批次，系统批次按批次读取 items，形成 N+1。直接运营目标消费者共七处：OperationTargets、Overview、TaskCenter、RulesCenter、Archives、MessageSending、AppShell。

## Product Handoff

- `message_id`: `handoff-2026-07-10-production-page-performance-dev`
- `design_status`: complete。
- `dev_handoff_ready`: true。
- 无数据迁移，无 worker 行为变化。
- 运营目标：现有 GET 增加有界 `page/page_size/q/ids/linked_group_id/capability`；`ids` 使用重复参数，capability 为 `send/listen/archive/task`。先分页目标，再只聚合当前页关系；旧无新增参数语义暂兼容，第一方全部显式有界。
- 运行摘要：重复 `target_ids` 只读当前目标页，空页不解释为全量。
- 任务列表：新增 `/api/tasks/page`；普通 Task 与账号安全系统任务统一轻量索引，顺序为 `priority ASC, created_at DESC, source_kind ASC, stable_id DESC`；`summary={total,running,failed}` 与 groups 在 `group_key`/分页前生成，顶层 total 在 group 过滤后生成；只水合当前页并消除 batch items N+1。
- 前端：目标管理服务端分页；任务编辑先开壳层、远程加载并用 ids 回显；Overview 当前页摘要；Rules/Archives 懒加载；MessageSending 按账号远程搜索；AppShell 定点读取；TaskCenter 服务端分页并只轮询当前查询。
- 禁止延长公共 15 秒 timeout，禁止失败后静默调用旧全量接口。

## QA 与 Release Gate

本地红绿标准：

1. 3,810 / 10,000 个目标下 page size、总数、稳定顺序、组合过滤、跨租户、重复 ids 和关联群定点查询正确；SQL 次数不随目标总量线性增加，50 条响应小于 100 KB。
2. 普通任务与至少 50 个安全批次共同分页；summary/groups 不受页码或 group 选择污染；batch 数增加时查询次数保持常量级，20 条响应小于 100 KB。
3. 七个目标消费者及任务列表前端数据流、请求序号、已有选中值、错误状态和构建全部通过。
4. 后端测试使用 `backend/.venv` 并有 60 秒硬超时；完成 compileall、frontend build、diff check、spec review 和 code quality review。

发布后 E4：`master -> release -> Deploy Production` 成功且核对实际镜像 commit；真实登录态对两个列表各做 30 次串行与一组 10 并发，零 408/499/502，p95 < 2 秒、p99 < 5 秒、单页 < 100 KB；逐页验证七个消费者和任务编辑，并在同窗口检查 nginx/backend 日志。

## 当前状态

- prod diagnosis：代码根因 `confirmed`；tasks 502 直接原因 `unproven`。
- product design：`complete`。
- dev：`complete`。运营目标有界查询、当前页摘要、任务轻量分页读模型、七个目标消费者、任务编辑先开壳层和 CORS 分页头均已实现。
- qa：`pass`。全量 no-postgres `1044 passed, 806 deselected`；PostgreSQL 定向 `20 passed`；frontend build、compileall、diff check 均通过。
- local browser：`pass`。在本地 PostgreSQL 注入 3,810 个目标与 170 个任务后，运营目标页 585ms 可见，目标 API 16ms / 8.9KB；任务列表 API 66ms / 16KB，第二页 430ms 可见；任务编辑 715ms 可操作、793ms 完成已选目标回显。Rules / Archives / MessageSending 均只在弹窗或选定账号后发起带 `page/page_size` 的目标查询。
- product acceptance：`accepted_local`。原始慢页、任务编辑、七消费者、显式错误、兼容和无 silent fallback 均已覆盖。
- release gate：`passed`。Commit `357c844d951f90659c077d91e002e9a1e7430ee2` 已按 `master -> release` 推送；Deploy Production run `29110463190` success，release `20260710172417_357c844`、backend/frontend 镜像与 Git SHA 一致，backend 和全部 worker healthy，公网 health HTTP 200。
- production E4：`pass`。任务中心首屏 1.224 秒，任务 API 252ms / 6.9KB；运营目标首屏 1.472 秒，目标 API 272ms / 1.85KB；任务详情 1.700 秒，编辑弹窗 427ms，目标 ids 水合 323ms / 719B。
- serial：`pass`。两个列表各 30 次全部 HTTP 200；任务列表 p95/p99 `446/451ms`、最大 7.1KB，运营目标 p95/p99 `339/346ms`、最大 1.85KB。
- concurrent：`pass`。10 路并发任务接口最慢 1.699 秒，运营目标最慢 830ms；全部 HTTP 200，零 408/499/502，单页均小于 100KB。
- related pages：`pass`。消息发送、规则、归档分别 1.328/2.721/0.710 秒可用；归档弹窗 308ms 打开，候选查询为 `page_size=50&capability=archive`、302ms / 3.9KB；生产页面 console error 为 0。
- production fixed：`confirmed`。用户反馈的慢页、任务编辑超时和连续刷新 502 症状已在真实生产登录态恢复。
- log correlation：`blocked`。本机发布后 SSH 只读核对被远端关闭连接，未取得同窗口 nginx/backend 请求日志；工作流已提供服务器侧镜像、容器和健康证据。
- historical `/api/tasks` 502 unique upstream cause：`unproven`。不把当前恢复倒推成旧 502 的唯一直接原因。

本地真实浏览器首次验收发现跨域前端无法读取存在于响应中的 `X-Total-Count`，页面显示“运营目标分页响应缺少 x-total-count”。已增加 CORS `Access-Control-Expose-Headers: X-Total-Count, X-Page, X-Page-Size`，补红绿集成测试并复测通过。浏览器控制台剩余三项均为既有 Ant Design 弃用 / Descriptions span 警告，不是请求失败。
