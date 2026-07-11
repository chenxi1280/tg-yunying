# 生产核心页面有界加载与任务列表分页设计

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 设计状态 | Product Design Complete（`design_status=complete`） |
| 开发交接 | 已具备完整开发输入（`dev_handoff_ready=true`） |
| 实现状态 | 截至 2026-07-10 未开始；本文描述本轮待实现目标，不代表已上线 |
| Intake | `intake-2026-07-10-production-page-performance` |
| Handoff | `handoff-2026-07-10-production-page-performance-dev` |
| 分级 | L2 / P1，`standard_team` |
| 生产相关 | 是；Release Gate required，当前 `release_gate=pending` |
| 证据等级 | E4 线上只读诊断证据；代码、QA、产品验收和修复状态仍未证明 |
| 最终状态 | `implementation_not_started`、`qa_pending`、`product_acceptance_pending`、`production_fixed=unproven` |

本设计解决运营目标与任务中心列表在当前生产数据规模下的无界读取问题。实现必须保持现有权限、租户隔离、错误可见性和 15 秒请求超时；不能通过放宽超时、静默降级、返回截断假全量或 mock success 绕过根因。

## 2. Intake Card

### 2.1 用户问题

- 生产核心页面打开缓慢或超时，任务编辑弹窗尤为明显。
- 运营目标接口被多个页面和弹窗共享，一个无界调用会同时阻塞运营目标、运营中心、规则中心、归档中心、消息发送和任务创建 / 编辑。
- 任务中心表格虽然只展示当前页，后端仍返回全部普通任务和账号安全系统任务，再由前端本地分页、搜索和分组。

### 2.2 线上只读证据

| 路径 | 生产观测 | 当前结论 |
| --- | --- | --- |
| `GET /api/operation-targets` | 3,810 条，约 1.91 MB，17.288 秒，前端在 15 秒触发 abort | 已证明无界列表超过前端请求边界 |
| `GET /api/tasks` | 67 条，约 207 KB，成功样本约 3.43 秒，并出现间歇 502 | 已证明列表载荷和查询均需要有界化 |
| 502 根因 | 尚无 nginx、容器或数据库日志闭环 | 直接原因仍为 `unproven`，不得把无界列表推断写成唯一 502 根因 |

### 2.3 分级与成功条件

- 等级：L2；优先级：P1；流程：`standard_team`。
- 影响生产核心读取链路，必须经过 dev、QA、product acceptance、Release Gate 和生产 E4 复核。
- 当前生产规模下，运营目标有界列表和任务有界列表各自响应目标小于 2 秒。
- 任一列表单页解码响应小于 100 KB。
- 任务创建 / 编辑弹窗打开后 2 秒内可操作；目标候选可以随后加载，但弹窗不能被全量目标请求阻塞。
- 生产连续刷新当前查询不出现 502；旧响应不覆盖新状态；失败继续可见。

## 3. 范围与非目标

### 3.1 本轮范围

1. `GET /api/operation-targets` 增加有界分页、远程搜索、ID 回显、关联群定点查询和能力筛选。
2. 运营目标关联账号计数改为“先分页目标，再对当前页目标做 SQL 条件聚合”。
3. `GET /api/operation-targets/runtime-summary` 支持按 `target_ids` 读取当前目标页摘要。
4. 新增 `GET /api/tasks/page`，统一分页普通 Task 和账号安全系统任务投影。
5. 任务列表搜索、统计和“目标群聊 + 关联频道”分组改为服务端语义。
6. 所有第一方运营目标消费者迁移到有界查询；任务中心列表迁移到新分页接口。
7. 前端保持 15 秒超时、请求序号和显式错误，任务中心每 60 秒轮询当前查询。

### 3.2 非目标

- 不增加或修改数据库迁移。
- 不改变 Planner、Dispatcher、Listener、Recovery、Metrics 或账号安全 worker 行为。
- 不改变任务详情、Action、Attempt、准入明细等既有详情下钻契约。
- 不删除旧 `GET /api/tasks`；兼容调用暂时保留，但第一方任务列表不再使用。
- 不删除 `GET /api/operation-targets` 的旧兼容语义。
- 不把前端 timeout 调大，也不在请求失败时静默回退旧全量接口。
- 不在本轮宣称 502 已定位或生产问题已修复；只有发布后的 E4 证据可以更新 `production_fixed`。

## 4. 运营目标有界列表契约

### 4.1 API 参数

`GET /api/operation-targets` 保留现有 `target_type`、`account_id`，新增：

