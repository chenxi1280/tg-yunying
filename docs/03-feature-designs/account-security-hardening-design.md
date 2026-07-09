# TG 账号安全加固与资料初始化设计文档

> 账号备用授权自动补齐的一期 PRD 见 `docs/03-feature-designs/account-standby-auto-authorization-prd.md`。当线上已维护备用 TG Developer App 时，账号管理仍必须以真实登录成功的 `standby_1 session` / `standby_2 session` 作为备用授权验收标准，不能把 Developer App 数量当成备用 session 数量。

## 1. 背景与目标

账号登录到平台后，还需要同时解决安全加固和账号资料初始化问题：

```text
账号已登录
  -> 建立平台主授权和备用授权状态
  -> 保留官方锚点设备
  -> 清理陌生或废弃登录设备
  -> 设置平台可信设备显示名
  -> 检查是否已设置 Telegram 二步验证密码
  -> 未设置的账号尽量批量设置二步验证密码
  -> 批量设置头像、昵称 / 姓名、@username
  -> 写入账号安全状态、资料初始化结果、批量任务结果和审计记录
```

本能力属于账号中心和风控中心之间的账号安全治理与账号资料初始化，不属于任务中心的普通运营任务创建入口。但资料初始化、设置二步密码、清理登录设备和备用 session 自动补齐一旦进入后台 worker 执行，执行状态必须能被任务中心读取；所有账号安全批次都必须以系统执行任务形式展示进度、失败原因和账号级结果，资料初始化批次额外展示头像缓存状态。

目标：

- 账号中心能看到每个 TG 账号的登录设备、平台可信状态、二步验证状态和最近加固结果。
- 账号中心能看到每个 TG 账号的主授权、备用授权数量、备用健康状态和官方锚点设备风险。
- 主授权登录成功后，系统自动补齐 `standby_1 session` 和 `standby_2 session`；定时账号状态检查发现任一 session 掉线时，自动用健康备用 session 激活恢复，并重建故障备用 session。
- 管理员可以先点击“清理登录设备”或“设置二步密码”，再在抽屉内选择账号并创建批次。
- 管理员可以先点击“资料初始化”，再在抽屉内选择账号，批量设置 TG 账号头像、昵称 / 姓名、简介和 `@username`。
- 接码专用分组账号只用于接收 Telegram 官方验证码、授权资产诊断和备用 session 补齐 / 自愈；不得改昵称 / TG 姓名 / 简介 / `@username` / 头像，不得初始化账号面具，不得设置或轮换 2FA 密码，不得一键清理其他登录设备，不得参与消息发送、监听、目标准入或任何运营任务。
- 昵称、TG 姓名、简介和 `@username` 默认用一次 AI 请求生成整批资料，支持命名风格提示、预览、去重、可编辑、可重抽；序号递增只作为手工兜底策略。
- 资料初始化的昵称 / TG 姓名必须同时回写平台账号展示名并调用 Telegram profile 更新。平台展示名属于“新托管账号”“托管账号”或导入占位名等可替换名称时，即使未开启“覆盖已有资料”，也要把本次生成名同步到 TG `first_name` / `last_name`。
- 资料初始化批次确认后，账号中心展示批次入口，任务中心展示后台运行状态；运营人员必须能看到等待缓存、执行中、成功、跳过、失败和最近失败原因。
- 验证码登录或扫码登录成功后，系统自动检查当前账号资料；展示名或 TG 姓名仍为英文 / 占位名、缺少 `username` 或缺少头像时，自动创建已确认的资料初始化批次，使用本地中文随机资料和素材中心随机头像池，不在登录接口内直接调用 Telegram 更新资料。
- 选择随机头像包时必须从素材中心头像包自动分配，不要求管理员手填素材 ID 或平台路径；头像更新只允许使用已完成 TG 缓存的素材。
- 头像更新成功后必须回写账号头像对象和预览 URL，账号列表和账号详情必须回显新头像。
- 平台可信设备显示名应统一，方便在 Telegram 登录设备列表里识别平台 Session。
- 批量执行前必须预检，明确哪些账号可执行、哪些需要等待、哪些需要人工处理。
- 执行过程必须可暂停、可重试、可追踪、可审计。
- 不长期明文保存 Telegram 二步验证密码，不在前端回显敏感信息。

非目标：

- 不管理用户自己的手机、电脑、浏览器等真实硬件资产。
- 不把“设备清理”做成任务中心普通运营任务；它必须作为账号安全系统任务投影到任务中心，只提供查看、刷新和跳转账号批次详情。
- 不绕过 Telegram 官方安全限制，例如新登录 Session 24 小时内不能退出其他 Session 的限制。
- 不删除当前平台 Session，否则平台会失去该账号控制能力。
- 不要求迁移期账号必须立刻补齐备用 Session。没有备用 Session 时，账号管理只提示风险，不阻塞当前主 Session 的既有能力。
- 不把二步验证密码当成免验证码登录入口。2FA 只用于 Telegram 完成第一步授权后要求二次校验的场景。
- 不把接码专用账号当成运营账号。接码专用账号登录成功后不自动创建资料初始化批次，不初始化账号面具，也不进入资料初始化、2FA、设备清理、消息发送或任务执行候选。

## 2. 概念定义

| 概念 | 说明 |
| --- | --- |
| 平台可信设备 | 当前平台使用 `session_ciphertext + developer_app_id + proxy_id` 连接 Telegram 的授权 Session。产品上叫“平台可信设备”，技术上是当前 MTProto 授权会话。 |
| 主授权 | 当前任务执行和同步默认使用的账号授权资产，由 `developer_app_id + proxy_id + session_ciphertext` 组成。 |
| 备用授权 | 已提前真实登录成功、可在主授权异常时切换使用的账号授权资产。只配置开发者应用但没有可用 session，不算备用授权。 |
| 官方锚点设备 | 保留在 Telegram 官方手机端或桌面端上的已登录设备，用于平台 session 全部失效时扫码恢复。 |
| 平台设备显示名 | Telegram 授权列表里展示的平台客户端信息，例如 device_model、system_version、app_version。它帮助识别平台 Session，不等同于 TG 账号昵称。 |
| 外部设备 | Telegram 返回的其他授权会话，例如手机端、桌面端、网页版、未知 API 客户端等。 |
| 设备清理 | 退出陌生设备、历史废弃平台授权或管理员明确选择的无用设备；必须保留 primary、standby_1、standby_2 三个平台 session 对应设备和至少 1 个官方锚点设备。 |
| 二步验证密码 | Telegram 账号的 2FA password，不是平台后台用户登录密码。 |
| 平台托管 2FA 密码 | 平台加密保存并用于自动补齐备用 session 的二步验证密码；可配置统一策略，查看、导出、使用都必须审计。 |
| 账号资料初始化 | 批量设置 TG 账号头像、first_name、last_name、bio 和 `@username`。 |
| 接码专用账号 | `account_identity=code_receiver` 或位于系统固定接码专用分组的账号。它只用于接收官方验证码、授权资产诊断和备用 session 补齐 / 自愈，禁止资料、2FA、设备清理和运营任务动作。 |
| AI 随机命名 | 调用平台 AI 能力，按运营画像、语言风格、性别倾向、地区倾向、禁用词和命名风格提示生成随机昵称、姓名、简介和 username 候选。 |
| 命名规则 | 批量生成昵称、姓名或用户名的规则。默认使用 AI 随机命名；基础名 + 序号只作为兜底手工模式。 |
| 安全处理批次 | 从“设置二步密码”“清理登录设备”或备用 session 自动补齐 / 自愈入口创建的处理批次。 |
| 安全处理项 | 设置二步密码、清理登录设备或备用 session 补齐 / 自愈批次里针对单个账号的一条处理记录。 |

## 3. Telegram 能力边界

后续实现需要基于 Telegram MTProto / Telethon 能力做适配：

- 读取登录设备：`account.getAuthorizations`，用于获取当前账号已授权 Session 列表。
- 退出指定设备：`account.resetAuthorization(hash)`，按 Session hash 退出单个外部授权。
- 设置二步验证密码：`account.updatePasswordSettings`，需要按 SRP 流程生成密码校验和新密码设置。
- 设置账号姓名 / 简介 / 头像：更新 Telegram profile，例如 first_name、last_name、about、profile photo。
- 设置 `@username`：更新 Telegram username，必须先做格式和冲突预检，最终以 Telegram 返回结果为准。

关键限制：

- Telegram 可能禁止新登录不到 24 小时的 Session 退出其他设备，错误通常表现为 `FRESH_RESET_AUTHORISATION_FORBIDDEN`。
- 不能退出当前平台 Session；如果退出当前 Session，该账号在平台内会变成需重新登录。
- 不能把所有非平台设备都退出。若平台主备 Session 全部失效且没有官方锚点设备，账号只能走短信、邮箱、Fragment 或官方恢复路径。
- 多个 TG Developer App 只能分散登录容量和授权风险，不能替代已登录 Session。备用授权必须提前登录成功并定期健康检查。
- 二步验证密码设置可能需要恢复邮箱确认；邮箱未确认时必须进入“待邮箱验证码确认”状态。
- 已有二步验证密码的账号不能直接覆盖；需要提供旧密码或进入人工恢复流程。
- `@username` 可能已被占用、格式不合法或触发 Telegram 限制；批量设置时必须支持自动跳号、重试候选名和失败留痕。
- 头像上传、用户名修改、姓名修改都可能触发 Telegram 频控；需要有界并发和失败重试，不应对同一账号连续快速修改。
- 清理设备、设置 2FA 和批量资料初始化都属于敏感动作，必须有权限、二次确认和审计。

