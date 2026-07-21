# 搜索排名观察任务与降权专用账号组硬化设计

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 设计状态 | Product Design Complete（2026-07-21 运营范围与节奏创建修订） |
| 设计日期 | 2026-07-20 |
| 任务类型 | `search_rank_deboost` |
| 账号组用途 | `pool_purpose=rank_deboost` |
| 发布等级 | L2，影响账号隔离、任务调度、代理出口和真实 Telegram 操作 |
| 上游真相源 | `docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/search-click-boost-prd.md` |
| 实现前置 | 本设计经用户确认后，按独立实现计划执行 TDD、迁移、QA 和 Release Gate |

本文解决当前实现中以下已确认问题：

1. 真实 `TelethonTelegramGateway` 尚未实现降权任务搜索、豁免群候选和按钮点击。
2. Runtime 把本地写统计当成真实点击成功。
3. `TgAccount.pool_id` 与 `account_identity` 可出现不一致，普通任务可能误选降权专用账号。
4. 普通账号无法原子迁移到降权专用组，新增账号直接选择专用组又不会同步身份。
5. 同租户无法通过 Web/API 创建多个降权专用组，降权任务也没有“全部专用账号”语义。
6. 消息发送、资料初始化、账号面具、2FA 和设备清理没有统一执行专用账号硬边界。
7. 任务创建拥有并重复创建分组代理绑定，形成一组只能创建一个任务的资源占用。
8. 限流按 action 校验，但一个 action 可写多次点击，存在越限空间。
9. 前端预览、测试替身和数据流索引把未实现的真实能力显示为已具备。

## 2. 产品目标与非目标

### 2.1 产品目标

- 一个租户可以维护多个降权专用账号组，每个账号始终只属于一个当前账号组。
- 降权专用账号只允许执行登录、授权资产维护、健康诊断和 `search_rank_deboost`，禁止进入其他运营动作。
- 普通任务选择“全部账号”时排除全部降权专用账号；降权任务选择“全部账号”时只选择所有启用降权专用组中的可用账号。
- 每个降权专用组持久绑定一个可执行代理运行端点，多个任务复用该绑定。
- 真实 Gateway 使用同一代理运行端点完成当前出口 IP 探测、Telegram 搜索和安全导航按钮点击。
- 每个 action 最多执行一次真实竞争群导航点击，点击前完成原子配额预占，点击后按 Gateway 事实写统计。
- 任务、账号、分组、代理、点击、失败和审计状态可从 Web 与数据库解释，不使用 mock success、直连 fallback 或静默降级。

### 2.2 非目标

- 不承诺点击会降低竞争群排名；产品名称继续使用“搜索排名观察任务”。
- 不加入竞争群、不关注频道、不投票、不发言、不点击外部 HTTP URL 或未知按钮。
- 不把账号改为多账号组归属；本期仍使用 `TgAccount.pool_id` 单一归属。
- 不复用 `search_join_group` 的 task、action、统计或代理绑定实例；只复用其 Telethon conversation、按钮解析和客户端生命周期模式。
- 不支持 Gateway 不可用时改走本机直连、账号级旧代理或授权槽位代理。
- 不在本设计阶段声明生产可用；生产可用必须由发布后真实 E4 证据证明。

## 3. 核心设计决策

### 3.1 账号用途事实源

`AccountPool.pool_purpose` 是账号用途真相源，`TgAccount.account_identity` 是与当前分组同步的执行期投影。

| 分组用途 | 账号身份投影 | 可参与任务 |
| --- | --- | --- |
| `normal` | `normal` | 普通运营任务，不含 `search_rank_deboost` |
| `code_receiver` | `code_receiver` | 不参与运营任务，只允许接码与授权资产维护 |
| `rank_deboost` | `rank_deboost` | 只允许 `search_rank_deboost` |

生产写路径不得分别修改两个字段。账号新增、账号移动、历史修复统一调用账号用途服务，在一个事务中完成：

```text
锁定账号和目标分组
  -> 校验同租户、分组启用、用途合法
  -> 计算目标 account_identity
  -> 更新 pool_id + account_identity
  -> 取消与新用途冲突的 pending 普通任务/旧消息动作
  -> reconcile 在线来源和任务范围
  -> 写前后快照审计
  -> commit
```

查询层仍执行双保险：普通任务同时排除 `account_identity in (code_receiver, rank_deboost)` 和专用 `pool_purpose/system_key`；降权任务同时要求身份与分组用途均为 `rank_deboost`。任何不一致状态都进入 `account_purpose_mismatch`，按更严格用途处理并禁止外部动作，直到修复完成。

### 3.2 单一用途策略模块

新增 `backend/app/services/account_usage_policy.py`，集中定义：

- `account_usage(account, pool) -> normal | code_receiver | rank_deboost | mismatch`
- `assert_account_action_allowed(account, pool, action_kind)`
- `apply_operational_account_filters(stmt)`
- `apply_rank_deboost_account_filters(stmt)`
- `sync_account_usage(session, account, target_pool, actor)`

