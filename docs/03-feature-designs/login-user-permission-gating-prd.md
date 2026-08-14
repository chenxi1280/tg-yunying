# 登录用户权限驱动资源加载专项 PRD

## 状态

`current`。本专项约束平台登录账号的首屏资源加载；不改变 Telegram 账号、Session、验证码或授权资产流程。

## 问题与目标

账号添加专员具备账号新增、登录和同步权限，但全局刷新无条件读取系统运行时诊断，触发 `/api/config/runtime` 的 `system.view` 守卫并让页面显示“后端未连接或接口异常：permission denied”。

目标是保持最小权限：账号添加专员能够使用其已授权的账号管理能力；系统诊断仍只对 `system.view` 用户开放。

## 用户与权限合同

| 登录用户 | 可加载资源 | 不可加载资源 | 页面结果 |
| --- | --- | --- | --- |
| 账号添加专员 | `/api/auth/me`、账号池、账号列表、`/api/tg-accounts/creation-capability` 和其授权的账号动作 | `/api/config/runtime`、系统设置资源 | 可进入 `/accounts`，无权限错误提示，新增按钮按最小能力投影启用 |
| `system.view` 用户 | 上述资源及 `/api/config/runtime` | 依各自细粒度权限决定 | 可查看系统运行时诊断 |

`/api/config/runtime` 的后端权限仍为 `system.view`。客户端不得通过增加角色权限、吞掉 403 或降级为假数据来规避该规则。

## 数据流与状态

```text
登录 token
  -> GET /api/auth/me
  -> resolved permissions
  -> has system.view?
       -> yes: GET /api/config/runtime -> runtime config
       -> no: runtime = null
  -> has accounts.create?
       -> yes: GET /api/tg-accounts/creation-capability -> can_create_tg_account
       -> no: account creation capability = null
  -> 当前页面的已授权资源加载
```

- 用户缺少 `system.view` 时不发起运行时配置请求，因此不会生成权限拒绝审计；若客户端直接请求，后端仍返回 403 并审计。
- `accounts.create` 用户的创建能力接口只返回单个布尔值，权限为 `accounts.create`；它与系统运行时诊断分离，不能用于推断系统配置细节。
- `runtime = null` 仅表示当前用户没有系统诊断数据，不表示后端不可达或服务异常。
- 账号新增、登录、同步仍按 `accounts.create`、`accounts.login`、`accounts.sync` 独立校验。

## 边界与风险

- 不修改 `AppUser` 的权限数据、`permission_version`、Token 或数据库 schema。
- 不放宽 `/api/config/runtime` 的后端权限，因为它暴露应用环境、队列、TG 网关、开发者应用和 AI 健康摘要。
- 不触发任何 Telegram 登录、验证码发送或账号状态变更。

## Product Design Complete

| 检查项 | 结论 |
| --- | --- |
| 用户原话 | 修复平台登录账号进入页面后的 `permission denied`，不把它误判为 Telegram 账号登录问题。 |
| 前端状态 | 先读用户权限；系统用户得到完整 `runtime`，账号创建用户得到单字段创建能力，其他用户不请求两者。 |
| 后端/API | 保留系统诊断接口的 `system.view` 守卫；新增 `accounts.create` 守卫的最小只读投影。 |
| 数据与并发 | 只读查询 Telegram 开发者应用健康状态；无写库、无 migration、无 worker 和无 Telegram 调用。 |
| 权限与安全 | 不向账号添加角色授予系统设置权限；直接越权请求仍由后端拒绝和审计。 |
| 失败路径 | 能力为 false 时，系统用户可跳转配置；无系统权限用户获得联系管理员提示，不能跳转至无权页面。 |
| QA/发布 | 覆盖接口守卫、低权限快照、系统用户快照、创建按钮状态和线上审计；发布后验证当前账号不再产生 runtime 403。 |

## 验收

1. 无 `system.view` 的账号添加角色加载应用时，不请求 `/api/config/runtime`，而是仅在具备 `accounts.create` 时请求账号创建能力投影，并继续加载账号页数据。
2. 创建能力投影只包含 `can_create_tg_account`；可用时新增账号按钮可用，不可用时无系统设置权限的用户只看到联系管理员的提示。
3. 具备 `system.view` 的用户仍请求并获取运行时配置。
4. 直接请求 `/api/config/runtime` 的无权限用户仍得到 403 `permission denied`。
5. 前端类型检查、定向权限回归和生产部署后，低权限登录用户不再产生该接口的权限拒绝审计。