参考：

- Telegram `account.getAuthorizations`: https://core.telegram.org/method/account.getAuthorizations
- Telegram `account.resetAuthorization`: https://core.telegram.org/method/account.resetAuthorization
- Telegram 2FA / SRP: https://core.telegram.org/api/srp

## 4. 模块归属

```text
账号中心
  展示账号安全事实、主备授权、官方锚点设备、发起单账号/批量设置二步密码、清理登录设备、资料初始化、同步安全状态，查看批次和账号结果

风控中心
  汇总外部设备未清理、2FA 未设置、资料不完整、设备异常变化、主备授权缺失等风险；主授权可用但缺少备用授权时只提示恢复风险，主备授权均不可用时才阻塞或降级账号参与任务

任务中心
  读取账号安全后台批次的系统任务投影，展示批次进度、头像缓存进度、账号级结果和失败事实；不作为发起账号维护动作的主入口

审计中心
  记录预检、确认、执行、跳过、失败、重试、敏感配置变更

执行中心
  按批次消费账号安全处理项和资料初始化项，执行 Telegram API 调用并回写结果
```

账号中心负责“怎么处理账号安全事实”，风控中心负责“这些安全事实会不会影响账号参与运营任务”。

## 5. 页面设计

### 5.1 账号列表

账号列表增加轻量字段，不把表格做成复杂安全控制台：

| 字段 | 展示 |
| --- | --- |
| 平台可信设备 | 已确认、待确认、无法确认 |
| 授权资产 | 主授权可用、备用 0/1/2 个、备用异常、可切换状态 |
| 官方锚点设备 | 已保留、未识别、缺失风险 |
| 外部设备 | 无外部设备、存在 N 个外部设备、读取失败 |
| 二步验证 | 已设置、未设置、待邮箱确认、未知、设置失败 |
| 资料完整度 | 头像、昵称、用户名是否已设置 |
| 安全处理 / 资料初始化 | 最近成功时间、最近失败原因 |

批量操作入口：

```text
选择账号
  -> 资料初始化
      -> 批量设置头像 / 昵称 / 简介 / @username
  -> 设置二步密码
      -> 对未设置 2FA 的账号尽量设置
  -> 清理登录设备
      -> 清理外部设备
```

### 5.2 账号详情

账号详情增加“账号安全”Tab：

- 平台可信设备：平台 Session、开发者应用、代理、最近验证时间。
- 登录设备列表：设备名称、应用、平台、IP/地区、创建时间、活跃时间、是否平台可信。
- 二步验证状态：已设置 / 未设置 / 待邮箱确认 / 未知。
- 账号资料：头像、first_name、last_name、bio、`@username`、最近同步时间。
- 最近加固记录：设备清理结果、2FA 设置结果、资料初始化结果、失败原因、trace_id。
- 操作按钮：同步安全状态、清理外部设备、设置二步验证、设置资料、查看批次记录。

### 5.3 批量动作抽屉

批量抽屉按动作拆开，不提供“选择动作”步骤：

```text
点击资料初始化 / 设置二步密码 / 清理登录设备
  ↓
选择账号
  ↓
配置动作或预检
  ↓
二次确认弹窗
  ↓
创建批次
```

动作入口：

| 动作 | 说明 |
| --- | --- |
| 资料初始化 | 只处理头像、昵称 / 姓名、简介和 `@username` |
| 设置二步密码 | 为未设置 2FA 的账号设置平台托管 2FA；已设置且平台掌握旧密码时可替换为平台托管 2FA；旧密码未知时进入人工处理 |
| 清理登录设备 | 只退出非平台授权会话，保留 primary、standby_1、standby_2 和官方锚点设备 |
| 同步安全状态 | 只读取安全事实，不创建批次 |

预检表字段：

| 字段 | 说明 |
| --- | --- |
| 账号 | display_name、username、手机号 |
| Session 状态 | 在线、需重新登录、Session 失效 |
| 平台可信设备 | 已识别、无法识别、需要刷新 |
| 外部设备数量 | 可清理数量 |
| 二步验证状态 | 已设置、未设置、待确认、未知 |
| 资料状态 | 头像、昵称、用户名是否缺失或需要覆盖 |
| 命名预览 | 本次将设置的头像、昵称、姓名、简介和 `@username`，支持单行编辑和批量重抽 |
| 是否可执行 | 可执行、跳过、需等待、需人工处理 |
| 原因 | 例如新 Session 未满 24 小时、需要旧密码、邮箱验证码待确认、用户名被占用、头像文件不可用 |

确认页必须显示：

- 本次会处理多少个账号。
- 会退出多少个外部设备。
- 会给多少个账号设置 Telegram 二步验证密码。
- 会修改多少个账号头像、昵称、姓名和 `@username`。
- 哪些账号会跳过以及跳过原因。
- 操作者通过二次确认弹窗确认执行；弹窗内填写操作原因，不再输入固定确认文案。

## 6. 批量流程

### 6.1 设备清理流程

```text
管理员点击 清理登录设备
  ↓
在抽屉中选择账号：账号组 / 筛选 / 搜索 / 跨页勾选 / 区间选择
  ↓
读取账号 Session、开发者应用和代理
  ↓
若 standby_1 / standby_2 session 未就绪，先自动补齐备用 session
  ↓
调用 getAuthorizations
  ↓
识别平台主授权、备用授权和官方锚点设备
  ↓
计算可清理设备列表
  ↓
预检是否可退出外部设备
  ↓
逐个 resetAuthorization(hash)
  ↓
重新读取设备列表
  ↓
确认主备授权和官方锚点设备仍保留，记录剩余外部设备
  ↓
回写账号安全快照和审计
```

识别平台可信 Session 与锚点设备的原则：

- 优先识别当前连接对应的授权会话。
- 备用授权应通过授权资产表或授权快照中的 `developer_app_id + proxy_id + session` 关系识别；无法确认的备用不得被自动清理。
- 清理设备前必须确认 primary session、standby_1 session、standby_2 session 的 Telegram 授权设备 hash；无法确认任一平台 session 时，不允许执行“一键清理外部设备”，只能进入等待补齐或人工确认。
- 官方锚点设备优先识别 Telegram 官方手机端或桌面端，并允许管理员在设备列表中标记“保留为锚点”。
- 结合 `api_id`、应用名、平台标识、创建时间、活跃时间和当前 Session 特征判断。
- 不能只依赖设备名称；设备名称可以被伪造或变化。
- 如果无法稳定识别平台可信 Session、备用授权或官方锚点设备，不允许执行“退出其他设备”类批量清理，只能进入人工确认。

失败处理：

| 失败 | 处理 |
| --- | --- |
| Session 失效 | 标记需重新登录，跳过设备清理 |
| 新 Session 未满 24 小时 | 标记“需等待”，建议自动延后到可执行时间 |
| 外部设备 hash 无效 | 重新读取设备列表后重试一次 |
| 无法识别平台主备授权 | 不执行自动设备删除，进入人工确认 |
| 无法识别官方锚点设备 | 允许提示风险；不阻塞当前账号使用，但禁止“一键清空非平台设备” |
| 无备用授权 | 账号管理提示“未配置备用授权”；不阻塞现有主 Session 继续使用 |
| 备用 session 未就绪 | 先进入备用 session 自动补齐；补齐失败时跳过设备清理并展示原因 |
| 清理后仍有外部设备 | 标记部分成功，保留剩余设备明细 |

### 6.1.1 备用 session 自动补齐与自愈

备用 session 自动补齐由账号安全 worker 执行，触发来源包括：

- 主授权首次登录成功。
- 定时账号状态检查或同步安全状态发现备用 session 缺失、失效、不可解密或健康检查失败。
- 清理登录设备批次执行前发现 standby_1 / standby_2 未就绪。
- 管理员在账号详情“授权资产”Tab 手动点击补齐备用 session。

定时账号状态检查默认每 1 小时扫描一次，可通过后台配置调整。扫描只创建缺口事实和必要的补齐 / 自愈批次，不在同一账号同一授权槽位上并发创建重复批次；同一槽位补齐失败后按 `next_retry_at` 等待，不用静默重试掩盖失败。清理登录设备批次的前置补齐如果失败，当前账号项必须跳过清理并展示失败原因，不能继续执行设备清理。

流程：

```text
选择账号授权槽位：standby_1 / standby_2
  ↓
分配开发者应用和代理
  ↓
使用账号官方验证码读取能力获取登录验证码
  ↓
如 Telegram 要求 2FA，读取平台加密托管 2FA 密码
  ↓
完成真实 Telegram 登录并保存 session_ciphertext
  ↓
如本次使用了 2FA，仅记录当前已验证密码；不自动修改 Telegram 2FA
  ↓
立即健康检查，成功后计入健康备用 session
```