允许矩阵：

| 动作 | normal | code_receiver | rank_deboost |
| --- | --- | --- | --- |
| 登录、重新登录 | 允许 | 允许 | 允许 |
| 授权资产诊断、备用 session 补齐/自愈 | 允许 | 允许 | 允许 |
| 只读设备诊断、健康探测 | 允许 | 允许 | 允许 |
| Telegram 官方验证码读取 | 允许 | 允许 | 允许 |
| 普通任务、消息发送、监听、目标准入 | 允许 | 禁止 | 禁止 |
| 资料初始化、账号面具初始化 | 允许 | 禁止 | 禁止 |
| 2FA 设置/轮换、设备清理 | 允许 | 禁止 | 禁止 |
| `search_rank_deboost` | 禁止 | 禁止 | 允许 |

该策略必须接入 API 预检、Service 写入、Planner、Dispatcher、Listener、Recovery、旧 MessageTask/Campaign、账号登录后自动任务和账号安全 worker。前端隐藏或禁用只用于减少误操作，不作为安全边界。

## 4. 账号组模型与生命周期

### 4.1 数据模型

`account_pools` 新增：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `is_enabled` | boolean | true | 是否允许新任务和新账号选择；禁用不改变组内账号用途 |
| `disabled_at` | datetime nullable | null | 禁用时间 |
| `disabled_by` | string | 空 | 禁用操作者 |
| `disable_reason` | string | 空 | 禁用原因 |

现有字段继续使用：

- `is_default` 只表示普通账号默认归属，不再承担“启用/禁用”含义。
- `is_system=true, system_key=rank_deboost` 表示租户内默认降权专用组；同租户最多一个系统默认组。
- 自定义降权专用组使用 `is_system=false, system_key=''`；同租户可存在多个，名称保持唯一。
- `pool_purpose` 创建后不可修改，防止普通组被原地转换后留下身份和任务污染。

### 4.2 分组操作

允许：

- 创建多个降权专用组。
- 重命名和修改说明。
- 禁用空组或非空组；非空组禁用后账号仍保持 `rank_deboost`，不自动回流普通任务。
- 重新启用分组。
- 在降权专用组之间移动账号。
- 在普通组与降权专用组之间原子转换账号用途。

禁止：

- 删除 `code_receiver` 或 `rank_deboost` 分组。
- 删除仍有账号、active 代理绑定或运行中任务引用的普通分组。
- 将禁用分组用于新增账号、账号迁入或新任务范围。
- 将 `pool_purpose` 从一个用途修改为另一个用途。

## 5. API 契约

### 5.1 账号组 API

| API | 语义 |
| --- | --- |
| `POST /api/account-pools/rank-deboost` | 请求包含 `name` 时创建自定义降权专用组；兼容期无 body 调用仍幂等返回系统默认组并记录 deprecated usage，前端新入口不再使用无 body 形式 |
| `POST /api/account-pools/rank-deboost/default` | 幂等确保租户默认系统降权组，兼容首次初始化 |
| `PATCH /api/account-pools/{pool_id}` | 更新名称、说明、`is_enabled`；禁止修改用途和系统标识 |
| `POST /api/tg-accounts/{account_id}/move-pool` | 原子迁移账号并同步身份，返回用途修复/冲突动作取消摘要 |
| `PUT /api/account-pools/{pool_id}/rank-deboost-proxy-binding` | 创建、复用或显式切换分组代理绑定 |
| `DELETE /api/account-pools/{pool_id}/rank-deboost-proxy-binding` | 显式解绑；有 running/paused 任务引用时拒绝 |
| `GET /api/account-pools` | 返回用途、启用状态、账号数和降权分组代理绑定摘要 |

创建降权分组请求：

```json
{
  "tenant_id": 1,
  "name": "排名观察黑账号组 A",
  "description": "只用于 search_rank_deboost"
}
```

分组代理绑定请求：

```json
{
  "proxy_airport_node_id": 123,
  "change_reason": "为黑账号组 A 配置独立出口"
}
```

### 5.2 降权任务账号范围（系统策略与存量兼容）

已保存的旧任务可继续拥有标准化 `account_config`，用于读取、编辑已存在配置和执行事实兼容：

```json
{
  "selection_mode": "all",
  "account_group_id": null,
  "account_ids": [],
  "max_concurrent": 10
}
```

语义：

| selection_mode | 降权任务候选范围 |
| --- | --- |
| `all` | 当前租户所有启用 `rank_deboost` 分组中的一致、可用账号 |
| `group` | 指定的一个启用 `rank_deboost` 分组中的一致、可用账号 |
| `manual` | 手动 ID 中位于启用 `rank_deboost` 分组且身份一致的账号 |

