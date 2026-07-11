# Findings

## Confirmed Root Causes

- `TelethonTelegramGateway` 没有 `execute_search_rank_deboost` 和 `search_rank_deboost_exempt_candidates`。
- Runtime 当前只写点击统计，没有真实按钮点击调用。
- `TgAccount.pool_id` 与 `account_identity` 是双事实源，账号创建不会根据分组同步身份，冲突校验也未接入生产写路径。
- 普通账号到降权账号的原子迁移被错误拦截。
- API 只确保单个系统降权分组，服务层的多分组创建函数未暴露。
- 普通消息、资料初始化、2FA 和设备清理等入口只拦截接码账号，没有统一拦截降权账号。
- 每次建任务都创建分组代理绑定，导致已有 active 绑定时无法创建第二个任务。
- Planner 按 action 校验配额，Runtime 可在一个 action 内处理多个点击目标。
- 前端普通任务预览和手动选择仍包含降权账号。

## Reusable Existing Patterns

- `backend/app/integrations/telegram/search_join.py` 已实现 Telethon conversation、搜索翻页、按钮解析和真实 `message.click`。
- `TelethonClientLifecycle` 的缓存键包含代理指纹，可以为同一 session 的不同分组代理维护独立客户端。
- `DeveloperAppCredentials` 已包含 SOCKS/HTTP 代理字段，可在 Dispatcher 解析分组绑定后生成覆盖代理的凭证。
- `proxy_for_airport_node` 可将可执行机场节点投影为 `AccountProxy`，但 Gateway 必须拒绝 Telethon 不支持的代理协议。

## Design Decisions

- 不改为账号分组多对多；每个账号始终只有一个当前用途分组。
- `pool_purpose` 是用途真相源，`account_identity` 是同步投影与执行期快速守卫。
- 分组代理绑定是账号组资产，任务只引用，不负责创建后永久占用或停止时自动解绑。
- Gateway 执行完整搜索与受限点击，业务层在调用前给出最大点击预算，Gateway 返回逐点击 outcome。
- 不以写统计代表外部成功，不使用 mock success 或直连 fallback。

## Implementation / QA Findings

- 子代理 review 发现 reservation 状态机两个 P1：pre-factual skip 未释放 reservation，retry 可能重排 consumed/unknown/released reservation；已修复并补测试。
- 旧测试仍引用 `_rank_deboost_pool_accounts`；保留兼容 wrapper 指向 `account_config group` 语义，避免 collection 入口断裂。
- 全量 no-PostgreSQL 初始阻断不是 rank 定向套件失败，而是严格账号用途 / runtime proxy readiness 影响旧 fixture：部分任务没有可用 operational 账号，部分代理机场节点没有 executable runtime proxy；已通过 legacy unpooled normal 规则、契约测试更新和可执行 proxy fixture 修复，最新全量 no-PostgreSQL 通过。
- Alembic offline SQL 阻断来自旧迁移 `0002_developer_app_pool.py` 对 offline `MockConnection` 调用 `sa.inspect`，不能作为本迁移已验证证据。
- 当前没有 PostgreSQL rank reservation 并发专项文件；Postgres evidence 不可标记通过。