自动补齐不得复用历史 QR 本身。首次 QR 登录记录只作为登录流水、审计和官方锚点设备识别依据。验证码不可读取、2FA 未托管、Telegram 限制、开发者应用异常或代理异常时，必须写入失败原因，并进入备用 session 缺口筛选。

自愈场景中，如果 primary session 掉线但 standby_1 或 standby_2 健康，worker 先把账号当前可用授权切到健康备用 session，再把掉线槽位标记为待补齐并创建补齐项；如果只有 primary 健康而 standby 不完整，worker 只补齐 standby，不切换 primary。

### 6.2 二步验证设置流程

```text
管理员点击 设置二步密码
  ↓
在抽屉中选择账号：账号组 / 筛选 / 搜索 / 跨页勾选 / 区间选择
  ↓
预检账号是否在线
  ↓
读取当前 2FA 状态
  ↓
未设置：读取系统设置中的固定托管 2FA 密码
  ↓
调用 updatePasswordSettings
  ↓
如需要邮箱确认，进入待确认状态
  ↓
确认成功后写入 2FA 状态和审计
```

密码策略：

| 策略 | 说明 | 默认 |
| --- | --- | --- |
| 系统设置固定托管密码 | 多个账号使用同一固定托管 2FA 密码，便于后续手动批量统一设置；必须加密保存、只允许首次设置、限制查看和导出 | 默认 |

安全要求：

- 前端不回显完整二步验证密码。
- 后端只允许加密保存，或只在批次执行期间以 TTL 密文保存，执行完成后清理临时明文材料。
- 如果平台需要长期托管 2FA 密码，必须使用独立的 `account_security_credentials` 表，字段加密，并提供“最后使用时间”“最后查看人”“一次性导出记录”。
- 平台统一托管密码必须由超级管理员或具备 `accounts.security.credential_manage` 的角色配置；保存、轮换、查看、导出和被自动登录使用都必须写审计。
- 密码 hint 不能包含完整密码。
- 恢复邮箱可选；如果配置恢复邮箱，必须处理邮箱验证码确认流程。

已有 2FA 的账号处理：

| 状态 | 默认处理 |
| --- | --- |
| 已设置且平台不知道旧密码 | 跳过，标记“已设置，需旧密码才可修改” |
| 已设置且平台托管旧密码 | 可在二次确认后替换为平台统一托管密码 |
| 未设置 | 按平台托管策略设置 |
| 状态未知 | 先刷新状态，不直接设置 |
| 待邮箱确认 | 不重复创建，继续确认流程 |

### 6.3 账号资料批量初始化流程

```text
管理员点击 资料初始化
  ↓
在抽屉中选择账号：账号组 / 筛选 / 搜索 / 跨页勾选 / 区间选择
  ↓
选择资料动作：头像 / 昵称姓名 / 简介 / @username
  ↓
配置 AI 随机生成策略、头像策略或导入映射表
  ↓
一次 AI 调用生成整批随机昵称、简介和 username 候选
  ↓
预览每个账号将要设置的资料，支持重抽和手工改
  ↓
检查格式、重复、占用和覆盖策略
  ↓
检查头像素材 TG 缓存状态；未 ready 的头像进入等待缓存，不参与本轮头像更新
  ↓
确认执行
  ↓
创建资料初始化批次，并向任务中心提供系统任务投影
  ↓
逐账号调用 Telegram profile / username / photo 更新，其中昵称 / TG 姓名同时更新平台展示名和 TG `first_name` / `last_name`
  ↓
同步远端资料并回写账号详情
  ↓
记录成功、跳过、失败和审计
```

#### 6.3.1 昵称和姓名规则

支持以下规则：

| 规则 | 示例 | 说明 |
| --- | --- | --- |
| AI 随机生成 | 锅巴洋芋、蕉太狼、早睡失败 | 默认方案，生成更像真实 TG 用户的生活化昵称，减少批量账号资料过度规律 |
| AI 按画像生成 | 成熟男名、年轻女名、东南亚风格名 | 根据运营场景生成不同风格 |
| 导入名单 | 康熙、雍正、乾隆 | 从表格或文本粘贴逐行分配 |
| 随机模板 | 名字库 + 后缀 | 无 AI 或 AI 不可用时兜底 |
| 基础名 + 序号 | 康熙1、康熙2、康熙3 | 手工兜底，不作为默认推荐 |
| 基础名 + 补零序号 | 康熙001、康熙002 | 手工兜底，适合测试或内部标识 |
| 保留已有值 | 已有昵称不覆盖 | 只补齐空值 |

字段映射：

| 平台字段 | Telegram 字段 | 说明 |
| --- | --- | --- |
| display_name | 平台内展示名 | 可跟 Telegram first_name 一致，也可作为平台备注 |
| tg_first_name | first_name | Telegram 必填展示名优先字段 |
| tg_last_name | last_name | 可选 |
| tg_bio | about / bio | 可选简介 |

默认策略：

- 默认先调用 AI 生成随机资料，并在前端展示完整预览。
- 只覆盖本批次勾选的字段。
- 默认不覆盖已有非空资料，除非管理员选择“覆盖已有资料”。
- 生成前做本批次去重，避免多个账号拿到同一个昵称或用户名。
- AI 超时、mock 或无健康供应商时，预检可使用本地随机中文昵称兜底并展示警告；不静默改用规律序号批量灌入。

AI 生成输入：

| 参数 | 说明 |
| --- | --- |
| account_count | 需要生成的账号数量 |
| language_style | 中文、英文、混合、东南亚风格等 |
| persona_style | 自然用户、行业用户、客服、社区成员等 |
| gender_bias | 不限制、偏男性、偏女性、中性 |
| age_style | 年轻、成熟、不限制 |
| username_prefix_hint | username 可用前缀提示，可为空 |
| forbidden_words | 禁用词，避免敏感、重复或品牌词 |
| custom_prompt | 命名风格提示，例如“像真实 TG 普通用户昵称，不要正式姓名或营销号名称” |
| uniqueness_seed | 批次随机种子，保证同一批次可追溯 |

AI 生成输出：

```json
{
  "items": [
    {
      "display_name": "锅巴洋芋",
      "first_name": "锅巴洋芋",
      "last_name": "",
      "bio": "看到有意思的会回两句",
      "username_candidates": ["guoba_yangyu", "potato_crisp", "yangyu_daily"]
    }
  ]
}
```

生成后必须做二次校验：

- 本批次姓名、简介、username 候选不能明显重复。
- 不能出现禁用词、敏感词、平台保留词。
- username 候选必须符合 Telegram 格式。
- 如果 AI 输出不合格，后端自动重试一次；仍不合格则标记该账号“需人工编辑”。

#### 6.3.2 `@username` 规则

用户名需要单独处理，不能简单用中文昵称直接生成。

支持以下策略：

| 策略 | 示例 | 说明 |
| --- | --- | --- |
| AI 生成候选 | linzhixia_28、daily_chen | 默认推荐 |
| 拼音前缀 + 序号 | kangxi01 | 由管理员手工输入拼音前缀 |
| 导入用户名 | 每行一个 username | 适合已有号段 |
| 自动重抽 | username 被占用则调用 AI 或模板生成下一个候选 | 默认开启 |
| 自动跳号 | kangxi001 被占用则尝试 kangxi002 | 仅在手工序号模式下使用 |

预检规则：

- 先校验本批次内是否重复。
- 先校验格式是否符合 Telegram username 要求，最终以 Telegram 返回为准。
- 可以按候选数量预留备用名，例如每个账号最多尝试 3 个候选。
- 已有 `@username` 的账号默认不覆盖；如果勾选覆盖，必须二次确认。
- AI 候选耗尽后可以单账号重抽，不能把失败账号静默改成相同前缀递增。

失败处理：

| 失败 | 处理 |
| --- | --- |
| 用户名被占用 | 自动尝试下一个候选；候选耗尽后重抽或标记需人工编辑 |
| 格式不合法 | 预检阶段直接跳过 |
| 触发频控 | 记录 `next_retry_at`，延后重试 |
| 账号受限 | 标记需风控处理，不继续修改资料 |

#### 6.3.3 头像规则

支持以下策略：

| 策略 | 说明 |
| --- | --- |
| 所有账号使用同一头像 | 操作简单，但账号相似度高，不推荐大批量使用 |
| 从头像素材池随机分配 | 推荐，每个账号尽量不同 |
| 按账号顺序分配头像 | 上传多张头像，按选择账号顺序一一分配 |
| 导入映射表 | 指定 account_id / 手机号 / username 对应头像文件 |
| 只补空头像 | 已有头像不覆盖 |

头像要求：

