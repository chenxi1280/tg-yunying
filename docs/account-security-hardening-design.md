# TG 账号安全加固与资料初始化设计文档

## 1. 背景与目标

账号登录到平台后，还需要同时解决安全加固和账号资料初始化问题：

```text
账号已登录
  -> 清理非平台登录设备
  -> 只保留平台可信 Session
  -> 设置平台可信设备显示名
  -> 检查是否已设置 Telegram 二步验证密码
  -> 未设置的账号尽量批量设置二步验证密码
  -> 批量设置头像、昵称 / 姓名、@username
  -> 写入账号安全状态、资料初始化结果、批量任务结果和审计记录
```

本能力属于账号中心和风控中心之间的账号安全治理与账号资料初始化，不属于任务中心。

目标：

- 账号中心能看到每个 TG 账号的登录设备、平台可信状态、二步验证状态和最近加固结果。
- 管理员可以选择多个账号，批量执行“清理外部设备”和“设置二步验证密码”。
- 管理员可以选择多个账号，批量设置 TG 账号头像、昵称 / 姓名、简介和 `@username`。
- 昵称、姓名、简介和 `@username` 默认用一次 AI 请求生成整批资料，支持命名风格提示、预览、去重、可编辑、可重抽；序号递增只作为手工兜底策略。
- 平台可信设备显示名应统一，方便在 Telegram 登录设备列表里识别平台 Session。
- 批量执行前必须预检，明确哪些账号可执行、哪些需要等待、哪些需要人工处理。
- 执行过程必须可暂停、可重试、可追踪、可审计。
- 不长期明文保存 Telegram 二步验证密码，不在前端回显敏感信息。

非目标：

- 不管理用户自己的手机、电脑、浏览器等真实硬件资产。
- 不把“设备清理”做成任务中心的运营任务。
- 不绕过 Telegram 官方安全限制，例如新登录 Session 24 小时内不能退出其他 Session 的限制。
- 不删除当前平台 Session，否则平台会失去该账号控制能力。

## 2. 概念定义

| 概念 | 说明 |
| --- | --- |
| 平台可信设备 | 当前平台使用 `session_ciphertext + developer_app_id + proxy_id` 连接 Telegram 的授权 Session。产品上叫“平台可信设备”，技术上是当前 MTProto 授权会话。 |
| 平台设备显示名 | Telegram 授权列表里展示的平台客户端信息，例如 device_model、system_version、app_version。它帮助识别平台 Session，不等同于 TG 账号昵称。 |
| 外部设备 | Telegram 返回的其他授权会话，例如手机端、桌面端、网页版、未知 API 客户端等。 |
| 设备清理 | 退出外部设备，只保留平台可信设备。 |
| 二步验证密码 | Telegram 账号的 2FA password，不是平台后台用户登录密码。 |
| 账号资料初始化 | 批量设置 TG 账号头像、first_name、last_name、bio 和 `@username`。 |
| AI 随机命名 | 调用平台 AI 能力，按运营画像、语言风格、性别倾向、地区倾向、禁用词和命名风格提示生成随机昵称、姓名、简介和 username 候选。 |
| 命名规则 | 批量生成昵称、姓名或用户名的规则。默认使用 AI 随机命名；基础名 + 序号只作为兜底手工模式。 |
| 安全加固批次 | 一次批量选择多个账号后创建的安全处理批次。 |
| 安全加固项 | 批次里针对单个账号的一条处理记录。 |

## 3. Telegram 能力边界

后续实现需要基于 Telegram MTProto / Telethon 能力做适配：

- 读取登录设备：`account.getAuthorizations`，用于获取当前账号已授权 Session 列表。
- 退出指定设备：`account.resetAuthorization(hash)`，按 Session hash 退出单个外部授权。
- 退出其他设备：`auth.resetAuthorizations`，退出除当前 Session 外的其他授权。
- 设置二步验证密码：`account.updatePasswordSettings`，需要按 SRP 流程生成密码校验和新密码设置。
- 设置账号姓名 / 简介 / 头像：更新 Telegram profile，例如 first_name、last_name、about、profile photo。
- 设置 `@username`：更新 Telegram username，必须先做格式和冲突预检，最终以 Telegram 返回结果为准。