普通任务使用同名模式，但 `all`、`group`、`manual` 都必须排除 `code_receiver` 和 `rank_deboost`。新建 `search_rank_deboost` 只接收一个 `account_group_id`，服务端将其投影为 `selection_mode=group`；不接受 `all`、手动账号或完整 `account_config`。前后端共用相同候选摘要字段：

```json
{
  "configured_count": 20,
  "eligible_count": 16,
  "excluded_by_purpose_count": 4,
  "excluded_pool_ids": [31],
  "missing_binding_pool_ids": [32],
  "disabled_pool_ids": [33]
}
```

### 5.3 2026-07-21 运营范围与节奏创建 API（当前生效）

`POST /api/tasks/search_rank_deboost` 的新建请求包含 `target_title`、`target_link`、`keywords`、`target_count`、`account_group_id`、`max_actions_per_day`、`scheduled_end`、`daily_jitter_percent`、`hourly_jitter_percent` 和可选 `quiet_hours`。`target_title` 与 `target_link` 共同表示一个我方目标群；链接必须可归一化为公开 Telegram username。服务端按该 username 解析或复用内部 `OperationTarget`、将其转换为内部 `target_group_ids`，保存提交名称的任务展示快照并生成任务名称。`account_group_id` 必须指向当前租户启用的 `pool_purpose=rank_deboost` 分组，服务端保存为 `Task.account_config.selection_mode=group`，并在启动准备时只从该黑账号组选择可用账号；邀请链接、peer id、机器人链接和裸 `target_operation_target_id` 必须明确拒绝，存量任务继续使用既有内部引用。

`max_actions_per_day` 是任务自然日的 click reservation/action 预算，独立于 `target_count`；`scheduled_end` 是任务真实停止边界；日/小时抖动只能重排 future action 的 `scheduled_at`，不突破任务日/小时限额、静默窗口、截止时间或已有账号/关键词/共享 IP 配额。静默时段按任务时区不创建 action，下一轮调度落在静默结束后；reservation 到期时间必须覆盖带抖动的 `scheduled_at`。创建只生成 draft；启动准备、真实候选搜索和可执行 binding 校验仍由服务端完成，失败时返回 blocker，不要求运营反向补填系统字段。

`proxy_airport_node_id`、机器人、单账号点击日限额、冷却、停留区间、并发、重试与准备态策略均为系统策略，不是新建 API 或前端创建页的可调字段；额外传入这些字段必须被明确拒绝。

`target_count` 表示任务生命周期的 factual confirmed click 数。已确认 action 计入完成；`pending`、`claiming`、`executing` 和未知 Gateway 结果继续占用一个槽位；明确无点击、跳过和失败释放槽位。达到已确认目标时任务设为 `completed` 并清除下一次运行时间；历史无该字段任务保留既有调度行为。

## 6. 分组代理绑定设计

### 6.1 绑定归属

`AccountGroupProxyBinding` 是账号组资产，不属于单个任务。任务只在 action payload 中记录绑定 ID 与 generation 快照。

- 同一启用分组最多一个 active 绑定。
- 多个降权任务可以复用同一分组 active 绑定。
- 任务创建不得无条件新建绑定。
- 停止或删除任务不得自动解绑共享绑定。
- 显式换节点时旧绑定进入 `unbound`，新绑定 generation + 1；使用旧 generation 的 pending action 全部 skipped 并重排。
- 禁用分组时暂停引用该分组的任务 action，但不静默解绑。

`account_group_proxy_bindings` 新增：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `runtime_proxy_id` | FK nullable | 可被 Telethon/HTTP 客户端执行的 SOCKS/HTTP 代理端点 |
| `last_probe_at` | datetime nullable | 最近一次同端点出口探测时间 |
| `last_probe_error` | string | 最近探测失败原因 |

### 6.2 可执行代理闸门

机场订阅中的原始 VMess、VLESS、Shadowsocks 等协议节点不能直接伪装成 Telethon SOCKS 代理。绑定时必须满足以下一种条件：

1. 节点本身提供 `socks5`、`socks4`、`http` 或 `https` 可执行端点；或
2. Clash/sing-box materializer 已为该节点提供独立本地 SOCKS/HTTP 运行端点，并写入 `runtime_proxy_id`。

没有 `runtime_proxy_id`、协议不受支持、端口不可达或订阅失效时，绑定预检失败。不得只凭原始节点 TCP 可连通就标记可执行。

### 6.3 当前出口探测

新增显式配置 `RANK_DEBOOST_EGRESS_PROBE_URL`。Gateway 在每个 action 的 Telegram 连接前：

1. 使用 `runtime_proxy_id` 发起 HTTPS 出口探测。
2. 解析当前 `observed_exit_ip`，写 `proxy_exit_ip_observations`，`check_source=rank_deboost_action`。
3. 与 active group binding 最近可信出口比较。
4. 使用同一代理凭证创建/获取 Telethon client；客户端缓存键必须包含代理指纹。
5. 探测缺失、解析失败、出口漂移或代理连接失败时，action `skipped`，不建立直连客户端。