- 上传前校验格式、大小和可读取性。
- 随机头像包和顺序分配都必须引用素材中心头像包；选择“随机头像包”时系统自动从筛选后的头像包分配，不要求管理员手填素材 ID 或平台路径。
- 头像素材必须先由素材缓存 worker 暂存到 TG 缓存频道，并具备 `cache_ready_status=ready`、`tg_cache_peer_id` 和 `tg_cache_message_id` 后才能用于资料初始化头像更新。
- 素材仍处于 `not_cached`、`refreshing`、`flood_wait` 或 `cache_failed` 时，账号批次项不得直接上传原始平台临时文件；应进入等待缓存、跳过头像或失败，并在任务中心详情展示缓存状态、等待数量、已 ready 数和失败原因。
- 如果素材来源是平台临时上传文件，素材缓存成功后本地临时文件可以被清理；资料初始化必须通过缓存账号从 TG 缓存消息下载头像并保存为账号头像对象，再调用目标账号更新头像。
- 执行后必须拉取远端 profile 或读取更新结果，确认头像已设置。
- 头像更新成功后必须保存新的 `avatar_object_key` 和 `avatar_preview_url` 所需信息，账号列表、账号详情、资料初始化结果和后续资料同步必须能回显新头像。
- 不建议大量账号使用同一头像；风控中心可把“头像重复度过高”作为资料风险提示。

### 6.4 平台可信设备显示名设置流程

平台可信设备显示名用于让管理员在 Telegram 登录设备列表里识别平台 Session。它不等同于账号昵称。

```text
创建或刷新平台 Telegram 客户端
  ↓
使用统一 device_model / system_version / app_version
  ↓
重新读取授权设备列表
  ↓
标记当前平台 Session 为可信设备
```

建议命名：

```text
TG运营平台-主控
TG运营平台-账号池A
TG运营平台-Worker01
```

规则：

- 已登录 Session 的设备显示名不一定能无损修改；必要时需要重新登录或重新创建平台 Session 才能体现新设备名。
- 如果 Telegram 不允许直接修改当前授权会话显示名，系统应记录“下次登录生效”，不能误报成功。
- 设备显示名不承载业务身份，不写账号昵称、手机号或敏感信息。

## 7. 前端界面完整设计

### 7.1 入口与信息架构

前端不新增独立一级导航，仍然放在“TG 账号管理”里。这里的边界是账号资产和账号维护：资料初始化、设置二步密码、清理登录设备、备用 session 补齐 / 自愈、同步安全状态都属于账号维护；消息发送、联系人发送、运营方案和风控处置不放在账号管理里。

```text
TG账号管理
  ├─ 账号状态摘要
  │   ├─ 在线 / 登录有问题（没有登录上平台）
  │   ├─ 受限 / 疑似封禁 / 已封禁
  │   ├─ 同步过期
  │   ├─ 资料待初始化
  │   ├─ 需重新资料初始化
  │   ├─ 未做过登录设备清理
  │   └─ 备用 session 缺口 / 可激活恢复
  ├─ 账号列表
  │   ├─ 资料初始化
  │   ├─ 设置二步密码
  │   ├─ 清理登录设备
  │   ├─ 补齐备用 session
  │   └─ 同步安全状态
  ├─ 账号详情
  │   ├─ 基础信息
  │   ├─ 登录 / 验证
  │   ├─ 授权资产
  │   ├─ 同步资产
  │   ├─ 账号安全
  │   ├─ 托管 2FA
  │   ├─ 资料初始化记录
  │   └─ 操作记录
  └─ 批次中心
      ├─ 加固批次列表
      ├─ 批次详情
      └─ 失败重试
```

账号详情中的联系人、群和频道只展示同步资产，不提供发送入口；联系人发送、人员发送、素材选择和发送记录统一进入“消息发送”页。账号受限、疑似封禁、代理异常等可以在账号管理里作为状态和筛选项展示，但策略处置、限制解除和处置队列必须跳转“风控中心”。

建议前端拆分文件：

```text
frontend/src/app/views/AccountsView.tsx
  保留账号列表和批量入口

frontend/src/app/views/AccountSecurityDrawer.tsx
  单账号安全详情、设备列表、2FA 状态、资料状态

frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx
  primary / standby_1 / standby_2 授权槽位、健康检查、补齐、切换和自愈记录

frontend/src/app/views/AccountSecurityBatchDrawer.tsx
  批量动作选择、预检、确认、执行结果，支持资料初始化、设置 2FA、清理登录设备和备用 session 补齐

frontend/src/app/views/AccountProfileInitPanel.tsx
  AI 随机命名、头像策略、username 候选、资料预览

frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx
  平台托管 2FA 策略配置、轮换、查看 / 导出审计入口

frontend/src/app/views/AccountSecurityBatchDetailModal.tsx
  批次详情、逐账号结果、失败重试
```

### 7.2 账号列表与批量入口

账号列表必须把批量动作入口做得明显，不能要求运营人员先发现表格勾选列再启用按钮。顶部固定展示按动作拆开的按钮：

```text
[资料初始化] [设置二步密码] [清理登录设备] [补齐备用 session] [同步安全状态]
已选择 N 个账号时额外显示：[用已选账号开始] [清空选择]
```

点击任一动作后进入对应抽屉，第一步都是“选择账号”。如果账号列表已有勾选账号，抽屉把这些账号作为初始选择；如果没有勾选，也必须允许直接进入抽屉后再选账号。主入口不能因为表格未勾选而禁用。

账号列表首屏只展示账号资产和维护状态，不展示联系人发送、人员发送、消息编辑、素材选择或风控策略编辑。

表格新增列：

| 列 | 内容 |
| --- | --- |
| 登录状态 | 在线、待登录、等待验证码、等待扫码、等待 2FA、需重新登录、Session 失效 |
| 账号状态 | 正常、受限、疑似封禁、已封禁、异常、禁用 |
| 同步状态 | 资料、联系人、群频道最近同步结果 |
| 安全状态 | 平台可信设备、外部设备数量、2FA 状态 |
| 授权资产 | primary session、standby_1 session、standby_2 session、健康备用 session 数、备用 session 缺口、可激活恢复状态 |
| 资料状态 | 头像、昵称、简介、username 是否完整；区分资料完整、资料待初始化和需重新资料初始化 |
| 登录设备清理 | 从未清理、外部设备未清理、最近清理成功、最近清理失败、等待新 session 限制 |
| 最近批次 | 最近一次资料初始化 / 2FA / 设备清理 / 备用 session 补齐结果、失败原因、跳过原因和是否需重抽 |

筛选项：

- 登录有问题
- 没有登录上平台
- 主授权不可用
- 待登录 / 等待验证码 / 等待扫码 / 等待2FA / 需重新登录 / 异常 / Session 失效
- 存在外部设备
- 未做过登录设备清理
- 外部设备未清理
- 最近设备清理失败
- 未设置 2FA
- 备用 session 缺失
- 备用授权不足 2 个
- 备用授权异常
- standby_1 session 缺失
- standby_2 session 缺失
- 备用 session 未登录
- 备用 session 不可解密
- 备用 session 健康检查失败
- 备用 session 不可激活
- 可从备用 session 激活恢复
- 资料不完整
- 资料待初始化
- 需重新资料初始化
- 无头像
- 无昵称
- 无简介
- 无 username
- username 冲突
- 头像缓存失败
- 最近批量失败
- 最近批量跳过
- 预览需重抽
- 新登录未满 24 小时

“登录有问题”是账号中心首屏快捷搜索入口，不属于风控处置队列。它只筛出没有登录上平台或主授权不可用的账号，命中范围包括待登录、等待验证码、等待扫码、等待2FA、需重新登录、异常、Session 失效、最近登录流水存在失败类型 / 失败详情的账号，以及 `primary_status != active` 的主授权不可用账号。列表必须直接展示最近登录流水的失败原因，并支持按“登录失败”“验证码没收到”“登录验证码没收到”“session 完全失效”等运营口径搜索。受限 / 疑似封禁 / 已封禁、健康分偏低、代理异常和备用 session 缺口保持独立筛选，不能静默混入“登录有问题”。

### 7.3 批量动作账号选择

四类批量动作共用账号选择器：

```text
选择账号
  ├─ 账号组
  ├─ 搜索手机号 / 昵称 / username / 账号 ID / 登录有问题
  ├─ 状态、健康分、在线状态、2FA、头像、资料完整度、需重新资料初始化、安全状态、备用 session 缺口、可激活恢复状态、最近批次筛选
  ├─ 当前页勾选 / 跨页累计选择
  ├─ 选择当前筛选前 100 个 / 追加当前筛选全部
  ├─ 只看资料待初始化 / 选择资料待初始化
  ├─ 只看需重新资料初始化 / 选择需重新资料初始化
  ├─ 只看 standby_1 缺失 / 只看 standby_2 缺失 / 只看备用 session 未登录 / 只看健康备用 session 不足 2 个 / 只看可激活恢复
  ├─ 区间选择，例如 Shift 点击或等效的“选择这一段”
  └─ 已选账号列表，可继续移除
```

选择器要求：

