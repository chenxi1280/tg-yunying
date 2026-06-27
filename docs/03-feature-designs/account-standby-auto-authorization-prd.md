# 账号备用授权自动补齐 PRD

> 日期口径：2026-06-14（Asia/Shanghai）
> 适用范围：TG账号管理、账号授权资产、账号安全批次、任务中心系统批次投影。

## 1. 背景

线上环境已经维护了备用 TG Developer App，但账号管理当前缺少一个可运营的闭环：管理员无法在账号列表中一键筛出没有备用授权的账号，并自动完成备用 session 登录、授权验证、二步密码校验和结果回写。

现有能力分散在几处：

- 系统设置可维护多个 TG Developer App，用于分担账号登录和授权容量。
- 账号详情“授权资产”已支持单账号新增备用授权、提交验证码/2FA、切换主授权。
- 账号安全批次已有“补齐备用 session”入口和任务中心类型 `account_standby_session_provision`。
- 后端已有自动补齐雏形，会选择开发者应用、代理、读取验证码、使用托管 2FA，但产品验收口径仍不完整。

本 PRD 聚焦把“备用开发者应用”转化为“真实可切换备用 session”。只有完成 Telegram 真实登录并保存加密 session 的 standby 授权，才算账号具备备用能力。

## 2. 目标

1. 账号管理支持一键筛选没有备用 session 的账号。
2. 管理员可批量创建“补齐备用 session”批次，系统自动为 `standby_1` 和 `standby_2` 缺口发起真实 Telegram 登录；当两个槽位都缺失时，同一个账号项必须连续补齐两个备用 session。
3. 自动登录必须覆盖验证码读取、授权验证、二步密码登录、开发者应用分配、代理分配和健康检查；验证码由当前主 session 自动读取和轮询，不要求运营人员手动从主应用复制验证码。
4. 当任一登录流程由运营人员或托管密码提交 Telegram 2FA 后，系统必须先完成真实登录，再立即把 Telegram 2FA 修改为新的平台托管密码并密文保存；后续备用槽位登录必须使用最新托管密码。
5. 成功后写入账号授权资产，账号列表和详情显示健康备用 session 数。
6. 失败时暴露清晰原因，不允许静默跳过、伪造成功或只写泛化错误。
7. 任务中心能看到批次进度、账号级结果、失败原因和重试入口。

## 3. 非目标

- 不把 Developer App 数量等同于备用 session 数量。
- 不绕过 Telegram 官方验证码、2FA、QR 或新登录限制。
- 不在无验证码读取能力、无托管 2FA 或 Telegram 明确限制时伪造自动成功。
- 不自动清理登录设备。设备清理仍走独立“清理登录设备”批次。
- 不把账号安全批次混入普通运营任务创建入口。
- 不长期明文保存 Telegram session、验证码或 2FA 密码。

## 4. 方案选择

### 方案 A：只保留单账号手动补齐

运营人员进入每个账号详情，手动选择开发者应用、代理，发起 standby 登录并输入验证码/2FA。

优点：实现改动小，风险低。

缺点：线上账号数量增加后无法运维，无法满足“一键补齐未备份账号”的目标。

### 方案 B：账号管理批量自动补齐（推荐）

账号列表提供“备用 session 缺口”筛选和“选择缺口账号”按钮。管理员确认后创建 `account_standby_session_provision` 批次，由账号安全 worker 自动分配资源、发起登录、读取验证码、使用托管 2FA、保存 standby session 并健康检查。

优点：复用现有授权资产、账号安全批次和任务中心投影，能把失败原因按账号暴露出来，适合作为一期上线。

缺点：依赖验证码读取能力和托管 2FA 覆盖率；部分账号仍会进入人工处理。

### 方案 C：登录后强制全自动补齐并阻塞账号可用

主授权登录成功后强制补齐一主两备，未补齐前账号不可参与任务。

优点：状态最统一。

缺点：会阻塞存量账号和当前生产任务，不适合迁移期。

结论：一期采用方案 B。主授权仍可继续使用；备用缺口作为恢复风险提示和批量补齐入口，不阻塞现有任务。

## 5. 用户角色和权限

| 角色 | 能力 |
| --- | --- |
| 平台管理员 | 创建批量补齐批次、管理 Developer App、代理、托管 2FA、查看审计 |
| 账号添加专员 | 查看账号缺口、发起补齐、处理验证码/2FA 人工项 |
| 运营主管 | 查看补齐进度、失败原因和恢复能力 |
| 只读观察员 | 查看账号授权摘要，不可发起登录或查看敏感配置 |