关键限制：

- Telegram 可能禁止新登录不到 24 小时的 Session 退出其他设备，错误通常表现为 `FRESH_RESET_AUTHORISATION_FORBIDDEN`。
- 不能退出当前平台 Session；如果退出当前 Session，该账号在平台内会变成需重新登录。
- 二步验证密码设置可能需要恢复邮箱确认；邮箱未确认时必须进入“待邮箱验证码确认”状态。
- 已有二步验证密码的账号不能直接覆盖；需要提供旧密码或进入人工恢复流程。
- `@username` 可能已被占用、格式不合法或触发 Telegram 限制；批量设置时必须支持自动跳号、重试候选名和失败留痕。
- 头像上传、用户名修改、姓名修改都可能触发 Telegram 频控；需要有界并发和失败重试，不应对同一账号连续快速修改。
- 清理设备、设置 2FA 和批量资料初始化都属于敏感动作，必须有权限、二次确认和审计。

参考：

- Telegram `account.getAuthorizations`: https://core.telegram.org/method/account.getAuthorizations
- Telegram `account.resetAuthorization`: https://core.telegram.org/method/account.resetAuthorization
- Telegram `auth.resetAuthorizations`: https://core.telegram.org/method/auth.resetAuthorizations
- Telegram 2FA / SRP: https://core.telegram.org/api/srp

## 4. 模块归属

```text
账号中心
  展示账号安全事实、发起单账号/批量安全加固、批量资料初始化、查看批次和账号结果

风控中心
  汇总外部设备未清理、2FA 未设置、资料不完整、设备异常变化等风险，并阻塞或降级账号参与任务

审计中心
  记录预检、确认、执行、跳过、失败、重试、敏感配置变更

执行中心
  按批次消费账号安全加固项和资料初始化项，执行 Telegram API 调用并回写结果
```

账号中心负责“怎么处理账号安全事实”，风控中心负责“这些安全事实会不会影响账号参与运营任务”。

## 5. 页面设计

### 5.1 账号列表

账号列表增加轻量字段，不把表格做成复杂安全控制台：

| 字段 | 展示 |
| --- | --- |
| 平台可信设备 | 已确认、待确认、无法确认 |
| 外部设备 | 无外部设备、存在 N 个外部设备、读取失败 |
| 二步验证 | 已设置、未设置、待邮箱确认、未知、设置失败 |
| 资料完整度 | 头像、昵称、用户名是否已设置 |
| 安全加固 / 资料初始化 | 最近成功时间、最近失败原因 |

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
- 操作按钮：刷新设备、清理外部设备、设置二步验证、设置资料、查看批次记录。

### 5.3 批量加固抽屉

批量抽屉分三步：

```text
选择动作
  ↓
预检账号
  ↓
确认并执行
```

动作选择：

| 动作 | 说明 |
| --- | --- |
| 清理外部设备 | 退出非平台授权会话，保留平台可信 Session |
| 设置二步验证密码 | 对未设置 2FA 的账号尽量设置 |
| 设置头像 | 批量上传头像，可固定、随机或按账号映射 |
| 设置昵称 / 姓名 | 批量设置 first_name、last_name、展示名，默认 AI 随机生成 |
| 设置 `@username` | 批量设置 Telegram username，默认 AI 生成候选并支持冲突重抽 |

预检表字段：

| 字段 | 说明 |
| --- | --- |
| 账号 | display_name、username、phone_masked |
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
- 操作者需要输入确认文案，例如“确认加固”。

## 6. 批量流程

### 6.1 设备清理流程