- 支持先选择一个账号组，再慢慢筛选和挑选账号。
- 分页不能丢失已选账号；翻页后已选数量持续可见。
- 支持“全组选入后再剔除”，适合批量处理一个账号组。
- “需重新资料初始化”覆盖最近资料初始化失败、资料初始化被跳过、预览校验失败需重抽、username 候选冲突、头像缓存失败、资料被人工标记为需重做、平台展示名或 TG 姓名仍是占位名等账号；资料初始化抽屉必须能把这类账号一键带入并触发 AI 生成预览或只重抽失败项。
- “standby_1 session 缺失”“standby_2 session 缺失”“备用 session 未登录”“健康备用 session 不足 2 个”覆盖账号授权资产缺口。只配置备用开发者应用或代理但没有真实登录成功的备用 session，不算可用备用授权；这类筛选应跳转或打开备用授权登录处理入口，不进入资料初始化批次。
- “可从备用 session 激活恢复”覆盖 primary session 或任一 standby session 掉线但仍存在健康备用 session 的账号。账号中心应提示可从健康备用 session 恢复，并把故障 session 保留为待修复授权资产。
- 支持从账号列表勾选带入，但带入后仍可增删。
- 禁止在同一个抽屉里混选动作。资料初始化、设置二步密码、清理登录设备、备用 session 补齐必须是四个独立入口。

### 7.3.1 授权资产 Tab

账号详情必须有独立“授权资产”Tab，不把一主两备塞在普通安全状态里。Tab 顶部展示恢复能力摘要：

```text
恢复能力：完整一主两备 / 缺 standby_1 / 缺 standby_2 / 可从备用 session 激活恢复 / 主备均失效
健康备用 session：0 / 1 / 2
官方锚点设备：已识别 / 未识别
```

授权槽位以三张紧凑卡展示：

| 槽位 | 展示字段 | 操作 |
| --- | --- | --- |
| primary | session 健康、开发者应用、代理、最近健康检查、Telegram 授权设备摘要、失败原因 | 健康检查、切换线路、重登、查看审计 |
| standby_1 | 是否已登录、session 可解密、健康检查、开发者应用、代理、最近补齐批次、失败原因 | 补齐、重试补齐、激活恢复、停用、查看审计 |
| standby_2 | 是否已登录、session 可解密、健康检查、开发者应用、代理、最近补齐批次、失败原因 | 补齐、重试补齐、激活恢复、停用、查看审计 |

交互规则：

- “补齐”打开备用 session 补齐抽屉，并默认锁定当前账号和槽位。
- “激活恢复”只在目标槽位健康且当前 primary 或另一个槽位异常时展示；点击后二次确认，确认文案展示将被激活的槽位和将进入待修复的故障槽位。
- 授权槽位不能展示完整 session 明文；敏感查看只展示状态和密文存在性。
- 槽位卡必须展示阻塞原因：验证码不可读取、2FA 未托管、开发者应用异常、代理异常、Telegram 限制、新登录等待或 session 不可解密。
- 列表筛选“可从备用 session 激活恢复”进入详情时，默认打开授权资产 Tab 并高亮可激活槽位。
- 授权资产读取、备用授权登录准备 / 启动 / 校验 / QR 检查和主授权切换必须绑定当前 `account_id`。切换账号或关闭详情后，旧账号异步响应不得覆盖当前账号的授权资产、备用登录表单、loading、错误提示或成功提示。
- 备用登录弹窗必须额外绑定当前弹窗会话序号。关闭弹窗、重新打开弹窗、重新发起备用登录、提交验证码 / 2FA 或检查 QR 登录都会废弃旧会话；旧会话响应不得覆盖当前备用登录资源、`login_flow`、验证码输入、loading、错误提示或成功提示。

### 7.3.2 设置二步密码 / 清理登录设备抽屉

步骤一：选择账号。

步骤二：预检。

- 展示每个账号的可执行状态。
- 对新 Session 未满 24 小时的账号显示预计可重试时间。
- 对已设置 2FA 且平台不知道旧密码的账号默认跳过并显示“需旧密码才可替换”。
- 对已设置 2FA 且平台托管旧密码的账号显示“可替换为平台托管 2FA”，确认页必须二次提示。
- 对 Session 失效账号引导重新登录。
- 清理登录设备预检必须展示将保留的 primary、standby_1、standby_2 和官方锚点设备；任一平台 session 设备 hash 无法确认时，不允许展示“一键清理外部设备”成功态。
- 清理登录设备预检发现 standby_1 / standby_2 未就绪时，当前账号行展示“先补齐备用 session”，并把补齐步骤纳入本批次前置流程。

步骤三：确认执行。

- 汇总会执行、会跳过、需等待、需人工处理的数量。
- 使用二次确认弹窗，不要求输入固定确认文案。
- 弹窗展示动作名称、账号数量、可执行 / 跳过 / 不可执行数量、主要风险提示。
- 清理登录设备确认弹窗必须展示“不会清理 primary / standby_1 / standby_2 / 官方锚点设备”，并列出预计清理外部设备数量。
- 审计需要原因时，在弹窗中填写操作原因。
- 创建批次后进入批次详情。

### 7.3.3 备用 session 补齐 / 自愈抽屉

备用 session 补齐是独立入口，不并入资料初始化或设备清理抽屉。

步骤一：选择账号和槽位。

- 从列表快捷入口进入时，默认筛选 standby_1 缺失、standby_2 缺失或健康备用 session 不足 2 个。
- 从账号详情槽位卡进入时，锁定单账号和单槽位。
- 支持批量选择“缺 standby_1”“缺 standby_2”“健康备用 session 不足 2 个”的账号。

步骤二：补齐配置。

| 配置 | 前端控件 |
| --- | --- |
| 槽位策略 | 自动补齐缺失槽位 / 仅 standby_1 / 仅 standby_2 |
| 开发者应用 | 自动分配 / 手动选择；异常应用不可选 |
| 代理 | 自动分配 / 手动选择；异常代理不可选 |
| 2FA 使用 | 使用平台托管 2FA；未托管账号标记为需人工 |

当槽位策略为“自动补齐缺失槽位”时，同一账号缺少 `standby_1` 和 `standby_2` 的，后台 worker 必须在同一个批次项内连续完成两个备用 session 的真实登录。验证码由当前主 session 自动读取和轮询，不要求运营人员手动从主应用复制验证码；只有验证码在有效期内仍不可读取、未托管 2FA、Telegram 要求 QR 或资源不可用时，才进入人工处理。

当任一登录流程第一次由运营人员输入 Telegram 2FA 旧密码时，系统只用该旧密码完成真实登录并临时托管，不在登录链路中自动修改 Telegram 2FA。固定密码配置上线后，线上已有账号必须由运营在配置完成后手动触发批量设置 / 轮换动作，才能逐步统一为系统固定托管密码；自动补齐 standby session 不承担改密职责。

步骤三：预检和确认。

- 展示验证码读取能力、2FA 托管状态、开发者应用健康、代理健康和新登录限制。
- 确认页展示可自动补齐、需人工 QR、需托管 2FA、需等待和不可执行数量。
- 创建批次后进入批次详情，任务中心显示为 `account_standby_session_provision`。

### 7.3.4 平台托管 2FA 设置面板

平台托管 2FA 不在账号列表行内编辑，必须放在账号安全配置或账号详情安全区的受控面板里。

- 系统设置增加“账号安全配置 / 固定 2FA 密码”入口，展示未配置 / 已配置状态；租户级固定密码只允许首次设置，不提供修改入口，不随任何普通详情接口回显。
- 账号详情托管 2FA 面板继续用于查看 / 复制已托管账号密码；保存动作仅记录当前真实可用密码，不修改 Telegram 2FA；轮换和批量设置动作必须使用系统固定密码口径，不得重新生成随机密码。
- 面板提供“查看托管密码”受控动作：具备 `accounts.security.credential_manage` 的人员可以直接查看，后端通过独立 reveal 接口解密并返回当前托管密码，前端只在本面板短时展示并提供复制按钮。
- 查看、复制、导出、轮换和自动登录使用都必须展示权限要求和审计说明，并写入账号、操作者和时间；查看动作不要求填写或记录原因。
- 接码专用账号的托管 2FA 面板必须进入只读查看形态：保留“查看托管密码 / 复制托管密码”，隐藏保存和轮换输入；后端保存与轮换接口也必须二次拒绝，返回“接码专用账号禁止修改二步验证密码”。
- 轮换前展示影响范围：会影响多少账号、多少账号旧密码未知、多少账号需人工确认。
- 未配置托管 2FA 时，备用 session 自动补齐抽屉必须显示阻塞原因，不允许静默跳过后仍显示补齐成功。
- 保存和轮换托管 2FA 必须绑定当前 `account_id`。切换账号或关闭详情后，旧账号异步响应不得清空当前账号输入、覆盖 loading、错误提示或成功提示。

### 7.4 批量资料初始化面板

资料初始化必须先预览，不能直接提交。

步骤一：选择账号，使用同一账号选择器。

配置区：