权限要求：

| 权限点 | 控制范围 |
| --- | --- |
| `accounts.view` | 查看账号列表和授权摘要 |
| `accounts.authorizations.manage` | 创建备用授权登录、批量补齐、切换主授权 |
| `accounts.security.batch` | 查看和管理账号安全批次 |
| `accounts.security.credential_manage` | 保存、轮换和使用平台托管 2FA |
| `developer_apps.manage` | 维护 TG Developer App 池 |
| `proxies.manage` | 维护账号代理池 |

所有创建批次、读取验证码、使用托管 2FA、保存 session、切换主授权和失败重试都必须写审计。

## 6. 核心概念

| 概念 | 定义 |
| --- | --- |
| Developer App 池 | 多个 `api_id/api_hash`，用于分担登录和授权容量 |
| 主授权 | 当前任务执行默认使用的 `developer_app + proxy + session` |
| 备用授权 | 已完成真实登录、保存加密 session、可健康检查的 standby 授权 |
| 备用缺口 | `standby_1` 或 `standby_2` 缺失、未登录、不可解密、健康检查失败或不可激活 |
| 托管 2FA | 平台加密保存的 Telegram 二步密码，仅在 Telegram 要求第二步校验时使用 |
| 官方验证码读取能力 | 使用当前主 session 读取 Telegram 官方验证码消息的能力 |
| 自动补齐批次 | `account_standby_session_provision` 系统批次 |

## 7. 页面需求

### 7.1 账号列表

账号列表必须支持以下筛选：

- 备用 session 缺口
- `standby_1 session` 缺失
- `standby_2 session` 缺失
- 健康备用 session 不足 2 个
- 备用 session 未登录
- 备用 session 不可解密
- 备用 session 健康检查失败
- 备用 session 不可激活
- 可从备用 session 激活恢复
- 未托管 2FA

批量入口：

| 按钮 | 行为 |
| --- | --- |
| 补齐备用 session | 打开备用 session 补齐抽屉 |
| 只看备用 session 缺口 | 应用缺口筛选 |
| 选择备用 session 缺口账号 | 将当前缺口账号加入批量选择 |

列表授权资产列必须展示：

- 主授权状态。
- 健康备用 session 数，例如 `1/2`。
- 风险提示，例如“未配置备用授权，主 session 失效时需要扫码或验证码恢复”。
- 是否可从备用 session 激活恢复。

### 7.2 备用 session 补齐抽屉

入口独立，不和资料初始化、设置二步密码、清理登录设备混用。

字段：

| 字段 | 要求 |
| --- | --- |
| 账号选择 | 支持当前筛选、跨页选择、区间选择、一键选择缺口账号 |
| 补齐槽位 | 自动补齐缺失槽位、仅 `standby_1`、仅 `standby_2` |
| Developer App 策略 | 默认自动选择健康且容量未满的备用应用，可显示命中结果 |
| 代理策略 | 默认自动选择健康代理，尽量避开当前主授权代理 |
| 2FA 使用 | 使用平台托管 2FA；未托管时预警，执行时如 Telegram 要求 2FA 则进入人工 |
| 操作原因 | 必填 |

预检必须展示：

- 总账号数、可自动补齐数、需人工数、等待数、跳过数。
- 每个账号的目标槽位、Developer App、代理、验证码读取状态、托管 2FA 状态。
- 阻塞原因和建议动作。

确认页必须展示：

- 本次将创建的批次类型：`account_standby_session_provision`。
- 会补齐哪些槽位。
- 会使用多少个 Developer App 和代理。
- 会读取验证码的账号数。
- 会使用托管 2FA 的账号数。
- 需人工处理和跳过账号清单。

### 7.3 账号详情授权资产 Tab

授权资产 Tab 必须展示：

- `primary session`
- `standby_1 session`
- `standby_2 session`
- 每个槽位的 Developer App、代理、session 是否存在、健康状态、最近健康检查、最近补齐批次、失败原因。

操作：

| 操作 | 行为 |
| --- | --- |
| 补齐 | 打开备用 session 补齐抽屉，并锁定当前账号和槽位 |
| 激活恢复 | 将健康 standby 切换为 primary，旧 primary 进入待修复 |
| 刷新授权资产 | 重新读取授权资产摘要 |
| 查看批次 | 跳到最近补齐批次详情 |

## 8. 自动补齐流程