探测 URL 不可用属于显式外部依赖失败，不允许回退绑定旧 IP 自证。

## 7. 真实 Telegram Gateway 设计

### 7.1 模块边界

新增：

- `backend/app/integrations/telegram/search_rank_deboost.py`：纯 Telethon conversation、结果解析、安全点击和 outcome 生成。
- `TelethonTelegramGateway.search_rank_deboost_candidates(...)`：为任务豁免群准备态执行真实候选搜索。
- `TelethonTelegramGateway.execute_search_rank_deboost(...)`：执行单 action 的探测、搜索和最多一次安全点击。

业务服务负责账号用途、任务状态、配额、绑定和数据库写入；Gateway 不直接写业务表。

### 7.2 Gateway 输入

```python
execute_search_rank_deboost(
    account_id: int,
    payload: dict,
    session_ciphertext: str,
    credentials: DeveloperAppCredentials,
    keyword_text: str,
) -> dict
```

`payload` 必须包含：

- `bot_username`
- `target_group_ids`
- `target_reference_type`：新建简化任务固定为 `operation_target`，用于说明 `target_group_ids` 引用的是 `OperationTarget.id`；历史未带类型的数据只可在唯一可解析时继续运行，若同一整数同时对应不同的 `OperationTarget` 与 `TgGroup`，必须返回明确 blocker，不能覆盖选一个。
- `exempt_group_username`
- `max_clicks=1`
- `dwell_seconds_min/max`
- `group_proxy_binding_id`
- `binding_generation`
- `account_pool_id`
- `keyword_hash`

Dispatcher 使用账号开发者应用凭证和分组 `runtime_proxy_id` 生成不可变 `DeveloperAppCredentials`，不得使用账号旧 `proxy_id`。

准备态和运行态共用同一授权解析器：解析账号主 session、开发者应用和 active `AccountGroupProxyBinding.runtime_proxy_id`，并校验 tenant、账号分组、binding id/generation 与 proxy id 一致。候选搜索和 action 执行都必须按以上五参数调用真实 Gateway；不得通过动态注入不存在的方法、只传前三个参数或使用默认凭证替代。

### 7.3 执行步骤

```text
用途与 action/binding generation 最终守卫
  -> 通过分组运行代理探测当前出口 IP
  -> 使用同一代理指纹获取 Telethon client
  -> 校验 session authorized
  -> conversation(@jisou)
  -> /start
  -> 发送解密后的关键词
  -> 解析当前页并最多受控翻到后续 10 页
  -> 找到我方目标群实时位置
  -> 排除我方目标群、任务豁免群和排名更低结果
  -> 将按钮和解析出的竞争群身份/位置精确绑定，再对剩余候选执行 effect allowlist
  -> 最多执行 1 个 Gateway 已证明可执行的 navigate_only 动作
  -> 在合法的 min/max 区间内随机等待配置停留时间并测量耗时
  -> 复核没有加入目标实体
  -> 返回逐点击 outcome 和当前出口证据
```

禁止行为：

- 不点击 `join_candidate`、`external_http_url`、`unknown`。
- 不调用 JoinChannelRequest、ImportChatInviteRequest、关注、投票或发送消息。
- 不因页面无安全按钮而返回成功点击。
- 不在 Gateway 内自行增加点击预算。
- 按钮不能精确绑定到允许竞争群时，只返回明确的无点击原因（如 `no_navigable_button`、`all_exempt_clicks` 或 `target_not_in_results`）；不得按按钮顺序、`search_results[0]` 或本地推演补造竞争群、点击或停留事实。
- 当前 Telethon 适配器中，`KeyboardButtonUrl` 的 `message.click()` 只返回 URL 字符串，并不在 Telegram 会话内完成导航；因此该按钮不能产生 `confirmed`，必须直接返回 `no_navigable_button` 且不调用 `page.click()`。未来要支持这类动作，必须接入能返回同一动作事实的可执行 Gateway，而不是把 URL 返回值当作点击成功。

### 7.4 Gateway 输出

```json
{
  "success": true,
  "execution_status": "confirmed",
  "observed_exit_ip": "1.2.3.4",
  "target_position": 5,
  "exempt_position": 2,
  "pages_scanned": 1,
  "click_outcomes": [
    {
      "status": "confirmed",
      "competitor_username": "group_a",
      "competitor_peer_id": "-100123",
      "competitor_title": "Group A",
      "position": 1,
      "button_hash": "sha256:...",
      "button_effect": "navigate_only",
      "join_button_detected": true,
      "joined": false,
      "dwell_seconds": 18
    }
  ]
}
```

`execution_status` 枚举：