| 配置 | 控件 |
| --- | --- |
| 生成方式 | 分段控件：AI 随机生成 / 导入名单 / 模板兜底 / 手工序号 |
| 语言风格 | 下拉：中文、英文、混合、东南亚等 |
| 账号画像 | 下拉或多选：自然用户、行业用户、客服、社区成员 |
| 性别倾向 | 分段控件：不限、偏男性、偏女性、中性 |
| 简介生成 | 开关：生成 bio / 不生成 bio |
| username | 开关：生成 username 候选 / 不修改 username |
| 命名风格提示 | 文本框：补充昵称风格、禁用正式姓名或营销号口吻 |
| 头像策略 | 随机素材池 / 顺序分配 / 导入映射 / 只补空头像 |
| 覆盖已有资料 | 开关，默认关闭；平台展示名为占位名时，昵称 / TG 姓名仍随资料初始化同步更新 |

操作区：

```text
[AI 生成预览] [重抽全部] [只重抽失败项] [导入名单] [确认创建批次]
```

点击“确认创建批次”后先弹出二次确认弹窗，展示账号数量、将修改的资料范围、可执行 / 跳过 / 不可执行数量和操作原因；确认后才创建批次。

预览表：

| 列 | 说明 |
| --- | --- |
| 账号 | 当前账号 |
| 头像 | 将使用的头像预览 |
| 昵称 / 姓名 | AI 生成结果，可编辑 |
| 简介 | AI 生成结果，可编辑 |
| username 候选 | 3 个候选，可编辑、可重抽 |
| 校验状态 | 合格、重复、格式错误、需人工编辑 |
| 操作 | 重抽、清空、保留原值 |

### 7.5 批次详情

批次详情需要让管理员知道“到底完成到哪一步”。

批次头部：

- 批次状态：预检中、待确认、执行中、部分成功、成功、失败、已取消。
- 动作范围：设备清理、2FA、资料、username、头像。
- 进度：成功、失败、跳过、待重试。
- 操作者、原因、创建时间、trace_id。

逐账号结果：

| 字段 | 说明 |
| --- | --- |
| 账号 | display_name / 手机号 |
| 设备清理 | 未执行 / 成功 / 失败 / 需等待 |
| 2FA | 未执行 / 已设置 / 已存在 / 待邮箱确认 / 失败 |
| 资料 | 成功 / 部分成功 / 失败 |
| username | 成功值、失败原因、已尝试候选 |
| 头像 | 成功、失败、头像来源 |
| 备用 session | 未执行 / 补齐成功 / 激活恢复成功 / 失败 / 需人工 |
| 操作 | 重试失败项、查看错误、重新登录、打开授权资产 |

批次详情必须按 `account_security_batch.system_task_type` 切换列组合：

- `account_profile_init` 展示资料、username、头像和头像缓存。
- `account_device_cleanup` 展示保留设备、清理设备、等待限制和清理后外部设备数量。
- `account_2fa_setup` 展示设置 / 替换 / 待邮箱确认 / 跳过原因。
- `account_standby_session_provision` 展示目标槽位、开发者应用、代理、验证码读取、2FA 使用、健康检查和激活恢复结果。

任务中心跳转来的系统任务详情必须打开同一个批次详情组件；不能在任务中心另做一套不完整详情。

## 8. 后端结构与功能设计

### 8.1 后端模块拆分

建议新增独立服务模块，不把所有逻辑继续塞进 `accounts.py`：

```text
backend/app/models/account_security.py
  账号安全快照、授权设备快照、批次、批次项、资料生成规则、密码托管引用

backend/app/schemas/account_security.py
  前端请求 / 响应模型

backend/app/services/account_security/
  security_snapshot.py
  batch_precheck.py
  batch_service.py
  device_cleanup.py
  two_fa.py
  profile_generation.py
  profile_update.py
  username_candidates.py
  avatar_assignment.py
  batch_worker.py

backend/app/api/routers/account_security.py
  账号安全和批量资料初始化接口

backend/app/integrations/telegram/account_security.py
  Telegram 设备、2FA、资料、username、头像网关封装
```

现有账号模块只保留入口调用和基础账号读取：

```text
backend/app/services/accounts.py
  继续负责账号创建、登录、同步、资料拉取和账号详情

account_security 服务
  负责设置二步密码批次、清理登录设备批次、备用 session 补齐 / 自愈批次、资料初始化批次、AI 命名、头像分配
```

### 8.2 后端状态机

批次状态：

| 状态 | 含义 |
| --- | --- |
| draft | 已创建但未确认 |
| prechecking | 预检中 |
| ready | 预检完成，等待确认 |
| running | 执行中 |
| partial_success | 部分成功 |
| succeeded | 全部成功或按规则跳过 |
| failed | 全部失败或关键步骤失败 |
| cancelled | 已取消 |

批次项状态：

| 状态 | 含义 |
| --- | --- |
| pending | 等待执行 |
| skipped | 预检跳过 |
| waiting | 受 Telegram 限制，需要等待 |
| running | 执行中 |
| partial_success | 部分步骤成功 |
| succeeded | 成功 |
| failed | 失败 |
| manual_required | 需要人工处理 |

### 8.3 预检功能

`batch_precheck.py` 负责一次性完成所有动作预检：

- 校验账号归属和权限。
- 校验 Session 是否在线。
- 读取平台可信设备和外部设备数量。
- 读取 2FA 状态。
- 读取现有头像、姓名、bio、username。
- 调用 AI 生成资料预览。
- 校验 username 格式、本批次重复、候选数量。
- 校验头像素材是否存在、格式是否可用。
- 生成每个账号的 `precheck_status`、`blockers`、`warnings`、`suggested_actions`。

预检不能修改 Telegram 远端资料，只能写预检记录和预览结果。

### 8.4 AI 随机命名服务

`profile_generation.py` 负责调用现有 AI 配置，不直接绑定某一家模型。

输入：

```text
tenant_id
account_ids
count
language_style
persona_style
gender_bias
age_style
bio_enabled
username_enabled
forbidden_words
existing_names
existing_usernames
```

输出：

```text
generated_display_name
generated_first_name
generated_last_name
generated_bio
username_candidates[]
generation_provider
generation_prompt_version
```

规则：

- 一次批量生成后必须本地去重。
- 输出不合格时最多自动重试一次。
- AI 不可用、超时或当前供应商不可用时，预检可以进入本地随机资料兜底并在预览中提示原因；管理员仍可手工编辑、重抽或切换模板。
- 不默认使用序号生成，避免账号资料过于规律。
- AI prompt 和输出摘要要写入批次 trace，但不保存敏感账号明文密码。

### 8.5 执行功能

`batch_worker.py` 按账号项执行：

```text
领取批次项
  ↓
账号级互斥锁
  ↓
刷新账号和安全快照
  ↓
按需补齐 standby_1 / standby_2 session
  ↓
执行设备清理
  ↓
执行 2FA 设置
  ↓
执行 profile 更新
  ↓
执行 username 设置
  ↓
检查头像素材缓存 ready；只对 ready 头像执行头像设置
  ↓
执行头像设置
  ↓
拉取远端 profile 回写
  ↓
写批次项结果、账号快照、审计
```

执行原则：

- 每一步独立记录状态，不能只给一个大失败。
- profile、username、avatar 任一步成功都要保存结果。
- username 候选按顺序尝试；都失败则只标记 username 失败，不回滚昵称和头像。
- avatar 步骤必须区分 `waiting_cache`、`succeeded`、`skipped`、`failed`；等待缓存不是资料初始化完成，不得把批次提前标成成功。
- 任务中心必须能按批次展示头像缓存进度：待缓存、缓存中、已 ready、FloodWait、缓存失败、不可恢复。
- `account-security` worker 每轮必须先推进素材 TG 缓存，再执行资料初始化批次；也允许单独启动 `material-cache` worker 只处理素材暂存。
- 登录后自动资料初始化只负责创建批次，不直接执行 profile / username / avatar 更新；执行仍统一进入 `account-security` worker。若头像素材池为空或缓存未 ready，账号项必须在批次详情中显示跳过、等待或失败原因，不允许静默当作已完整初始化。
- 接码专用账号在登录后自动资料初始化、账号面具初始化、批量资料预检、设置 2FA 预检、设备清理预检和 `account-security` worker 执行前都必须硬拦截；拦截状态写 `code_receiver_reserved` 或等价可读原因。接码账号登录遇到 Telegram 2FA 时只能记录当前输入密码，不得调用 Telegram 修改 / 轮换真实 2FA 密码。备用 session 补齐 / 自愈不属于该禁用动作集合，仍可执行。
- 执行完成后调用现有资料同步能力刷新账号详情。
- 同一账号同时只能有一个批次项执行。

### 8.6 后端接口响应

批量预览响应需要直接支撑前端表格：

```json
{
  "batch_preview_id": "preview_123",
  "summary": {
    "total": 20,
    "executable": 16,
    "skipped": 2,
    "manual_required": 2
  },
  "items": [
    {
      "account_id": 1,
      "display_name_before": "旧账号",
      "generated_display_name": "锅巴洋芋",
      "generated_first_name": "锅巴洋芋",
      "generated_last_name": "",
      "generated_bio": "看到有意思的会回两句",
      "username_candidates": ["guoba_yangyu", "potato_crisp", "yangyu_daily"],
      "avatar_source": "material:12/avatar-01.jpg",
      "precheck_status": "executable",
      "warnings": []
    }
  ]
}
```

## 9. 数据模型建议

### 9.1 账号安全快照