```text
管理员选择备用 session 缺口账号
  -> 预检账号在线状态、主 session、目标槽位
  -> 分配健康 Developer App
  -> 分配健康代理
  -> 创建账号安全批次
  -> worker 按账号串行领取批次项
  -> 发起 Telegram 登录
  -> 使用当前主 session 自动读取 / 轮询官方验证码
  -> 自动提交验证码
  -> 如 Telegram 要求 2FA，读取平台托管 2FA 并提交
  -> 获取 raw session
  -> 加密保存为 TgAccountAuthorization
  -> 执行健康检查
  -> 更新账号授权摘要、批次项和审计
```

同一账号同一槽位不得并发创建两个补齐任务。若已有运行中批次项，后来的批次项进入等待并展示原因。

当 `standby_slot_strategy=auto_missing` 时，单个账号项必须按 `standby_1`、`standby_2` 顺序补齐所有缺失槽位；任一槽位失败时停止后续槽位并展示当前失败原因，已成功的槽位保留授权资产和审计记录。显式选择 `standby_1` 或 `standby_2` 时，只处理指定槽位。

## 9. 状态机

### 9.1 批次状态

| 状态 | 含义 |
| --- | --- |
| `ready` | 已预检但未确认或无可执行账号 |
| `running` | worker 正在执行 |
| `succeeded` | 全部可执行项成功 |
| `partial_success` | 部分成功、部分失败 |
| `failed` | 可执行项全部失败 |
| `manual_required` | 全部账号都需要人工处理 |
| `cancelled` | 管理员取消 |

### 9.2 账号项状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 等待执行 |
| `running` | 正在补齐 |
| `succeeded` | 目标槽位补齐成功且健康检查通过 |
| `waiting` | 受 Telegram 限制或账号互斥锁影响，等待重试 |
| `manual_required` | 需要人工验证码、QR、2FA 或账号恢复 |
| `skipped` | 预检不可执行或已满足一主两备 |
| `failed` | 执行失败 |

### 9.3 备用 session 状态

| 状态 | 含义 |
| --- | --- |
| `not_requested` | 本次没有请求备用补齐 |
| `pending` | 已进入补齐队列 |
| `login_started` | 已发起 Telegram 登录 |
| `code_waiting` | 等待或读取验证码 |
| `two_fa_waiting` | Telegram 要求 2FA |
| `succeeded` | 已保存健康 standby session |
| `manual_required` | 自动链路无法继续 |
| `failed` | 明确失败 |

## 10. 失败原因

失败必须保留账号级明细，不能合并成“执行失败”。

| 失败类型 | 展示文案 | 处理建议 |
| --- | --- | --- |
| `account_not_online` | 主 session 不可用，无法自动读取备用登录验证码 | 先重新登录主授权或从健康 standby 激活 |
| `developer_app_unavailable` | 没有可用 TG Developer App | 在系统设置补充或修复 Developer App |
| `proxy_unavailable` | 没有可用代理 | 修复或补充代理 |
| `verification_code_unreadable` | 验证码不可读取，已记录备用授权登录流水 | 人工查看验证码或改用 QR |
| `two_fa_not_managed` | Telegram 要求 2FA，但账号未托管 2FA | 进入托管 2FA 面板或人工输入 |
| `two_fa_invalid` | 托管 2FA 校验失败 | 轮换托管 2FA 后重试 |
| `two_fa_rotation_failed` | 备用登录已完成，但 2FA 新密码轮换失败 | 检查 Telegram 2FA 修改限制或恢复邮箱确认状态后重试 |
| `telegram_limit` | Telegram 限制本次新登录或授权 | 等待限制解除后重试 |
| `session_encrypt_failed` | session 加密保存失败 | 修复加密配置后重试 |
| `health_check_failed` | 备用 session 登录成功但健康检查失败 | 查看登录尝试和代理状态 |

## 11. 数据和接口

复用现有对象：

- `telegram_developer_apps`
- `tg_accounts`
- `tg_account_authorizations`
- `tg_login_flows`
- `tg_verification_codes`
- `tg_account_security_batches`
- `tg_account_security_batch_items`
- `tg_account_security_snapshots`
- `audit_logs`

接口要求：

| 接口 | 用途 |
| --- | --- |
| `GET /api/tg-accounts` | 返回 `authorization_summary`，支持前端筛选缺口 |
| `GET /api/tg-accounts/{id}/authorizations` | 查看授权资产槽位 |
| `POST /api/tg-accounts/{id}/authorizations/login/start` | 单账号发起备用授权登录 |
| `POST /api/tg-accounts/{id}/authorizations/login/verify` | 单账号提交验证码/2FA |
| `POST /api/tg-accounts/{id}/authorizations/login/qr/check` | 单账号检查 QR 登录 |
| `POST /api/tg-accounts/security-batches/precheck` | 批量补齐预检 |
| `POST /api/tg-accounts/security-batches` | 创建补齐批次 |
| `GET /api/tg-accounts/security-batches/{id}` | 查看批次详情 |
| `POST /api/tg-accounts/security-batches/{id}/retry` | 重试失败项 |
| `GET /api/tasks?type=account_standby_session_provision` | 任务中心查看系统批次投影 |