| 参数 | 语义 |
| --- | --- |
| `page` | 页码，从 1 开始；有界模式缺省为 1 |
| `page_size` | 单页条数；有界模式缺省为 20，最大 100 |
| `q` | 对标题、username、TG peer id 做去首尾空格后的不区分大小写搜索 |
| `ids` | 可重复的正整数 ID 参数，例如 `ids=1&ids=2`，用于编辑态已选项回显；去重后参与租户过滤和分页 |
| `linked_group_id` | 只返回关联到指定 `TgGroup.id` 的目标，用于 AppShell 深链定点查 |
| `capability` | 单值枚举：`send`、`listen`、`archive`、`task` |
| `target_type` | 保留 `group/channel` 过滤 |
| `account_id` | 保留按当前账号可发送关系过滤 |

兼容判定必须明确：

- 请求未携带任何新增参数 `page/page_size/q/ids/linked_group_id/capability` 时，保留旧列表语义；原有 `target_type/account_id` 可继续按旧方式过滤并返回完整匹配集合。
- 只要出现任一新增参数即进入有界模式；缺少 `page/page_size` 时使用 `1/20`。
- 所有第一方消费者必须显式传 `page` 和 `page_size`，不得依赖兼容模式。
- 非法页码、page size、ID 或 capability 返回可见 4xx detail，不得忽略参数后退回全量。

### 4.2 响应与分页头

有界模式继续返回 `OperationTargetOut[]`，避免破坏既有目标类型；同时必须返回：

```text
X-Total-Count: 当前过滤条件下总数
X-Page: 实际页码
X-Page-Size: 实际单页条数
```

响应中的 `available_send_account_count`、`listener_account_count`、`can_listen`、`can_archive`、`can_task` 和 `task_capabilities` 保持既有字段语义。空页返回空数组和正确总数，不把越界页静默改成最后一页。

### 4.3 查询和聚合顺序

```text
tenant_id + deleted/类型/账号/q/ids/关联群/能力过滤
  -> COUNT 当前过滤总数
  -> 稳定排序 OperationTarget.id DESC
  -> offset/limit 取得当前页目标
  -> 只为当前页 target peer 批量读取关联 TgGroup
  -> 只为当前页 group_ids 做 SQL GROUP BY 条件计数
  -> 合并 OperationTargetOut
```

关联账号统计必须使用 SQL 条件聚合或等价的数据库聚合，例如按 `group_id` 计算可发送账号数和监听账号数。禁止把当前租户全部 `TgGroupAccount` ORM 行物化到 Python 后再分组；也禁止分页之后逐目标查询账号关系形成 N+1。

所有主查询、count、关联群和条件聚合都必须带当前用户 `tenant_id`。`ids`、`linked_group_id`、`account_id` 不得跨租户返回对象存在性。

### 4.4 运行摘要按页读取

`GET /api/operation-targets/runtime-summary` 新增可重复参数，例如 `target_ids=1&target_ids=2`：

- 只返回当前租户且 ID 命中的目标运行摘要。
- Overview 先取得当前目标页，再把该页 ID 传给 runtime-summary。
- 空 ID 集合不允许被解释成“全量”；Overview 当前页为空时不发请求或获得空数组。
- 旧无 `target_ids` 语义暂保留给兼容调用，但第一方 Overview 不再使用全量语义。

## 5. 任务列表分页契约

### 5.1 新 API

新增：

```text
GET /api/tasks/page?page=1&page_size=20&type=&status=&q=&group_key=
```

参数：

| 参数 | 语义 |
| --- | --- |
| `page` | 从 1 开始，缺省 1 |
| `page_size` | 缺省 20，最大 100 |
| `type` | 单一任务类型筛选，沿用现有类型值和系统任务类型值 |
| `status` | 单一任务状态筛选，沿用现有规范化状态语义 |
| `q` | 搜索任务 ID、名称和后端批量解析得到的目标 / 频道摘要 |
| `group_key` | 使用响应 `groups[].key` 精确过滤“目标群聊 + 关联频道”分组 |

响应模型为：

```text
TaskListPageOut
  items: TaskListItemOut[]
  total: int
  page: int
  page_size: int
  summary: TaskListSummaryOut
  groups: TaskListGroupOut[]
```

### 5.2 列表项边界

`TaskListItemOut` 只包含列表展示和列表动作判断所需字段：任务 ID、来源类型、名称、类型、状态、优先级、目标摘要、账号范围摘要、运行阶段、轻量统计、最近失败、下次运行时间、创建 / 更新时间、`group_key` 和详情入口所需标识。

列表项不得返回四类完整配置：

- `account_config`
- `pacing_config`
- `failure_policy`
- `type_config`

编辑和完整配置读取继续使用 `GET /api/tasks/{task_id}`。前端不得因列表项没有完整配置而回退为逐行详情请求；只有用户打开详情或编辑时才按单任务 ID 读取。

### 5.3 普通任务与系统任务统一集合