```text
tg_account_security_snapshots
  id
  tenant_id
  account_id
  trusted_session_status
  two_fa_status
  external_authorization_count
  last_device_scan_at
  last_2fa_check_at
  profile_status
  profile_last_updated_at
  trusted_device_label
  last_hardened_at
  last_error
  trace_id
  created_at
```

`trusted_session_status`：

- confirmed
- unknown
- missing
- stale

`two_fa_status`：

- unknown
- missing
- enabled
- pending_email_confirmation
- failed

`profile_status`：

- unknown
- incomplete
- complete
- update_pending
- update_failed

### 9.2 登录设备快照

授权资产权威表：

```text
tg_account_authorizations
  id
  tenant_id
  account_id
  role: primary / standby_1 / standby_2
  developer_app_id
  proxy_id
  session_ciphertext
  status: active / standby / unhealthy / disabled
  health_status
  is_current
  telegram_authorization_hash_ciphertext
  last_health_check_at
  last_success_at
  last_switched_at
  failure_reason
  disabled_at
  created_by
  created_at
  updated_at
```

规则：

- 每条授权资产必须对应一次真实 Telegram 登录成功记录；只有开发者应用或代理配置，没有 `session_ciphertext`，不算可切换备用授权。
- 存量账号迁移期没有授权资产表记录时，由 `tg_accounts.developer_app_id + tg_accounts.proxy_id + tg_accounts.session_ciphertext` 投影为主授权。
- 备用授权切换为主授权时更新 `is_current` 和 `role`，旧主授权进入异常或备用待修复状态，不自动删除 session。
- 停用授权资产必须写审计，并且不得清理官方锚点设备。

登录设备快照只记录 Telegram 当前返回的授权设备事实，不替代授权资产权威表：

```text
tg_account_authorization_snapshots
  id
  tenant_id
  account_id
  batch_id
  authorization_hash_ciphertext
  is_platform_trusted
  is_current_session
  device_model
  platform
  system_version
  api_id
  app_name
  app_version
  ip_masked
  country
  region
  date_created
  date_active
  status
  scanned_at
```

说明：

- `authorization_hash` 属于敏感操作凭据，建议加密或只在执行期保存。
- IP 只展示脱敏值，完整 IP 如需保存必须加密并受权限控制。
- 快照用于审计和差异比较，不作为长期设备资产管理。

### 9.3 安全处理批次

```text
tg_account_security_batches
  id
  tenant_id
  action_types
  status
  total_count
  success_count
  skipped_count
  failed_count
  created_by
  confirmed_by
  password_strategy
  password_secret_ref
  profile_strategy
  username_strategy
  avatar_strategy
  overwrite_existing_profile
  reason
  trace_id
  created_at
  started_at
  finished_at
```

`action_types`：

- cleanup_devices
- set_two_fa
- update_profile
- update_username
- update_avatar
- provision_standby_session
- self_heal_session

每个批次只允许来自一个入口的动作集合：设置二步密码批次只包含 `set_two_fa`，清理登录设备批次只包含 `cleanup_devices`，备用 session 补齐 / 自愈批次只包含 `provision_standby_session` 或 `self_heal_session`，资料初始化批次可以包含 `update_profile`、`update_username`、`update_avatar`。

### 9.4 安全处理项

```text
tg_account_security_batch_items
  id
  batch_id
  tenant_id
  account_id
  status
  precheck_status
  cleanup_status
  two_fa_status
  profile_status
  username_status
  avatar_status
  external_devices_before
  external_devices_after
  generated_display_name
  generated_first_name
  generated_last_name
  generated_username
  avatar_source
  avatar_cache_status
  avatar_cache_peer_id
  avatar_cache_message_id
  avatar_object_key_after
  skipped_reason
  failure_type
  failure_detail
  next_retry_at
  trace_id
  created_at
  started_at
  finished_at
```

头像缓存字段可在第一版由响应层根据 `avatar_source -> materials` 派生；如果实现上需要避免详情页多表实时扫描，可以落表保存最近一次缓存快照。无论采用哪种实现，任务中心详情必须能看到每个账号头像步骤对应的缓存状态和更新后的头像对象。

### 9.5 批量资料生成规则

```text
tg_account_profile_batch_rules
  id
  batch_id
  tenant_id
  generation_mode
  ai_provider_id
  ai_prompt_version
  language_style
  persona_style
  gender_bias
  age_style
  forbidden_words
  uniqueness_seed
  name_base
  name_start_index
  name_padding
  username_prefix
  username_start_index
  username_padding
  username_max_attempts
  bio_template
  avatar_assignment_mode
  overwrite_existing
  created_at
```

## 10. 接口设计建议

账号中心接口：

```text
GET  /api/tg-accounts/security/summary
GET  /api/tg-accounts/{account_id}/security
POST /api/tg-accounts/{account_id}/security/refresh
POST /api/tg-accounts/{account_id}/authorizations/standby/provision
POST /api/tg-accounts/{account_id}/authorizations/self-heal
POST /api/tg-accounts/{account_id}/security/cleanup-devices
POST /api/tg-accounts/{account_id}/security/set-2fa
POST /api/tg-accounts/{account_id}/security/update-profile
POST /api/tg-accounts/{account_id}/security/managed-2fa
POST /api/tg-accounts/{account_id}/security/managed-2fa/rotate
POST /api/tg-accounts/{account_id}/security/managed-2fa/reveal
```

批量接口：

```text
POST /api/tg-accounts/security-batches/precheck
POST /api/tg-accounts/security-batches
POST /api/tg-accounts/security-batches/profile-preview
GET  /api/tg-accounts/security-batches
GET  /api/tg-accounts/security-batches/{batch_id}
POST /api/tg-accounts/security-batches/{batch_id}/retry
POST /api/tg-accounts/security-batches/{batch_id}/cancel
```

任务中心系统投影：

```text
GET /api/tasks
GET /api/tasks/{task_id}
```

投影规则：

- `GET /api/tasks` 默认包含资料初始化批次投影；筛选普通任务类型时不混入，筛选 `type=account_profile_init` 时只返回资料初始化批次。
- `GET /api/tasks` 也必须包含清理登录设备、设置二步密码和备用 session 自动补齐批次投影；筛选 `type=account_device_cleanup`、`type=account_2fa_setup`、`type=account_standby_session_provision` 时只返回对应系统批次。
- 投影 ID 固定为 `account_security_batch:{batch_id}`，详情接口通过 `GET /api/tasks/{task_id}` 读取，避免和普通任务 UUID 混淆。
- 投影 `type` 根据批次动作生成：`account_profile_init`、`account_device_cleanup`、`account_2fa_setup`、`account_standby_session_provision`；`target_summary` 分别为账号资料初始化、清理登录设备、设置二步密码、备用 session 补齐。
- 普通任务控制接口不接受投影 ID；启动、暂停、停止、重置、删除只适用于真实 `tasks` 表任务。
- 详情响应扩展 `account_security_batch`，包含批次状态、账号项列表、动作类型、账号级状态、等待原因、失败原因、保留平台 session 摘要、外部设备清理结果、2FA 设置 / 替换结果、备用 session 补齐结果；资料初始化批次额外包含头像缓存汇总、头像来源、缓存状态和更新后的头像预览。

请求示例：

```json
{
  "account_ids": [1, 2, 3],
  "action_types": ["cleanup_devices", "set_two_fa", "update_profile", "update_username", "update_avatar"],
  "password_strategy": "generated_unique",
  "profile_strategy": {
    "generation_mode": "ai_random",
    "language_style": "中文",
    "persona_style": "自然用户",
    "gender_bias": "不限",
    "age_style": "不限",
    "bio_enabled": true,
    "username_enabled": true,
    "username_prefix_hint": "",
    "username_max_attempts": 3,
    "overwrite_existing": false
  },
  "avatar_strategy": {
    "mode": "random_from_material_pool",
    "material_group_id": 12
  },
  "recovery_email": "",
  "reason": "账号接入后统一安全加固"
}
```

## 11. Telegram 网关接口建议

在 `backend/app/integrations/telegram` 增加账号安全网关能力：

```text
list_authorizations(session_ciphertext, credentials, proxy)
cleanup_authorization(session_ciphertext, credentials, proxy, authorization_hash)
get_two_fa_status(session_ciphertext, credentials, proxy)
set_two_fa_password(session_ciphertext, credentials, proxy, password, hint, recovery_email)
confirm_two_fa_email(session_ciphertext, credentials, proxy, code)
update_profile(session_ciphertext, credentials, proxy, first_name, last_name, bio)
update_username(session_ciphertext, credentials, proxy, username)
update_profile_photo(session_ciphertext, credentials, proxy, avatar_path)
read_current_authorization(session_ciphertext, credentials, proxy)
```

实现原则：