`authorization_summary` 至少包含：

```json
{
  "primary_status": "active",
  "primary_source": "legacy_account",
  "standby_count": 0,
  "target_standby_count": 2,
  "has_standby": false,
  "is_blocking": false,
  "risk_hint": "未配置备用授权，主 session 失效时需要扫码或验证码恢复"
}
```

## 12. 规则和约束

- 自动补齐必须真实调用 Telegram 登录链路，不能创建空 session 或 mock 成功。
- 2FA 只用于 Telegram 第一阶段授权后的二次校验，不能替代验证码、QR 或 future auth token。
- 健康备用 session 数以可解密、可连接、状态健康的 standby session 为准。
- 主授权可用但缺少备用 session 时只提示恢复风险，不阻塞当前任务。
- 同一账号同一槽位只允许一个运行中补齐项。
- 备用授权登录成功后，不覆盖当前主授权，除非管理员执行“激活恢复”。
- 旧故障授权不得自动删除，必须保留失败原因和审计。
- Developer App 和代理选择应优先避开当前主授权资源；资源不足时允许使用同资源，但必须显示容量风险。

## 13. 验收标准

### 13.1 账号列表

- 能筛出所有健康备用 session 不足 2 个的账号。
- 点击“选择备用 session 缺口账号”后，批量抽屉带入正确账号。
- 只有 Developer App 但没有 standby session 的账号仍显示为备用缺口。
- 主授权在线但无备用的账号不被阻塞，风险提示可见。

### 13.2 预检

- 无健康 Developer App 的账号项进入不可执行，并显示原因。
- 无健康代理的账号项进入不可执行，并显示原因。
- 未托管 2FA 的账号在预检显示警告；执行时若 Telegram 要求 2FA，进入人工处理。
- 已满足一主两备的账号自动跳过，不创建重复 standby。

### 13.3 执行

- 可执行账号能自动发起备用授权登录。
- 验证码可读取时，系统自动提交验证码；验证码第一时间未出现在主 session 时，worker 在验证码有效期内自动轮询，不要求人工输入。
- Telegram 要求 2FA 且平台已托管密码时，系统自动提交 2FA。
- 两个备用槽位都缺失且选择自动补齐时，一次批次执行后应同时新增 `standby_1` 和 `standby_2` 授权资产。
- 成功后 `tg_account_authorizations` 新增对应 `standby_1` 或 `standby_2`，且 `session_ciphertext` 非空。
- 成功后账号列表 `standby_count` 增加。
- 单账号补齐不覆盖当前 primary session。

### 13.4 失败暴露

- 验证码不可读取时，批次项显示 `verification_code_unreadable` 或等价明确原因。
- 2FA 未托管时，不显示成功，必须进入 `manual_required`。
- Developer App 异常、代理异常、Telegram 限制都在批次项展示。
- 任务中心 `account_standby_session_provision` 详情能看到账号级失败原因。

### 13.5 审计

- 创建批次写审计。
- 发起备用登录写审计。
- 使用托管 2FA 写审计。
- 保存 standby session 写审计。
- 激活恢复写审计。
- 重试和取消批次写审计。

## 14. 一期交付范围

一期必须交付：

- 账号列表缺口筛选和批量选择。
- 备用 session 补齐抽屉。
- 批量预检。
- 自动 Developer App 和代理选择。
- 自动验证码读取和提交。
- 托管 2FA 自动使用。
- 授权资产写入和列表摘要刷新。
- 任务中心系统批次投影。
- 失败原因和审计。

一期可保留为人工处理：

- 验证码读取不到。
- 未托管 2FA。
- Telegram 要求 QR。
- Telegram 新登录限制。
- Developer App 或代理资源不足。

## 15. 与现有文档关系

本 PRD 是 `docs/01-product/tg-ops-platform-prd.md` 中“开发者应用池与账号授权资产”和 `docs/03-feature-designs/account-security-hardening-design.md` 中“备用 session 自动补齐与自愈”的专项细化。若两处描述与本文冲突，以本文的一期验收口径为准。