分页集合同时包含：

- `tasks` 表中的普通 Task。
- `tg_account_security_batches` 投影出的 `account_profile_init`、`account_device_cleanup`、`account_2fa_setup`、`account_standby_session_provision` 系统任务。

两个来源必须先投影到同一轻量索引集合，再共同过滤、排序、计数和分页。可以使用数据库 `UNION ALL`，也可以在查询次数和内存受控的前提下批量读取轻量索引后归并；禁止在分页前构造任一路完整 `TaskOut`、完整配置或逐行关联摘要。

稳定排序固定为：

1. `priority ASC`；
2. `created_at DESC`；
3. `source_kind ASC`，普通 Task 在同时间同优先级下先于系统任务；
4. 规范化 `stable_id DESC` 作为最终稳定键。

账号安全系统任务列表统计必须按当前候选 batch IDs 做一次 SQL 条件聚合，批量得到 total/success/failure/skipped/manual/pending/running/waiting-cache 和最近失败；禁止每个 batch 单独读取全部 items，消除当前批次 items N+1。

### 5.4 服务端搜索、统计和分组

处理顺序：

```text
tenant + deleted/system-visible
  -> type/status/q 基础筛选
  -> 计算 summary + groups（均不应用当前 group_key，保证统计和分组选项稳定）
  -> 可选 group_key 精确筛选
  -> 计算当前分组过滤后的 total
  -> 稳定排序和分页
  -> 为当前页批量补目标/频道摘要与 runtime summary
```

`summary` 基于 `type/status/q` 后、`group_key` 与分页前的集合生成，固定返回 `total/running/failed`；因此切换页码或快捷分组不会改变顶部统计卡。响应顶层 `total` 则表示应用 `group_key` 后的列表总数。`groups` 与 summary 使用相同基础集合，每项包含稳定 key、完整可读标签、任务数、目标群标识和关联频道标识；不能依赖当前页 items 反推全局统计或分组数量。

任务搜索和分组需要的目标 / 频道上下文必须数据库侧过滤或对候选 ID 批量解析。不能为每个任务逐条读取 `OperationTarget` 或 `ChannelMessage`，也不能为了 q/group 构造而返回完整四类配置。

### 5.5 兼容路径

- 旧 `GET /api/tasks` 暂保留现有兼容响应，不在本轮删除。
- TaskCenter 第一方列表、筛选、统计、快捷分组和轮询全部改用 `/api/tasks/page`。
- 不允许新接口失败后静默调用旧 `/api/tasks`；错误必须展示并保留最后一次成功数据，是否清空由既有页面错误契约决定。

## 6. 第一方页面行为

| 消费者 | 待实现有界行为 |
| --- | --- |
| `OperationTargetsView` | 服务端分页与远程搜索；翻页、搜索、刷新和写后刷新都绑定当前 query + 请求序号 |
| `TaskCenterView` 列表 | 使用 `/tasks/page`；分页、type/status/q/group_key、summary、groups 全部服务端处理；每 60 秒轮询当前查询 |
| 任务创建 / 编辑弹窗 | 点击后先打开弹窗；目标候选在弹窗内懒加载；输入搜索词走 `q`；已有目标用 `ids` 回显；不等待全量目标后再开弹窗 |
| `OverviewView` | 只取当前目标页；再用当前页 `target_ids` 读取 runtime summary；旧页响应不得覆盖新页 |
| `RulesCenterView` | 目标选择器实际打开或进入需要目标的区域时才懒加载；使用 `target_type=group + page/page_size + q` |
| `ArchivesView` | 新建归档目标选择器打开时才懒加载；使用 `target_type=group + page/page_size + q` |
| `MessageSendingView` | 按当前发送账号使用 `account_id + page/page_size + q` 远程读取目标；账号切换后旧响应不得覆盖当前账号 |
| `AppShell` | 关联群深链使用 `linked_group_id + page=1` 定点查，不再全量读取 group 目标后前端过滤 |

所有第一方调用都必须显式进入有界模式。Rules/Archives 的“懒加载”不是把全量请求延后执行；每次仍必须分页。任务编辑 `ids` 回显与远程 `q` 搜索结果合并时按 ID 去重，已选项不能因当前搜索页不包含它而丢失。

## 7. 并发、错误、权限与一致性