| 状态 | Action 结果 |
| --- | --- |
| `confirmed` | 一个真实 `confirmed` 点击，本期最多一个，Action success |
| `observed_no_click` / `no_navigable_button` / `all_exempt_clicks` | 完成搜索但没有安全候选，Action skipped，保留观察事实 |
| `target_not_in_results` | 我方目标未出现，Action skipped |
| `target_identity_missing` | action 没有可验证目标身份，Action skipped；正常链路应在启动或 Planner 阶段阻断 |
| `human_verification_required` | 机器人要求人机验证，Action failed/blocked |
| `proxy_egress_guard_failed` | 当前出口无法证明，Action skipped 并告警 |
| `button_contract_changed` | 按钮结构不在样本允许范围，Action failed 并暂停任务 |
| `join_button_violation` | 自检发现已加入，Action failed、账号 limited、立即告警 |
| `gateway_failed` | Telegram/session/RPC 明确失败，Action failed |
| `unknown_after_click` | 已进入 Gateway 调用边界后，`message.click` 结果、出口事实、返回结构或完整点击事实无法确认；不自动重试，进入 recovery |

Runtime 只根据唯一的 `click_outcomes.status=confirmed` 写 `SearchRankDeboostActionStat`，统计身份、位置、button hash/effect、`joined=false`、实测 `dwell_seconds` 和 Gateway 返回的出口证据均来自同一个 outcome。所有无点击状态（`observed_no_click`、`no_navigable_button`、`all_exempt_clicks`、`target_not_in_results`、`target_identity_missing`）及 `unknown_after_click` 都不得计为点击成功。

## 8. Planner、配额与 Action 设计

### 8.1 每个 Action 最多一次点击

本期固定 `max_clicks_per_action=1`，不暴露可配置多点击。一个 action 对应：

- 一个账号
- 一个关键词
- 一个账号组及绑定 generation
- 最多一个真实竞争群导航点击

这样 `max_actions_per_hour` 与最大真实点击数一致，避免 action 级节奏和点击级风险分离。

### 8.2 点击预占表

新增 `search_rank_deboost_click_reservations`：

| 字段 | 说明 |
| --- | --- |
| `id` | UUID |
| `tenant_id`、`task_id`、`action_id` | 任务和 action |
| `account_id`、`account_pool_id` | 账号和共享 IP 分组 |
| `keyword_hash` | 关键词哈希，不保存明文 |
| `local_date`、`hour_bucket` | 租户时区窗口 |
| `reserved_count` | 本期固定 1 |
| `consumed_count` | confirmed 点击数，0 或 1 |
| `status` | `reserved/consumed/released/unknown` |
| `expires_at` | 仅限尚未进入 Gateway 的 pending action 回收窗口 |

约束与索引：`action_id` 全局唯一；按 `(tenant_id, account_id, local_date, status)`、`(tenant_id, account_id, keyword_hash, local_date, status)`、`(tenant_id, account_pool_id, local_date, status)` 和 `(task_id, hour_bucket, status)` 建组合索引。配额检查、reservation 插入和 action 插入必须处于同一事务，并持有同租户配额锁及 task planning lock；跨任务共享维度不能只依赖进程内锁。

Planner 在持有锁时原子检查并创建 reservation；限额查询计算 consumed、unknown，以及未过期或已进入非 pending 状态的 reserved，避免未知点击被提前释放：

- 单账号每日点击上限：跨同租户全部降权任务。
- 同账号同关键词每日上限：跨同租户全部降权任务。
- 分组共享 IP 每日上限：跨同租户全部降权任务，按 `account_pool_id` 聚合。
- 任务每小时上限：按 task 聚合。
- 单账号冷却：跨同租户全部降权任务。

Gateway 调用前先校验 reservation；账号、payload、代理绑定等任一预检失败、pending action 的过期 reserved，以及 ExecutionAttempt 尚未记录 Gateway 边界的 stale executing action，都必须释放 reservation，不能进入 Telegram。只有 Gateway 明确声明无点击的协议状态才可在调用后 release；点击确认后为 consumed；调用边界后的异常、已记录 Gateway 边界的 worker 失联、`unknown_after_click`、出口证据不一致、非对象返回、未知状态和 confirmed 缺少完整点击事实都转为 unknown 并继续占用配额，Action 标为 `unknown_after_send`，禁止自动重试。released action 手工重试前必须再次通过共享配额检查。

`dwell_seconds_min/max` 必须是 `1..600` 内的整数，且 `max >= min`；Planner 不为倒置或越界区间创建 action，Gateway 也会在点击前复核。

### 8.3 Planner 账号范围

Planner 按任务 `account_config` 解析候选，再逐账号读取其分组 active binding。以下情况不创建 action并写可见 blocker：

- 分组禁用。
- `account_identity` 与 `pool_purpose` 不一致。
- 分组缺少 active/runtime proxy binding。
- 任一 `target_group_ids` 不能解析为本租户公开 username；不得创建仅会在运行期 `target_identity_missing` 的 action。peer id 可保留为审计辅助身份，但不能单独驱动搜索结果匹配。
- 绑定 generation 变化。
- 账号非 active、session 无效、开发者应用不可用。
- 协议样本门槛不满足。
- 配额或冷却已满。

