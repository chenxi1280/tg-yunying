# 账号列表头像异步加载

## 1. Mini Bug Card

- bug_id: `accounts-avatar-waterfall-20260813`
- level: L1 quick_fix
- symptom: `/accounts` 首屏账号数据可用后，当前分页的头像请求继续形成长瀑布，影响页面可操作感知。
- observed_scope: TG 账号管理列表的账号列；账号详情和资料编辑头像不在本次范围。
- root_cause: 列表行渲染时直接为当前页每个 `Avatar` 设置 `src`，浏览器立即并发或排队请求全部头像。
- follow_up_scope: 账号页改为服务端 20 条分页，可用性汇总只读当前页账号；不修改数据库、Telegram Session、2FA 或头像存储格式。

## 2. 交互状态

有 `avatar_object_key` 的账号按以下状态显示：

```text
未进入可视区：有头像
进入可视区并开始请求：加载中
请求成功：头像图片
请求失败或缺少可访问预览地址：加载失败
```

没有 `avatar_object_key` 的账号继续显示平台展示名首字，不发头像请求。

## 3. 前端设计

- `AccountLazyAvatar` 负责单个列表头像的可见性观察和加载状态，不改变账号业务状态。
- 使用 `IntersectionObserver` 观察头像容器；只有首次进入可视区才设置图片 `src`。
- 加载中的图片保持隐藏，避免未解码图片或破图图标覆盖状态文案。
- 成功和失败均为终态；组件卸载时断开 observer，避免遗留观察任务。
- 分页、搜索或账号列表变化后，新挂载的行独立进入上述状态机。

## 4. 数据流转

```text
GET /api/tg-accounts
  -> Account.avatar_object_key + avatar_preview_url
  -> 账号表格先渲染文字状态
  -> 行头像进入浏览器可视区
  -> GET avatar_preview_url
  -> load: 显示头像 / error: 显示加载失败
```

头像失败不改变账号资料完整度，也不触发自动重试或后端写入。

## 5. 权限与安全

- 沿用当前账号列表权限和媒体 URL 鉴权，不增加新接口或权限点。
- 不在日志、DOM 文案或请求参数中增加手机号、Session、2FA 等敏感信息。

## 6. QA 验收

1. 打开 `/accounts`，账号文字行可先展示，未进入可视区且已有头像的行显示“有头像”。
2. 滚动到该行后显示“加载中”，网络面板此时才出现对应头像请求。
3. 请求成功后显示圆形头像；请求失败显示“加载失败”。
4. 无头像账号显示展示名首字且不请求头像。
5. 翻页和搜索后的新行遵循同一状态机。
6. TypeScript 正式构建和聚焦契约测试通过。

## 7. Release Gate

- release_mode: github_actions
- migration_impact: none
- worker_impact: none
- external_platform_impact: none; 不调用 Telegram
- rollback_plan: 回滚到上一不可变前端 release；无数据迁移和生产写入。
- production_probe: 核对部署 SHA、`/accounts` HTTP 200，并在真实浏览器观察可视区前后请求与三态文案。

## 8. 账号页 20 条服务端分页补充

- 首次进入账号页只调用 `GET /api/tg-accounts?page=1&page_size=20`，不得循环读取全租户账号。
- 翻页、分组和搜索都重新请求服务端；分页总数读取 `X-Total-Count`，每页数量固定 20。
- 搜索保持账号中心现有业务词，包括“登录有问题 / 账号级受限 / 健康分偏低 / 代理异常 / 资料待初始化”。
- `GET /api/tg-accounts/availability/summary?account_ids=...` 只接受当前页最多 20 个账号 ID；页面汇总卡明确标为“当前页”。
- 发布验收必须比较静态发布目录 `index.html` 中的主 JS 与 Nginx 本机入口返回的主 JS；不一致时发布失败。