```text
管理员选择账号
  ↓
读取账号 Session、开发者应用和代理
  ↓
调用 getAuthorizations
  ↓
识别平台可信 Session
  ↓
计算外部设备列表
  ↓
预检是否可退出外部设备
  ↓
逐个 resetAuthorization(hash)
  ↓
重新读取设备列表
  ↓
确认只剩平台可信 Session 或记录剩余外部设备
  ↓
回写账号安全快照和审计
```

识别平台可信 Session 的原则：

- 优先识别当前连接对应的授权会话。
- 结合 `api_id`、应用名、平台标识、创建时间、活跃时间和当前 Session 特征判断。
- 不能只依赖设备名称；设备名称可以被伪造或变化。
- 如果无法稳定识别平台可信 Session，只允许使用“退出其他设备但保留当前 Session”的 Telegram 官方能力，不允许按猜测删除。

失败处理：

| 失败 | 处理 |
| --- | --- |
| Session 失效 | 标记需重新登录，跳过设备清理 |
| 新 Session 未满 24 小时 | 标记“需等待”，建议自动延后到可执行时间 |
| 外部设备 hash 无效 | 刷新设备列表后重试一次 |
| 无法识别平台 Session | 不执行单设备删除，进入人工确认 |
| 清理后仍有外部设备 | 标记部分成功，保留剩余设备明细 |

### 6.2 二步验证设置流程

```text
管理员选择账号
  ↓
预检账号是否在线
  ↓
读取当前 2FA 状态
  ↓
未设置：生成或读取本次密码策略
  ↓
调用 updatePasswordSettings
  ↓
如需要邮箱确认，进入待确认状态
  ↓
确认成功后写入 2FA 状态和审计
```

密码策略建议：

| 策略 | 说明 | 默认 |
| --- | --- | --- |
| 系统生成每账号唯一密码 | 每个账号生成不同强密码，加密托管，支持一次性导出 | 推荐 |
| 批量统一密码 | 多个账号使用同一密码，操作简单但风险集中 | 不推荐，仅超级管理员可用 |
| 人工输入每账号密码 | 上传或逐个填写，不长期保存明文 | 可选 |

安全要求：

- 前端不回显完整二步验证密码。
- 后端只允许加密保存，或只在批次执行期间以 TTL 密文保存，执行完成后清理临时明文材料。
- 如果平台需要长期托管 2FA 密码，必须使用独立的 `account_security_credentials` 表，字段加密，并提供“最后使用时间”“最后查看人”“一次性导出记录”。
- 批量统一密码必须显示风险提示并写入审计原因。
- 密码 hint 不能包含完整密码。
- 恢复邮箱可选；如果配置恢复邮箱，必须处理邮箱验证码确认流程。

已有 2FA 的账号处理：

| 状态 | 默认处理 |
| --- | --- |
| 已设置且平台不知道旧密码 | 跳过，标记“已设置，需旧密码才可修改” |
| 已设置且平台托管旧密码 | 可按管理员选择更新，但默认不覆盖 |
| 未设置 | 按本次策略设置 |
| 状态未知 | 先刷新状态，不直接设置 |
| 待邮箱确认 | 不重复创建，继续确认流程 |

### 6.3 账号资料批量初始化流程

```text
管理员选择账号
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
确认执行
  ↓
逐账号调用 Telegram profile / username / photo 更新
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
| 导入映射表 | 指定 account_id / phone_masked / username 对应头像文件 |
| 只补空头像 | 已有头像不覆盖 |

头像要求：

- 上传前校验格式、大小和可读取性。
- 执行后必须拉取远端 profile 或读取更新结果，确认头像已设置。
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

前端不新增独立一级导航，仍然放在“TG 账号管理”里，避免账号相关能力分散。

```text
TG账号管理
  ├─ 账号列表
  │   ├─ 批量选择
  │   ├─ 批量安全加固
  │   └─ 批量资料初始化
  ├─ 账号详情
  │   ├─ 基础信息
  │   ├─ 账号安全
  │   ├─ 资料初始化记录
  │   └─ 操作记录
  └─ 批次中心
      ├─ 加固批次列表
      ├─ 批次详情
      └─ 失败重试