## 9. 任务准备与状态机

### 9.1 创建草稿

`POST /api/tasks/search_rank_deboost` 只创建 draft，不拥有代理绑定，也不在创建事务中探测账号、代理或 Gateway。调用方选择一个黑账号组及任务日预算/截止时间/抖动/静默窗口；服务端校验目标群公开 username、关键词、总目标和账号组用途，把账号组投影为 `Task.account_config.selection_mode=group`、把节奏写入 `Task.pacing_config` 并把 readiness 写为 `pending`。豁免群可以暂为 `pending_real_search`，详情直接展示该已持久化记录；前端按钮必须称为“创建草稿”，不得显示为已启动。

### 9.2 准备并启动

`start_task` 对 draft 执行：

```text
锁定任务
  -> 重算账号范围和分组绑定
  -> 解析全部我方目标群的公开 username（peer id 仅作审计辅助）
  -> 选择准备账号（用途一致、session/绑定可用）
  -> Gateway 真实候选搜索
  -> 持久化真实随机豁免群
  -> Gateway readiness probe（同代理出口 + bot response contract）
  -> 重新校验协议样本、配额和绑定 generation
  -> Task running + next_run_at
  -> 单次 commit
```

失败时任务保持 draft，`stats.rank_deboost_readiness` 写 blocker、时间和证据摘要；不创建新的分组绑定，不留下 action 或点击 reservation。

### 9.3 create_and_start

`POST /api/tasks/search_rank_deboost/create_and_start` 仅保留为兼容拒绝入口，固定返回“只能先创建草稿，再由服务端准备并启动”的明确 400，不创建 task、binding 或 reservation。第一方前端只调用创建草稿接口，随后由用户显式调用 `start_task`；这样失败 blocker 可以回写到既有 draft 并在详情中复核。

### 9.4 编辑、暂停、停止和删除

- 编辑页可修改目标群、关键词、目标次数、黑账号组、任务日预算、完成截止时间、日/小时抖动和静默时段；目标群填写完整名称和公开 Telegram 链接，不选择或回传内部 `OperationTarget` ID。代理、机器人、单账号点击限额、停留、并发和重试均为系统托管字段。
- 编辑目标群或关键词的实际值发生变化时，清理未来 pending action、释放未开始 reservation、把 readiness 置为 `pending`、将豁免群重置为 `pending_real_search`，并把任务回到 draft，下一次显式启动必须重新完成真实候选搜索与准备校验。前端可省略未修改字段，服务端仍需比较归一化后的值，不能因重发相同内容误重置。
- 仅编辑目标次数或运营范围/节奏会清理未来 pending action 并按既有 factual confirmed click 立即重算完成态；未完成任务回到 draft，等待显式启动。仅改日预算、截止时间、抖动或静默时段不重选豁免群、不重做 Gateway readiness；变更黑账号组只写 `required_check=account_group_binding`，启动时仅复验该组 binding，不改变已确认事实。
- 计划 action 的 reservation 必须按其 `scheduled_at` 对应的任务本地日期和小时桶计数，而不是按 Planner 当前时间计数；日抖动把计划分散到任务时区剩余自然日，小时抖动只在选中本地小时内延后。创建前、Planner 配额校验和 reservation 写入必须使用同一计划窗口。
- `scheduled_end` 与静默窗口都有 Planner、Dispatcher 和紧邻 Gateway 的三重边界；任务状态也必须在最终 Gateway 边界重读且为 `running`。截止、静默或任务变为暂停/停止/草稿/完成/删除后，尚未进入 Gateway 的 action 写对应 skip 原因（任务状态为 `task_not_active`）并释放黑搜索 reservation；已写 `gateway_call_started_at` 的 action 保留真实结果状态，不被配置更新、截止、静默或后续任务状态改写。
- 编辑目标群或目标次数后，服务端按当前目标群和目标次数重生成系统任务名称。
- 暂停不解绑共享分组代理；pending action skipped/released。
- 停止和删除不解绑共享分组代理；只清理本任务 action、reservation、告警和 readiness。
- 分组禁用或绑定切换时，所有引用任务进入 paused/blocked，必须人工确认后 resume。

## 10. 前端设计

### 10.1 账号中心

新增/调整：

- “新增账号分组”支持选择“普通账号组 / 排名观察专用组”；接码专用组仍由系统确保。
- 降权专用组展示专用标识、启用状态、账号数、代理绑定节点、当前出口、最近探测和引用任务数。
- 分组详情提供启用/禁用、配置/更换代理绑定、批量迁入/迁出账号。
- 账号新增和移动分组时展示用途变化确认；从普通转为降权时明确将取消普通 pending 动作。
- 禁用降权组后，不提供普通任务回流；必须显式迁出账号。

### 10.2 普通任务向导