- 保持公共 API client 的 15 秒 timeout，不因性能问题改大。
- 列表请求身份至少包含 `page/page_size/filter/search/group/account/target ids` 与请求序号；只有最新请求可以更新 rows、total、summary、groups、loading 和 error。
- 任务中心 60 秒轮询使用用户当前查询快照；筛选或页码变化后旧 timer 响应失效。
- 弹窗目标搜索使用独立请求序号，不与列表、详情或保存动作共用 busy 状态。
- 后端 FastAPI `detail`、响应正文和 trace_id 继续经公共 `ApiError.message` 展示；不得吞掉 4xx/5xx 或只显示固定失败文案。
- 原写动作成功、后续有界列表刷新失败时，继续按既有契约区分“动作已完成”和“数据刷新失败”。
- `targets.view`、`tasks.view` 及既有写权限不变；新路由不得绕过 permission middleware。
- 普通 Task、账号安全 batch、OperationTarget、TgGroup、TgGroupAccount、runtime summary 的每条查询都必须按当前租户隔离。

## 8. 数据、迁移、worker 与发布

### 8.1 数据与运行影响

- 不新增表、字段、索引或 Alembic 迁移。
- 不改变任务、Action、Attempt、账号安全 batch item 或运营目标写入语义。
- 不改变 worker 角色、调度频率、任务执行和 runtime summary 刷新职责。
- 本轮只改变 API 读取投影、查询边界和前端消费方式。

### 8.2 发布与回滚

- 生产路径：`master -> release -> GitHub Actions Deploy Production`。
- Release Gate 必须包含后端定向测试、前端数据流测试、前端 build、静态检查与兼容路径测试。
- 无迁移，因此回滚方式为代码版本回滚；回滚后旧 API 语义恢复，不需要数据回滚。
- 回滚不会恢复新接口调用的可用性，因此前后端必须作为同一 release candidate 发布；不实现静默旧接口 fallback。

## 9. QA 与产品验收

### 9.1 后端 QA

- 运营目标分页头、过滤组合、稳定排序、空页、非法参数和租户隔离。
- 旧无新增参数调用仍保持兼容；所有新增参数进入有界模式。
- 运营目标只聚合当前页 group IDs，断言不全量物化 `TgGroupAccount`，并对查询次数设置回归门槛。
- runtime-summary `target_ids` 过滤、空集合和跨租户 ID。
- `/tasks/page` 同时覆盖普通 Task 与四类账号安全系统任务，验证稳定排序、跨来源分页、total、summary 和 groups。
- 任务列表 item 不包含四类完整 config；详情仍完整。
- 系统 batch item 统计为批量聚合，不随 batch 数线性增加查询。

### 9.2 前端 QA

- 运营目标真实服务端分页、搜索和请求序号。
- 任务中心使用服务端分页 / 筛选 / 分组 / 统计，60 秒轮询保持当前查询。
- 创建 / 编辑弹窗先打开，目标懒加载、远程搜索和 ids 回显均可见且不会被旧响应覆盖。
- Overview 当前页摘要、Rules/Archives 懒加载、MessageSending 按账号远程搜索、AppShell 定点查询。
- 任一读取失败显示后端 detail / trace_id，不出现 silent fallback 或不可见 Promise rejection。

### 9.3 生产 E4 验收

- 在真实登录态和当前生产数据规模下分别记录两个有界列表的 HTTP 状态、耗时、条数和 decoded body size。
- 两个有界列表各自小于 2 秒，单页小于 100 KB。
- 任务编辑弹窗 2 秒内可操作，已选目标正确回显，远程搜索可继续加载。
- 运营目标、任务中心、运营中心、规则中心、归档中心、消息发送和关联群深链均不再触发第一方无界目标读取。
- 连续刷新与 60 秒轮询期间零 502，旧响应不覆盖新查询。
- Release Gate 通过、部署成功或公网 health 通过都不能单独写 `production_fixed`；必须等待上述真实页面与接口证据。

## 10. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始需求与生产证据 | 已覆盖；502 直接原因明确保留为 unproven |
| 前端状态与全部消费者 | 已覆盖八类消费行为、弹窗先开、懒加载、分页、远程搜索和轮询 |
| 后端 API 与读模型 | 已覆盖两个列表契约、runtime-summary、跨来源任务分页和批量统计 |
| 数据流转与租户隔离 | 已覆盖 query、count、聚合、摘要和 ID 定点读取 |
| 失败路径与并发 | 已覆盖 15 秒 timeout、请求序号、显式错误和写后刷新失败 |
| 兼容边界 | 运营目标旧无新增参数语义和旧 `/tasks` 暂保留；第一方不再使用无界语义 |
| 幂等与数据一致性 | 本轮为只读查询，无写入幂等变化；排序和分页稳定键已固定 |
| 发布、迁移与回滚 | 无迁移、无 worker 影响、前后端同版本发布、代码回滚 |
| QA 与生产验收 | 已定义本地回归、Release Gate 和 E4 成功条件 |

结论：`design_status=complete`，`dev_handoff_ready=true`。截至本文日期，开发、QA、产品验收、Release Gate 和生产修复均未完成，不得把本设计作为上线事实。