```

建议前端拆分文件：

```text
frontend/src/app/views/AccountsView.tsx
  保留账号列表和批量入口

frontend/src/app/views/AccountSecurityDrawer.tsx
  单账号安全详情、设备列表、2FA 状态、资料状态

frontend/src/app/views/AccountSecurityBatchDrawer.tsx
  批量动作选择、预检、确认、执行结果

frontend/src/app/views/AccountProfileInitPanel.tsx
  AI 随机命名、头像策略、username 候选、资料预览

frontend/src/app/views/AccountSecurityBatchDetailModal.tsx
  批次详情、逐账号结果、失败重试
```

### 7.2 账号列表交互

账号列表增加批量选择栏：

```text
已选择 N 个账号
  [资料初始化] [设置二步密码] [清理登录设备] [刷新安全状态] [清空选择]
```

表格新增列：

| 列 | 内容 |
| --- | --- |
| 安全状态 | 平台可信设备、外部设备数量、2FA 状态 |
| 资料状态 | 头像、昵称、简介、username 是否完整 |
| 最近批次 | 最近一次加固 / 资料初始化结果 |

筛选项：

- 存在外部设备
- 未设置 2FA
- 资料不完整
- 无头像
- 无 username
- 最近批量失败
- 新登录未满 24 小时

### 7.3 批量安全加固抽屉

步骤一：选择动作。

```text
[x] 清理外部设备
[x] 设置二步验证密码
```

步骤二：预检。

- 展示每个账号的可执行状态。
- 对新 Session 未满 24 小时的账号显示预计可重试时间。
- 对已设置 2FA 的账号默认跳过。
- 对 Session 失效账号引导重新登录。

步骤三：确认执行。

- 汇总会执行、会跳过、需等待、需人工处理的数量。
- 要求输入确认文案。
- 创建批次后进入批次详情。

### 7.4 批量资料初始化面板

资料初始化必须先预览，不能直接提交。

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
| 覆盖已有资料 | 开关，默认关闭 |

操作区：

```text
[AI 生成预览] [重抽全部] [只重抽失败项] [导入名单] [确认创建批次]
```

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
| 账号 | display_name / phone_masked |
| 设备清理 | 未执行 / 成功 / 失败 / 需等待 |
| 2FA | 未执行 / 已设置 / 已存在 / 待邮箱确认 / 失败 |
| 资料 | 成功 / 部分成功 / 失败 |
| username | 成功值、失败原因、已尝试候选 |
| 头像 | 成功、失败、头像来源 |
| 操作 | 重试失败项、查看错误、重新登录 |

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
  负责安全加固批次、资料初始化批次、设备清理、2FA 设置、AI 命名、头像分配
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
执行设备清理
  ↓
执行 2FA 设置
  ↓
执行 profile 更新
  ↓
执行 username 设置
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

### 9.3 安全加固批次

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
  confirm_text
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
- set_trusted_device_label
- update_profile
- update_username
- update_avatar
- cleanup_devices,set_two_fa

### 9.4 安全加固项

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
  skipped_reason
  failure_type
  failure_detail
  next_retry_at
  trace_id
  created_at
  started_at
  finished_at
```

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
POST /api/tg-accounts/{account_id}/security/cleanup-devices
POST /api/tg-accounts/{account_id}/security/set-2fa
POST /api/tg-accounts/{account_id}/security/update-profile
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
cleanup_other_authorizations(session_ciphertext, credentials, proxy)
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
| accounts.security.batch | 创建批量加固 |
| accounts.security.export_secret | 导出或查看托管密码 |
| accounts.profile.batch_update | 批量设置头像、昵称、简介和用户名 |
| accounts.profile.overwrite | 覆盖已有账号资料 |