- “全部账号”预览排除接码与降权专用账号，数量与后端预检一致。
- 账号组选择禁用专用组。
- 手动账号选项对专用账号禁用并展示用途原因。
- 提交后仍以后端双保险为准。

### 10.3 降权任务向导

创建向导固定为“任务类型 → 目标群 → 关键词与目标次数 → 执行范围与节奏 → 确认”。运营人员填写一个我方目标群的完整名称和公开 Telegram 链接、输入关键词和目标次数，并选择一个启用黑账号组、每日执行上限、完成截止时间、日/小时抖动和可选静默时段；系统自动解析内部目标记录，并仅从该分组选择符合配额与风险闸门的账号。

创建草稿不做真实账号、binding 或 Gateway 探测；这些检查在显式启动准备时执行，失败 blocker 写回该 draft。创建后的详情展示已持久化的豁免群、readiness 和执行事实。任务配置不填写单个 `proxy_airport_node_id`，不展示手动账号、并发、单账号限额、停留或重试配置；代理属于分组资产，其他运行参数属于系统策略。存量任务编辑页同样展示并可修改已保存的运营范围与节奏，账号范围事实仅在详情只读展示。

### 10.4 任务详情

新增 readiness 与真实执行摘要：

- 账号选择模式、涉及分组、配置/可用账号数。
- 每组 active binding、generation、当前出口、最近探测时间。
- 豁免群来源、选择账号、选择时间和搜索证据摘要。
- `GET /api/tasks/{id}` 直接返回当前持久化的豁免群；“重选随机豁免群”只执行重选，不作为读取当前事实的前置动作。
- `observed/clicked/observed_no_click/unknown_after_click/failed` 分开统计。
- 配额预占、已消费、unknown 占用和下一恢复时间。
- Gateway contract/version 和最近按钮结构变化。
- 目标进度：已确认点击 / 目标次数、待确认槽位、剩余可规划槽位及 `target_count_reached` 完成原因。

## 11. 数据迁移与兼容

新增迁移 `0087_search_rank_deboost_hardening.py`：

1. 为 `account_pools` 增加启用/禁用字段，存量全部 `is_enabled=true`。
2. 为 `account_group_proxy_bindings` 增加 `runtime_proxy_id`、探测与引用投影字段。
3. 创建 `search_rank_deboost_click_reservations` 和限额查询索引。
4. 按当前 `pool_id -> pool_purpose` 回填 `tg_accounts.account_identity`：code receiver、rank deboost、normal。
5. 对身份与分组不一致、分组不存在或跨租户引用写迁移审计；无法自动判断的账号标记 `account_purpose_mismatch` 并禁止外部动作。
6. 存量 rank task 的单个 `account_pool_id` 转换为 `account_config.selection_mode=group`。
7. 存量 active group binding 保留，但 `runtime_proxy_id` 为空时标记 `needs_runtime_proxy`；任务不能启动，直到显式重新绑定。
8. 存量 `pending_real_search` 草稿保持 draft；任何历史 running rank task 暂停并写 `migration_requires_gateway_revalidation`。
9. 历史点击统计保持只读，不反向伪造 reservation。

兼容期读取旧 `type_config.account_pool_id`，但所有新写入只保存标准 `account_config`；兼容读取在完成生产迁移和存量任务重存后删除，不长期保留双写。

## 12. 错误、告警与审计

新增或规范错误码：

| 错误码 | 处理 |
| --- | --- |
| `account_purpose_mismatch` | 阻断账号所有外部动作，进入修复队列 |
| `rank_deboost_pool_disabled` | 不规划新 action，暂停引用任务 |
| `rank_deboost_runtime_proxy_missing` | 阻断启动/执行 |
| `rank_deboost_proxy_protocol_unsupported` | 阻断绑定 |
| `proxy_egress_probe_unavailable` | skipped + 告警，不回退 |
| `proxy_egress_guard_failed` | skipped + 分组告警 |
| `rank_deboost_gateway_contract_changed` | failed + 暂停任务 |
| `rank_deboost_click_reservation_conflict` | 不创建 action，下一轮重算 |
| `unknown_after_click` | 不自动重试，进入 recovery |
| `rank_deboost_join_button_violation` | failed + 账号 limited + 高优先级告警 |

审计必须覆盖：分组创建/启禁用、账号用途迁移、冲突动作取消、代理绑定创建/复用/切换/解绑、真实豁免群重选、任务启动准备、reservation 状态变化、Gateway contract 变化和人工恢复。

日志不得记录关键词明文、session、代理密码、订阅 URL、callback_data 明文或完整手机号。关键词只记录 hash，按钮只记录稳定 hash 与 effect。

## 13. 测试与验收

### 13.1 后端单元/集成测试