- 所有 Telegram 调用继续走账号绑定代理。
- 网关返回稳定中文 failure_type，不把 Telethon 原始异常直接暴露给前端。
- 清理设备前后都要读取设备列表，不能只相信单次 API 返回。
- 不提供 `cleanup_other_authorizations` 作为业务接口；批量设备清理必须基于设备列表逐个 `cleanup_authorization`，并在执行前确认不会清理 primary、standby_1、standby_2 或官方锚点设备。
- 2FA 设置走独立服务封装 SRP 细节，业务层只处理状态机。
- 资料初始化要拆成 profile、username、avatar 三个独立步骤；某一步失败不能抹掉其他步骤的成功结果。
- username 设置必须支持候选名重试，并把最终成功用户名回写账号资料。

## 12. 权限与审计

新增权限建议：

| 权限 | 用途 |
| --- | --- |
| accounts.security.read | 查看账号安全状态和设备快照 |
| accounts.security.cleanup_devices | 清理外部设备 |
| accounts.security.set_2fa | 设置二步验证密码 |
| accounts.security.batch | 创建设置二步密码 / 清理登录设备 / 备用 session 补齐批次 |
| accounts.security.session_manage | 手动补齐、切换、停用和自愈账号授权 session |
| accounts.security.credential_manage | 配置、轮换平台托管 2FA 密码策略 |
| accounts.security.export_secret | 导出或查看托管密码 |
| accounts.profile.batch_update | 批量设置头像、昵称、简介和用户名 |
| accounts.profile.overwrite | 覆盖已有账号资料 |

审计动作：

- 查看账号安全详情
- 同步安全状态
- 创建账号安全处理批次
- 确认账号安全处理批次
- 清理外部设备成功 / 失败 / 部分成功
- 设置二步验证成功 / 失败 / 待邮箱确认
- 批量设置账号资料成功 / 失败 / 部分成功
- 批量设置用户名成功 / 失败 / 跳号成功
- 批量设置头像成功 / 失败
- 查看或导出二步验证密码
- 重试 / 取消批次

审计详情必须包含：

```text
account_id
batch_id
action_types
operator
reason
external_devices_before
external_devices_after
two_fa_status_before
two_fa_status_after
profile_before
profile_after
generated_username
avatar_source
failure_type
trace_id
```

## 13. 风控联动

风控中心新增风险原因：

| 风险原因 | 风控处理 |
| --- | --- |
| 存在外部设备 | 账号可降低评分；高风险策略下阻塞新任务 |
| 平台可信设备无法确认 | 阻塞设备清理，任务执行可按账号健康状态决定 |
| 未设置二步验证 | 降低评分；可提示设置二步密码 |
| 2FA 设置失败 | 进入处置队列 |
| 资料不完整 | 普通提醒；不默认阻塞任务 |
| 需重新资料初始化 | 普通提醒；账号中心提供筛选和重新触发资料初始化入口，不默认阻塞任务 |
| 头像重复度过高 | 降低账号可信度提示，建议批量换头像 |
| username 设置失败 | 普通提醒，除非任务依赖公开用户名 |
| 新 Session 未满 24 小时 | 延后加固，不直接判定账号异常 |

任务中心只消费风控结果，不直接展示设备和密码细节。

## 14. 执行与并发策略

默认按账号维度串行执行，每个批次有界并发：

- 单批次默认并发：2 到 5 个账号。
- 同一账号同一时间只允许一个安全处理项执行。
- 设备清理和 2FA 设置都要先拿账号级互斥锁。
- 头像、昵称、用户名更新也要拿账号级互斥锁，同一账号不能并发改资料。
- 遇到 Telegram 限制、FloodWait 或 24 小时限制时，记录 `next_retry_at`，不持续重试。
- 失败重试最多 2 次；安全类失败不能无限循环。
- 批量执行期间账号仍可参与任务，但推荐在“清理设备中 / 设置2FA中 / 修改资料中”状态下暂停该账号新执行项，避免 Session 状态变化。
- 资料初始化按 profile -> username -> avatar 顺序执行，便于失败定位；如果将来拆成独立子批次，也必须保留同一个资料初始化批次视图、任务中心投影和账号级结果汇总。

## 15. 实施阶段

### 阶段一：可见与预检

- 增加账号安全 Tab。
- 增加设备读取和 2FA 状态读取。
- 增加账号资料状态读取，包括头像、昵称、简介和 `@username`。
- 增加批量预检和资料预览，不实际执行清理和设置。
- 风控中心展示“外部设备未清理”“未设置二步验证”风险。

验收：

- 在线账号能同步安全状态并更新设备列表。
- 账号列表能筛选“登录有问题”“存在外部设备”“未做过登录设备清理”“未设置二步验证”“资料待初始化”“需重新资料初始化”“备用 session 缺口”“可从备用 session 激活恢复”。
- 批量资料预览能展示 AI 随机生成的昵称、简介和多个 username 候选。
- 批量预检能清楚返回可执行、跳过、需等待、需人工处理。

### 阶段二：设备清理

- 实现单账号清理外部设备。
- 实现批量清理外部设备。
- 支持筛选未做过登录设备清理的账号，支持当前筛选全选和跨页累计选择。
- 清理设备前自动补齐 standby_1 / standby_2 session。
- 处理 24 小时限制、primary / standby 平台 session 保护、部分成功。

验收：

- 不会退出 primary session、standby_1 session、standby_2 session 和官方锚点设备。
- 只有 primary session 已登录、standby_1 / standby_2 未登录时，清理设备批次先自动补齐备用 session；补齐失败的账号跳过清理并展示原因。
- 清理后会重新读取设备列表并更新快照。
- 失败账号有明确原因和可重试入口。
- 清理登录设备批次在任务中心可见，能看到运行状态、成功 / 跳过 / 失败数量和账号级失败原因。

### 阶段三：二步验证设置

- 实现未设置 2FA 账号的设置流程。
- 支持平台统一托管 2FA 密码。
- 支持邮箱确认状态流转。
- 增加密码托管、查看、导出权限和审计。

验收：

- 未设置 2FA 的账号可以批量设置。
- 已设置 2FA 且平台托管旧密码的账号可以替换为平台统一托管密码；不知道旧密码的账号必须进入人工处理，不误覆盖。
- 密码不在前端完整回显，查看和导出必须审计。
- 备用 session 自动补齐时可以使用平台托管 2FA 密码完成登录。
- 接码专用账号在设置 2FA、保存托管 2FA 或轮换托管 2FA 中必须跳过 / 拒绝，worker 执行前必须二次拦截；登录 2FA 只记录当前密码，不得生成并写入新的 Telegram 2FA；备用 session 补齐 / 自愈仍可使用已托管 2FA 完成登录。

### 阶段四：资料批量初始化

- 实现批量设置昵称 / 姓名 / 简介。
- 实现批量设置 `@username`，支持自动跳号和候选名重试。
- 实现批量设置头像，支持素材池随机、顺序分配和只补空头像。
- 资料初始化批次进入任务中心展示后台状态，支持查看账号级执行结果和头像缓存进度。
- 头像更新只使用已缓存完成的头像素材，未缓存完成时等待或按规则跳过，不直接使用尚未缓存完成的临时文件。
- 执行后同步远端资料并回写账号详情。

验收：

- 点击资料初始化后，能在抽屉内选择账号，并调用 AI 随机生成不同昵称、简介和 username 候选。
- `@username` 被占用时能自动尝试下一个候选。
- 已有资料默认不覆盖，勾选覆盖时必须二次确认并写审计。
- 头像设置失败不影响昵称和用户名成功结果。
- 批次提交后在任务中心能看到运行状态、等待缓存状态、成功 / 跳过 / 失败数量和账号级失败原因。
- 素材缓存未完成时不会提前更新头像；缓存完成后才执行头像更新。
- 更新头像成功后，账号列表和账号详情能回显新头像。
- 接码专用账号登录后不自动创建资料初始化批次，不初始化账号面具；批量资料预览和 worker 执行前都必须跳过并显示“接码专用账号只允许接收验证码”等可读原因。

### 阶段五：风控闭环

- 设备和 2FA 风险进入账号评分。
- 资料完整度、头像重复度和 username 设置结果进入风险提示。
- 高风险策略可阻塞未加固账号参与任务。
- 设置二步密码和清理登录设备结果进入运营概览和处置队列。

验收：

- 任务预检能提示账号安全风险。
- 风控中心能跳转到账号安全批量处理。
- 加固成功后账号风险能恢复或降级为普通提醒。

## 16. 测试范围

后续实现时至少补以下测试：

- 在线账号设备刷新成功。
- Session 失效账号设备刷新失败并进入需重新登录。
- 设备清理不会删除当前平台 Session。
- 新 Session 未满 24 小时时标记需等待。
- 批量预检混合成功、跳过、失败账号。
- 未设置 2FA 账号设置成功。
- 已设置 2FA 账号默认跳过。
- 邮箱未确认进入待确认状态。
- 批量昵称默认按 AI 随机生成成功。
- AI 生成失败、超时或供应商不可用时能提示原因，并用本地随机资料兜底；不自动静默改成规律序号。
- 批量 username 预检能识别本批次重复和格式错误。
- username 被占用时能自动跳号，候选耗尽后记录失败。
- 批量头像支持随机分配和只补空头像。
- 已有资料默认不覆盖，覆盖时写审计。
- 密码查看 / 导出必须有权限并写审计。
- 风控预检能识别未加固账号。