审计动作：

- 查看账号安全详情
- 刷新登录设备
- 创建账号安全加固批次
- 确认账号安全加固批次
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
| 未设置二步验证 | 降低评分；可提示批量加固 |
| 2FA 设置失败 | 进入处置队列 |
| 资料不完整 | 普通提醒；不默认阻塞任务 |
| 头像重复度过高 | 降低账号可信度提示，建议批量换头像 |
| username 设置失败 | 普通提醒，除非任务依赖公开用户名 |
| 新 Session 未满 24 小时 | 延后加固，不直接判定账号异常 |

任务中心只消费风控结果，不直接展示设备和密码细节。

## 14. 执行与并发策略

默认按账号维度串行执行，每个批次有界并发：

- 单批次默认并发：2 到 5 个账号。
- 同一账号同一时间只允许一个安全加固项执行。
- 设备清理和 2FA 设置都要先拿账号级互斥锁。
- 头像、昵称、用户名更新也要拿账号级互斥锁，同一账号不能并发改资料。
- 遇到 Telegram 限制、FloodWait 或 24 小时限制时，记录 `next_retry_at`，不持续重试。
- 失败重试最多 2 次；安全类失败不能无限循环。
- 批量执行期间账号仍可参与任务，但推荐在“清理设备中 / 设置2FA中 / 修改资料中”状态下暂停该账号新执行项，避免 Session 状态变化。
- 资料初始化按 profile -> username -> avatar 顺序执行，便于失败定位；也可以在后续实现中按策略拆分为独立批次。

## 15. 实施阶段

### 阶段一：可见与预检

- 增加账号安全 Tab。
- 增加设备读取和 2FA 状态读取。
- 增加账号资料状态读取，包括头像、昵称、简介和 `@username`。
- 增加批量预检和资料预览，不实际执行清理和设置。
- 风控中心展示“外部设备未清理”“未设置二步验证”风险。

验收：

- 在线账号能刷新设备列表。
- 账号列表能筛选“存在外部设备”“未设置二步验证”。
- 批量资料预览能展示 AI 随机生成的昵称、简介和多个 username 候选。
- 批量预检能清楚返回可执行、跳过、需等待、需人工处理。

### 阶段二：设备清理

- 实现单账号清理外部设备。
- 实现批量清理外部设备。
- 处理 24 小时限制、当前 Session 保护、部分成功。

验收：

- 不会退出平台当前 Session。
- 清理后会重新读取设备列表并更新快照。
- 失败账号有明确原因和可重试入口。

### 阶段三：二步验证设置

- 实现未设置 2FA 账号的设置流程。
- 支持系统生成每账号唯一密码。
- 支持邮箱确认状态流转。
- 增加密码托管、查看、导出权限和审计。

验收：

- 未设置 2FA 的账号可以批量设置。
- 已设置 2FA 的账号默认跳过，不误覆盖。
- 密码不在前端完整回显，查看和导出必须审计。

### 阶段四：资料批量初始化

- 实现批量设置昵称 / 姓名 / 简介。
- 实现批量设置 `@username`，支持自动跳号和候选名重试。
- 实现批量设置头像，支持素材池随机、顺序分配和只补空头像。
- 执行后同步远端资料并回写账号详情。

验收：

- 选择多个账号后能调用 AI 随机生成不同昵称、简介和 username 候选。
- `@username` 被占用时能自动尝试下一个候选。
- 已有资料默认不覆盖，勾选覆盖时必须二次确认并写审计。
- 头像设置失败不影响昵称和用户名成功结果。

### 阶段五：风控闭环

- 设备和 2FA 风险进入账号评分。
- 资料完整度、头像重复度和 username 设置结果进入风险提示。
- 高风险策略可阻塞未加固账号参与任务。
- 批量加固结果进入运营概览和处置队列。

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