- 新增账号直接选择降权组时，`pool_id/account_identity` 原子一致。
- 普通账号可迁入降权组，降权账号可显式迁出；冲突 pending 动作被取消并审计。
- 创建多个降权组、禁用/启用、不可删除和用途不可变。
- 普通任务 all/group/manual 均排除专用账号；降权任务 all/group/manual 只选择启用降权账号。
- MessageTask/Campaign、资料初始化、账号面具、2FA、设备清理、Listener、Planner、Dispatcher 和 Recovery 全部执行统一用途策略。
- 多个任务复用同一分组 binding；停止/删除任务不解绑；运行中引用阻止显式解绑。
- 不支持协议、缺 runtime proxy、出口探测失败和 IP 漂移全部 fail closed。
- 真实 `TelethonTelegramGateway` 类显式定义两个 rank 方法；测试不得使用 `raising=False` 注入不存在的方法。
- Gateway 只处理身份精确绑定且自身可证明执行的 `navigate_only` 动作；Telethon `KeyboardButtonUrl` 仅用于身份解析，不能因 URL 返回值记为点击。私密 `+invite` / `joinchat/`、外部、多段 URL、join/external/unknown 或未绑定按钮一律不能执行。
- confirmed 点击才写 stat；观察无点击不计成功；点击后未知进入 unknown。
- action 最多一个点击；reservation 防止并发任务突破账号、关键词、分组 IP 和任务小时上限。
- create_and_start 失败不留下 task、binding 或 reservation。

### 13.2 前端验收

- 可创建多个降权专用组并启禁用。
- 账号新增/迁移后用途标识立即更新。
- 普通任务预览、组选择和手动选择均不包含可提交的降权账号。
- 新建降权任务显示目标群（完整名称 + 公开 Telegram 链接）、关键词、目标次数、黑账号组、每日执行上限、完成截止时间、日/小时抖动和静默时段；提交体不含内部目标 ID、完整 `account_config`、代理、机器人、手动账号、单账号限额、停留或重试字段，额外字段被服务端明确拒绝。
- 系统候选范围只统计启用降权组；缺 binding、协议样本或公开 username 时明确展示 blocker。
- 详情区分观察、真实点击、无安全按钮、未知和失败。

### 13.3 真实环境验收

本地测试和 CI 通过只记 `qa_pass`，不记 `production_fixed`。生产 E4 至少需要：

1. 一个启用降权专用组和 1-2 个灰度账号，身份/分组一致。
2. 分组 runtime proxy 实时探测出口与绑定一致，Telethon client cache 使用相同代理指纹。
3. 真实 `@jisou` 搜索取得目标排名和真实豁免群。
4. 至少一次 `navigate_only` 按钮真实点击，Gateway outcome、Action、ExecutionAttempt 和 Stat 可互相对齐。
5. 证明未加入竞争群、未走本机直连、未触发普通消息/资料/2FA/设备动作。
6. 普通 AI 活群和评论任务选择“全部账号”时不包含灰度账号。
7. unknown、按钮变化或出口漂移场景至少验证一个 fail-closed 证据。

## 14. Release Gate 与回滚

发布顺序：

1. 迁移与用途回填。
2. 后端统一用途策略和 API。
3. 分组代理运行端点与 Gateway。
4. Planner/Dispatcher/reservation。
5. 前端账号中心与任务向导。
6. CI、灰度发布、真实任务 E4 验收。

Release Gate 必须检查：

- 数据迁移冲突数量为 0 或全部进入可解释修复队列。
- 普通任务账号隔离回归通过。
- 真实 Gateway 方法存在且契约测试调用生产类。
- 出口探测不使用绑定旧 IP 自证。
- 逐点击 reservation 与 unknown 语义通过并发测试。
- 前端构建和权限门禁通过。

回滚时保留新增字段、用途回填、审计和历史统计，不降级为旧的身份分裂逻辑。关闭新任务创建与 Dispatcher rank 分支，暂停所有 rank task；不得通过删除迁移表或恢复直连来回滚。

## 15. Product Design Complete 自检

- 原始需求：多个黑账号组、普通任务全选排除、降权任务只取黑账号，以及新建填写目标群完整名称与公开链接、关键词、目标次数、黑账号组、每日执行上限、完成截止时间、日/小时抖动和静默时段，已覆盖。
- 功能设计：分组、账号迁移、任务选择、绑定、Gateway、限流、详情和告警已覆盖。
- 前端状态：创建、启禁用、选择、缺口、readiness 和真实结果已覆盖。
- 后端/API/worker：schema、service、planner、dispatcher、gateway、recovery 和迁移已覆盖。
- 数据流转：账号用途、任务准备、代理出口、真实点击、统计和审计已覆盖。
- 权限安全：统一后端硬边界、敏感信息、无 fallback 和外部动作 allowlist 已覆盖。
- 边界场景：身份冲突、禁用组、缺绑定、协议不支持、出口漂移、按钮变化、unknown 和并发配额已覆盖。
- 发布风险：迁移、灰度、E4 证据和回滚已覆盖。
- 设计不存在未决产品项；进入开发前只需要用户确认本设计，不再需要补产品决策。
