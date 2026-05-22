# TG 运营管理平台 PRD

> 基于 `docs/tg-ops-platform.md` 拆出的详细产品需求文档。
> 本文用于描述整体功能、页面按钮、业务流程、状态机、数据表、数据流转、执行器、规划器和验收口径。
> 当前日期口径：2026-05-21（Asia/Shanghai）。数据库字段与接口以当前代码为准，本文负责统一产品和研发理解。

---

## 1. 文档目标

### 1.1 目标

- 把 TG 运营管理平台从总纲设计落到可研发、可测试、可交付的 PRD。
- 明确每个页面的功能、按钮、弹窗、表格、状态和异常处理。
- 明确从账号接入、目标确认、规则配置、任务创建、执行、监听、归档、数据和审计的完整数据流。
- 明确核心数据表、状态字段和表间关系。
- 明确 Planner、Dispatcher、Listener、Recovery、Metrics 等执行器职责。
- 明确当前已实现、应继续收敛和后续增强的边界。

### 1.2 非目标

- 不替代数据库迁移文件和 ORM 模型。
- 不替代接口 schema 文档。
- 不继续沿用旧 Campaign、多租户 SaaS、卡密、订阅套餐作为产品主线。
- 不把 Telegram 官方限制包装成平台可绕过能力。

### 1.3 读者

| 角色 | 关注点 |
| --- | --- |
| 产品 / 交付 | 功能边界、页面流程、按钮和异常提示 |
| 前端 | 页面结构、弹窗、表格、权限和交互状态 |
| 后端 | API、数据表、状态机、执行链路和审计 |
| 测试 | 主流程、异常流、状态流转和回归范围 |
| 运维 | worker、队列、容量、指标、故障恢复 |

---

## 2. 产品总览

### 2.1 产品定位

TG 运营管理平台面向 Telegram 运营团队，用一个后台统一管理：

- TG 账号接入、资料初始化、二步密码托管和登录设备清理。
- 运营目标群、频道、讨论组和联系人。
- 消息发送、AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复。
- 规则、风控、素材、AI 配置、监听、执行、归档、数据和审计。

### 2.2 核心业务闭环

```text
系统初始化
  -> TG 账号接入
  -> 资料初始化 / 二步密码 / 登录设备清理
  -> 资产同步
  -> 任务内确认或创建运营目标
  -> 规则与风控配置
  -> 任务创建预检
  -> Planner 规划 Action
  -> Dispatcher 执行 Telegram 动作
  -> Listener 采集上下文和源事件
  -> Recovery 修复异常状态
  -> Metrics 生成运行快照
  -> 运营数据、归档、审计复盘
```

### 2.3 当前主导航

当前前端登录后导航为：

| 菜单 | 路由 | 权限 | 说明 |
| --- | --- | --- | --- |
| 运营概览 | `/dashboard` | `overview.view` | 今日任务、账号、目标、队列、风险和 24 小时趋势 |
| TG账号管理 | `/accounts` | `accounts.view` | 账号登录、同步、安全、资料、分组、代理和恢复 |
| 运营目标 | `/targets` | `targets.view` | 群、频道、讨论组、联系人等可运营对象 |
| 消息发送 | `/message-sending` | `message_sending.view` | 手动消息发送和批量发送 |
| 任务中心 | `/task-center` | `tasks.view` | 5 类持续运营任务 |
| 监听中心 | `/listeners` | `listeners.view` | 群/频道监听状态、账号和事件 |
| 规则中心 | `/rules` | `rules.view` | 规则集、规则版本、测试和命中 |
| 风控中心 | `/risk-control` | `risk.view` | 策略、评分、代理、处置队列 |
| 归档中心 | `/archives` | `archives.view` | 群消息、成员、上下文归档 |
| 运营数据 | `/usage-reports` | `usage.view` | 任务、账号、目标、AI 用量和失败统计 |
| 系统设置 | `/system-config` | `system.view` | TG 开发者应用、AI、素材、后台账号权限 |
| 审计记录 | `/audit` | `audits.view` | 操作审计、筛选和导出 |
| 操作手册 | `/manual` | `manual.view` | 管理员内置操作说明和最近更新 |

### 2.4 角色和权限

| 角色 | 默认能力 |
| --- | --- |
| 系统管理员 | 全部菜单、系统配置、权限、开发者应用、AI、风控和审计 |
| 运营管理员 | 账号同步、目标、消息、任务、监听、规则、风控、归档、数据和审计查看 |
| 账号添加专员 | 账号新增、登录、同步和基础状态检查 |
| 只读观察员 | 运营概览、运营数据、操作手册、审计查看 |

权限分为：

- 菜单权限：控制导航入口，例如 `accounts.view`。
- 按钮权限：控制动作入口，例如 `accounts.create`、`tasks.manage`、`rules.publish`。
- 后端写接口权限：前端隐藏按钮不能替代后端校验。
- 审计权限：敏感导出和危险动作必须留痕。

### 2.5 最近更新口径

前端操作手册必须展示近期已落地能力，避免运营人员只在研发文档里看到变更：

| 更新项 | 手册展示口径 |
| --- | --- |
| 账号安全加固 | 在 TG 账号管理中说明账号详情的账号安全页、刷新设备和 2FA 状态、清理外部设备、设置 2FA、最近安全批次结果 |
| 批量资料初始化 | 在 TG 账号管理中说明账号列表勾选、命名风格提示、一次 AI 批量生成或手工编辑昵称、username、简介、头像等资料预览，确认后按批次执行 |
| 任务内目标输入 | 在任务中心说明可选择已有目标，也可直接粘贴群聊 / 频道 `@username`、公开链接、邀请链接或 peer id |
| 账号-目标准入前置 | 在任务中心说明频道浏览、点赞、评论和 AI 活跃群启动前先检查账号是否已关注 / 已加入；未满足账号先按抖动节奏关注或加入，成功后才进入主互动 |
| 频道评论异常归因 | 在异常处理中说明频道帖子无法解析到讨论区、频道未绑定讨论组、账号无法进入讨论组评论时，归因为评论区不可用，建议改用浏览/点赞或先修复讨论组权限 |

### 2.6 2026-05-21 更新记录

- PRD 日期口径更新为 2026-05-21，并把今天的同步范围明确为主设计文档、前端操作手册、账号安全专项、频道任务执行异常和当前代码差异。
- 任务内目标输入继续作为新版任务创建主入口：运营人员创建任务时可以选择已有目标，也可以粘贴群聊 / 频道入口，由后端解析并自动创建或复用 `operation_targets`。目标输入只允许在创建任务时使用；编辑任务只能调整已有目标、账号范围、规则和执行参数，不能再通过编辑弹窗新建目标。
- 账号-目标准入前置统一为“先检查账号是否已关注 / 已加入；未满足账号先关注或加入；成功后才进入主互动”。已满足账号不等待未满足账号，准入成功账号追加进入后续主互动容量。
- 频道评论/回复补充异常归因：当 Telethon 返回 `GetDiscussionMessageRequest`、`DiscussionMessage` 或 “message ID used in the peer was invalid” 等讨论区解析错误时，后端应映射为 `COMMENT_UNAVAILABLE`，并提示确认消息 ID 属于频道帖子、频道已绑定讨论组、执行账号可进入讨论组并评论。
- 手机号不脱敏展示本轮覆盖所有涉及账号或联系人的运营链路：账号列表/检索、联系人、消息发送账号与私聊对象、归档成员与消息发送人、风控账号评分、账号安全批量预览、审计记录和导出日志均优先使用完整 `phone_number`；`phone_masked` 仅作为历史数据缺失完整手机号时的兼容兜底。
- 执行层文档口径保持“已有 planner / dispatcher / listener / recovery / metrics 分角色 drain 能力，但生产多进程拆分、并发配额、token 预留 / 退款、容量面板和压测结论仍需确认”。
- 系统设置和数据模型中的 `tenant_*` 表名仅代表当前代码表结构，不再作为多租户 SaaS、卡密或订阅套餐的产品主线表达。

---

## 3. 模块 PRD

## 3.1 运营概览

### 页面目标

让运营人员进入后台后先看到当前系统能不能运行、哪里异常、今天执行量和风险在哪里。

### 页面数据

- TG 账号总数、在线和待登录账号。
- 运营目标总数。
- 运行中任务数。
- 待执行 action 数。
- 失败任务 / 失败 action 数。
- 风险提醒数量。
- 24 小时运营趋势、互动拆分、成功率和失败率。
- 风险列表。

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 刷新当前数据 | 调用全局 refresh，重新拉取 overview、账号、任务、目标、规则等数据 |
| 风险行入口 | 跳转到对应账号、任务、风控或审计页面，后续可增强 |

### 数据来源

- `GET /api/overview`
- `tasks` / `actions`
- `tg_accounts`
- `operation_targets`
- `risk-control` 聚合服务

---

## 3.2 系统设置

### 页面目标

维护平台运行基础配置，不承载任务节奏和风控策略。

### 功能块

| 功能块 | 主要对象 | 主要按钮 |
| --- | --- | --- |
| Telegram 开发者应用 | `telegram_developer_apps` | 新增应用、详情、编辑、检查、启用、禁用 |
| AI 供应商 | `ai_providers` | 新增供应商、编辑、检查、启用、禁用 |
| 平台 AI 配置 | `tenant_ai_settings` | 编辑当前平台实例的 AI 配置；表名保留代码兼容，不表达多租户 SaaS 主线 |
| 提示词模板 | `prompt_templates` | 新增提示词、编辑 |
| 素材库 | `materials`、`material_asset_versions`、`material_tg_ref_versions` | 新增素材、上传、批量上传、编辑 |
| 后台账号权限 | `app_users` | 新增用户、编辑、重置密码、调整额度、配置菜单/按钮权限 |
| 平台额度 | `tenants` | 编辑账号额度、任务额度、通知配置；底层表名保留当前实现 |

### 关键规则

- 新增 TG 账号前必须至少有一个健康且可分配的开发者应用。
- AI 生成资料、AI 活跃群、AI 评论和 AI 改写都依赖健康 AI 供应商。
- 素材被规则版本、任务或发送记录引用后不能物理删除。
- 权限配置修改后必须更新 `permission_version` 并写审计。

---

## 3.3 TG账号管理

### 页面目标

完成 TG 账号接入、登录恢复、资产同步、资料初始化、二步密码托管、登录设备清理、账号分组、代理绑定和可用性判断。

### 页面结构

- 账号分组筛选。
- 搜索框。
- 批量操作条。
- 账号风险摘要卡片。
- 账号表格。
- 账号详情弹窗。
- 批量资料初始化抽屉。
- 批量设置二步密码抽屉。
- 批量清理登录设备抽屉。

### 账号表格核心列

| 列 | 内容 |
| --- | --- |
| 账号 | 头像、平台展示名、username、手机号、账号分组、TG 昵称 |
| 状态 | 在线、待登录、等待验证码、等待扫码、等待2FA、需重新登录、受限、异常、禁用 |
| 开发者应用 | 应用名称、健康状态、凭证版本 |
| 代理 | 代理名称、代理状态、告警状态 |
| 安全/资料 | 安全待刷新、资料完整/待初始化、username 状态 |
| 操作 | 详情、提取验证码、移动分组、检查、同步、完成登录、移除 |

手机号不脱敏展示已作为本轮实现项进入全部账号关联链路。当前代码仍保留 `phone_masked` 兼容字段，用于历史数据缺失完整手机号时兜底；新接口和前端展示应统一优先使用完整 `phone_number`，联系人、归档成员、审计记录和导出日志的搜索 / 展示也必须覆盖完整手机号。

### 批量按钮

| 按钮 | 启用条件 | 行为 |
| --- | --- | --- |
| 资料初始化 | 已选择账号且有同步权限 | 打开批量资料初始化抽屉 |
| 设置二步密码 | 已选择账号且有同步权限 | 打开批量设置二步密码抽屉，只包含 `set_two_fa` |
| 清理登录设备 | 已选择账号且有同步权限 | 打开批量清理登录设备抽屉，只包含 `cleanup_devices` |
| 刷新安全状态 | 已选择账号且有同步权限 | 逐账号调用安全刷新 |
| 清空选择 | 已选择账号 | 清空勾选 |

### 单行按钮

| 状态 | 按钮 |
| --- | --- |
| 待登录 / 等待验证码 / 等待扫码 / 等待2FA / 需重新登录 / 异常 | 完成登录 / 继续登录、移除 |
| 在线 | 详情、提取验证码、移动分组、检查、同步 |
| 其他可查看状态 | 详情、移动分组 |

### 账号登录流程

```text
新增账号
  -> 选择或自动分配开发者应用
  -> 输入手机号
  -> 选择验证码登录或二维码登录
  -> 等待 Telegram 返回 code / QR
  -> 输入验证码或扫码
  -> 如需 2FA，输入二步密码
  -> 写入 session_ciphertext
  -> 账号状态变为 在线
  -> 同步群、频道、联系人、资料
```

### 资料初始化流程

```text
选择多个账号
  -> 点击 资料初始化
  -> 配置生成方式、语言、画像、禁用词、custom_prompt、头像策略
  -> 预检 / AI 生成预览
  -> 不触发实时登录设备扫描，只读取已有安全快照和账号在线状态
  -> 一次 AI 请求生成整批昵称、简介和 username 候选
  -> AI 超时、mock、无健康供应商或返回不足时使用本地随机网名兜底并展示 warning
  -> 勾选头像但没有可用头像来源时只跳过头像，不阻塞昵称、简介和 username
  -> 预览表单行可编辑
  -> 逐行确认昵称、username、简介、头像策略、生成来源和 warning
  -> 输入 确认加固
  -> 创建批次
  -> worker 按账号执行 profile -> username -> avatar
  -> 回写账号资料、批次项和审计
```

#### 资料初始化命名口径

- 默认昵称是自然、随机、生活化的 TG 网名，不是正式姓名。
- 默认提示词示例包括“锅巴洋芋、蕉太狼、早睡失败、小熊便利店、不吃香菜、月亮打烊”。
- 前端必须提供“命名风格提示”输入框，对应 `profile_strategy.custom_prompt`。
- `display_name` 和 `first_name` 可以直接使用同一个网名；`last_name` 可以为空，不强行拆成中文姓氏和名字。
- `username_candidates` 必须遵守 Telegram username 规则，只能包含英文、数字和下划线，不能包含中文。
- 批量预览必须一次请求 AI 生成整批账号资料，不能按账号逐个请求模型。
- 资料预览超时时间按账号数量伸缩，避免 50 个账号以上批量生成时被普通接口超时提前打断。
- 预览行必须展示生成来源和异常原因，例如 AI 成功、AI 超时、本地兜底、头像来源缺失、username 不可用或账号离线。
- 确认执行后必须能在批次详情追踪每个账号的 profile、username、avatar 动作结果。
- 后续需要继续完善批次进度、失败重试、头像素材池、username 冲突处理和操作手册说明。

### 设置二步密码流程

```text
选择多个账号
  -> 点击 设置二步密码
  -> 抽屉动作范围只显示 设置二步验证
  -> 不展示 AI 命名、头像、username 或资料覆盖配置
  -> 预检账号在线、session 和已有 2FA 状态
  -> 已设置二步验证的账号标记跳过或 warning
  -> 输入 确认加固
  -> 创建批次
  -> worker 为未设置账号生成并托管二步密码
  -> 写入安全快照、批次项、失败原因和审计
```

### 清理登录设备流程

```text
选择多个账号
  -> 点击 清理登录设备
  -> 抽屉动作范围只显示 清理外部设备
  -> 不展示 AI 命名、头像、username 或二步密码配置
  -> 预检账号在线、session、平台可信设备和新登录 24 小时限制
  -> 保留当前平台 Session，只清理非平台可信登录设备
  -> 输入 确认加固
  -> 创建批次
  -> worker 逐账号清理外部登录设备
  -> 写入安全快照、批次项、失败原因和审计
```

### 动作边界

| 入口 | 允许动作 | 禁止混入 |
| --- | --- | --- |
| 资料初始化 | `update_profile`、`update_username`、`update_avatar` | `set_two_fa`、`cleanup_devices` |
| 设置二步密码 | `set_two_fa` | 资料、头像、username、设备清理 |
| 清理登录设备 | `cleanup_devices` | 资料、头像、username、二步密码 |

### 账号状态

| 状态 | 含义 | 任务可用性 |
| --- | --- | --- |
| 在线 | session 可用，最近连接正常 | 可用 |
| 待登录 | 账号创建但未开始登录 | 不可用 |
| 等待验证码 | 已发起验证码登录 | 不可用 |
| 等待扫码 | 已发起二维码登录 | 不可用 |
| 等待2FA | 需要输入 Telegram 二步密码 | 不可用 |
| 需重新登录 | session 失效或凭证不可用 | 不可用 |
| 受限 / 疑似封禁 / 已封禁 | Telegram 限制发送或互动 | 不可用或只读 |
| 异常 | 代理、权限、同步或登录异常 | 需风控判断 |
| 禁用 | 人工移除或停用 | 不可用 |

---

## 3.4 运营目标

### 页面目标

把账号同步和任务内输入沉淀得到的群、频道、讨论组、联系人整理成可运营对象，作为消息发送、任务执行、数据复盘和审计归因的底层业务对象。运营目标页不是创建任务的必经入口，也不再提供“新建群聊 / 新建频道目标”作为任务准备流程。

### 目标类型

| 类型 | 能力 |
| --- | --- |
| 群 | 发送、监听、AI 活跃、转发源、转发目标、归档 |
| 频道 | 浏览、点赞、评论、回复、监听新消息、归档 |
| 讨论组 | 频道评论/回复承载、上下文采集 |
| 联系人 / 私聊 | 消息发送，默认不进入持续任务 |

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 同步全部目标 | 从在线账号同步群、频道、联系人到 `operation_targets` |
| 目标详情 | 查看授权、关联账号、最近消息、任务和能力 |
| 查看准入状态 | 查看任务触发的关注 / 加入结果、失败原因和可复用账号关系 |
| 处理准入失败账号 | 后续在运营目标页统一处理重新准入 / 重试，不在任务详情页直接操作 |
| 同步消息 | 对频道目标同步频道消息 |
| 创建任务 | 带目标预填进入任务中心 |
| 消息发送 | 带目标预填进入消息发送 |
| 授权 / 能力调整 | 修改目标可发送、可监听、可归档等能力 |

运营目标页不提供新建群聊或新建频道目标按钮。运营人员需要对新群聊 / 新频道创建任务时，应在任务创建目标步骤粘贴入口；系统在后台自动创建或复用 `operation_targets`。

### 任务内目标录入规则

| 输入 | 处理 |
| --- | --- |
| `@channel_name` | 去掉 `@` 后保存 username |
| `https://t.me/channel_name` | 规范为 username |
| `https://t.me/+...` / `joinchat` | 保留邀请链接，用于前置关注 |
| peer id | 用于识别已同步目标，通常不能单独完成主动加入 |

### 目标管理与任务内创建关系

```text
同步目标 / 任务内粘贴目标
  -> 保存 operation_targets
  -> 目标详情展示历史账号-目标关系
  -> 任务中心使用或复用该目标
```

运营目标不要求一开始就有账号已关注或已加入，也不要求运营人员在运营目标页先创建群聊或频道。准入动作主要由任务创建或任务启动触发；任务详情先只展示准入状态和失败原因，重新准入 / 重试后续统一在运营目标页处理。任务创建不应只允许选择已关注账号，而是允许选择账号范围，并把账号拆成三类：

| 分类 | 含义 | 任务处理 |
| --- | --- | --- |
| 已满足 | 频道任务已关注；群聊监听源已加入；AI 活跃群和转发目标群已加入且可发言；账号状态可用 | 可直接进入主互动 |
| 可准备 | 未关注 / 未加入，但目标有 `@username`、公开链接或邀请链接可加入 | 先生成关注 / 加入前置动作，成功后进入主互动 |
| 不可准备 | 账号受限、离线、缺少加入入口、peer id 不能主动加入或 TG 返回限制 | 不进入主互动，展示失败原因 |

目标详情必须展示由任务或同步沉淀出来的账号-目标关系：

- 已满足本任务准入账号数：频道已关注、转发源群已加入 / 可读取、AI 活跃群和转发目标群已加入且可发言。
- 未关注 / 未加入但可准备账号数。
- 准备中账号数。
- 失败账号数和失败原因。
- 最近一次准备批次状态。
- 失败账号的后续处理状态；重新准入 / 重试入口沉到运营目标页，不放在任务详情页。

---

## 3.4.1 任务内目标输入

任务中心是新目标进入系统的主入口。创建任务时，目标步骤必须支持：

| 输入方式 | 行为 |
| --- | --- |
| 选择已有目标 | 使用已有 `operation_targets.id` |
| 粘贴 `@username` | 后端解析为群聊或频道 username，并自动创建或复用运营目标 |
| 粘贴公开链接 | 后端规范化链接，自动创建或复用运营目标 |
| 粘贴 `https://t.me/+...` / `joinchat` | 保存邀请链接，用于关注频道或加入群聊前置 |
| 输入 peer id | 仅用于识别已同步目标；没有加入入口时不能承诺自动加入 |

任务创建接口必须在后端事务中完成 target upsert，前端不能被要求先跳到运营目标页创建目标。

任务编辑边界：

- 创建任务时允许 `target_type + target_input + target_title` 自动创建或复用目标。
- 编辑任务时不允许输入新目标入口，也不允许通过编辑接口 upsert 新目标。
- 编辑任务可继续切换为已有运营目标，或调整账号范围、规则、节奏、结束时间和失败策略。
- 如果运营人员要对新群聊 / 新频道发起任务，必须从“创建任务”重新进入目标输入流程。

## 3.5 消息发送

### 页面目标

支持面向单个或多个目标创建人工消息发送任务，适合运营人员明确发一批消息的场景。

### 功能点

- 选择发送账号或账号范围。
- 选择目标：运营目标、账号联系人、手动对象。
- 输入文本，选择素材。
- 发送前预检查：账号可用性、目标能力、风控限制、规则命中。
- 创建发送任务或批量发送任务。
- 查看任务状态并重试、取消或派发。

### 数据流

```text
前端选择账号和目标
  -> POST /api/risk-control/preflight
  -> 可用账号 / 受限账号 / 阻塞账号
  -> POST /api/message-send-tasks 或 /api/message-send-tasks/batch
  -> message_tasks
  -> message_task_attempts
  -> Telegram Gateway
  -> 回写状态、失败原因和 remote_message_id
```

---

## 3.6 任务中心

### 页面目标

创建和运行持续运营任务，把业务意图拆成可追踪的 Task 和 Action。

### 任务类型

| 类型 | `tasks.type` | 说明 |
| --- | --- | --- |
| AI 活跃群 | `group_ai_chat` | 在授权群中按上下文和账号画像生成多账号自然对话 |
| 转发监听群 | `group_relay` | 监听源群消息，经规则过滤、转换、路由后转发到目标群 |
| 频道浏览 | `channel_view` | 给频道消息安排浏览动作 |
| 频道点赞 | `channel_like` | 给频道消息安排 reaction 动作 |
| 频道评论/回复 | `channel_comment` | 在频道讨论区评论或回复指定评论 |

### 任务列表

| 区域 | 内容 |
| --- | --- |
| 统计卡片 | 任务总数、执行中、失败任务 |
| 工具条 | 搜索、刷新 |
| 主按钮 | 创建任务 |
| 表格 | 任务名称、类型、状态、目标、账号范围、成功/失败、下次运行、操作 |

### 任务操作按钮

| 按钮 | 状态要求 | 行为 |
| --- | --- | --- |
| 详情 | 任意非删除任务 | 打开任务详情弹窗 |
| 编辑 | 非删除任务 | 打开编辑任务弹窗 |
| 启动 | draft / paused / failed / stopped | 切到 running，设置 next_run_at |
| 暂停 | running | 暂停规划和 dispatch |
| 继续 | paused | 恢复 running |
| 停止 | running / paused | 停止任务，不再规划新 action |
| 重试 | failed / partial | 按失败策略重新排队 |
| 重置 | 已有执行数据 | 清理运行统计并重新规划 |
| 删除 | 任意非删除任务 | 软删除，写 deleted_at、deleted_by、delete_reason |

### 任务详情

任务详情弹窗必须在主执行明细前展示“准入前置”区域。它用于说明本任务内关注频道 / 加入群聊的子任务情况，避免运营人员误以为任务卡住或必须先去运营目标页处理。

准入前置展示字段：

| 字段 | 说明 |
| --- | --- |
| 子任务类型 | `target_membership`，覆盖频道关注和群聊加入 |
| 子任务状态 | `not_required`、`pending`、`running`、`partial_success`、`blocked`、`completed`、`failed` |
| 目标 | 当前任务解析出的频道 / 群聊目标、入口类型、是否复用运营目标 |
| 容量统计 | 已满足、待准备、准备中、成功、失败、不可准备 |
| 预计进度 | 基于准入 action 总数、已完成数、批次间隔、退避等待和 FloodWait 估算 |
| 预计完成 | 展示预计剩余时间或预计完成时间；无法估算时展示原因 |
| 当前阶段 | 排队中、加入 / 关注中、等待 FloodWait、等待 AI 回答验证、等待人工处理 |
| 账号明细 | 账号、状态、挑战问题、AI 答案、是否可发言和失败原因；任务详情只展示，不在此处处理重试 |

执行语义：

- 准入前置是任务的可见子任务，不是任务级全局串行锁。
- 已关注频道或已加入群聊的账号必须先进入主互动 action，不等待其他账号加入 / 关注。
- 未关注 / 未加入账号在准入子任务中按抖动、限速和风控执行，成功后追加进入后续主互动容量。
- 部分账号准入失败只影响该账号，不影响已满足账号和准入成功账号继续执行。
- 只有 0 个账号满足准入且 0 个账号准入成功时，主互动才保持阻塞或失败。

任务列表展示简化摘要，例如“主任务执行中，准入 3/10，预计 8 分钟补齐”；任务详情展示完整进度、账号级结果和失败原因。

### 创建任务向导

当前创建弹窗为 5 步：

| 步骤 | 页面 | 字段 |
| --- | --- | --- |
| 1 | 基础信息 | 任务类型、任务名称、结束时间 |
| 2 | 目标选择 | 群目标、源群、目标群、频道、消息范围、指定消息 |
| 3 | 类型参数 | 规则版本、AI 黑话、内容处理方式、频道动作量、评论方向 |
| 4 | 账号与节奏 | 账号选择、24 小时活跃曲线、高级覆盖 |
| 5 | 预检确认 | 账号摘要、目标能力、预计动作量、规则版本、风险、阻塞项 |

底部按钮：

| 按钮 | 行为 |
| --- | --- |
| 取消 | 关闭创建弹窗 |
| 上一步 | 回到上一向导步骤 |
| 下一步 | 校验当前步骤并进入下一步 |
| 保存草稿 | 创建 `draft` 任务 |
| 创建并启动 | 创建后立即进入 `running` |

### 账号选择

| 模式 | 字段 |
| --- | --- |
| 全部账号 | `selection_mode=all` |
| 账号分组 | `selection_mode=group`、`account_group_id` |
| 手动选择 | `selection_mode=manual`、`account_ids` |

### 24 小时活跃曲线

曲线用于推导每小时动作强度：

```text
小时计划量 =
  任务默认基线
  * 曲线强度
  * 可用账号系数
  * 风控系数
  * 目标能力系数
```

曲线不是装饰 UI。Planner 需要按任务类型解释曲线：

| 任务类型 | 曲线含义 |
| --- | --- |
| AI 活跃群 | 每小时发言量 |
| 转发监听群 | 目标群发送量 |
| 频道浏览 | 浏览量 |
| 频道点赞 | 点赞量 |
| 频道评论/回复 | 评论/回复量 |

### 目标输入

任务创建向导的目标步骤必须支持：

- 已有运营目标下拉选择。
- 新目标输入框：`target_type`、`target_input`、`target_title`。
- 当使用新目标输入时，预检必须展示解析结果：新建目标、复用目标、无法解析或缺少加入入口。

### 创建前预检

预检必须返回：

- `decision`: allow / warn / block。
- `target_resolution`: 目标解析、创建或复用结果。
- 候选账号数、可用账号数、受限账号数、阻塞账号数。
- 已满足账号数、可准备账号数、不可准备账号数。
- 目标能力。
- 预计 action 数。
- 预计关注 / 加入前置动作数。
- `membership_subtask_preview`: 准入子任务预览，包含预计进度、预计耗时、预计完成时间、容量统计和 warning。
- 容量缺口。
- 规则版本。
- 风控命中。
- 阻塞项和警告。

### 账号-目标准入前置

频道浏览、点赞、评论、回复、AI 活跃群、转发监听群和转发目标群启动前必须先检查账号对目标的准入状态：

- 频道任务中已关注频道的账号标记 `ready`；转发监听源群只要求账号已加入 / 可读取；AI 活跃群和转发目标群必须要求账号已加入且可发言，只有 `can_send=True` 才能进入主互动 action。
- 未关注 / 未加入但有加入入口的账号生成统一准入前置 action。
- 统一准入 action 命名为 `ensure_target_membership`，覆盖频道关注和群聊加入；历史 `ensure_channel_membership` 必须继续兼容展示和执行。
- 准入 action 按抖动、限速、FloodWait 和风控节奏执行。
- 主互动只使用准入成功或原本已满足账号；原本已满足账号不等待未满足账号完成准入。已有账号“已加入但不可发言”不能作为 AI 活跃群或转发目标群 ready，需要重新进入准入流程直到达到可发言状态。
- 准入成功账号需要追加进入后续主互动容量，不能只记录成功但不参与任务。
- 0 个账号准入成功时，主互动不规划。

入群验证处理：

- 文本问题、简单算数题、固定问答由 AI 自动尝试回答。
- 每个账号对同一目标最多自动尝试一次。
- AI 无法判断、图片验证码、人工审批或 TG 拒绝时，标记 `manual_required`、`challenge_failed` 或 `failed`。
- 验证问题、AI 答案、结果和失败原因写入 action result 和任务 stats。

### 频道评论/回复异常归因

频道评论/回复依赖频道帖子、绑定讨论组和账号讨论组权限三个条件同时成立。执行前和执行失败归因必须按下面口径展示：

| 场景 | 归因 | 前端/手册提示 |
| --- | --- | --- |
| 消息 ID 不是频道帖子，或无法通过频道帖子找到讨论区消息 | `COMMENT_UNAVAILABLE` | 请确认消息 ID 属于频道帖子 |
| 频道未绑定讨论组，或讨论组不可见 | `COMMENT_UNAVAILABLE` | 请先确认频道已绑定讨论组 |
| 执行账号未加入讨论组、无评论权限或被 TG 限制 | `COMMENT_UNAVAILABLE` 或账号受限原因 | 请确认执行账号可进入讨论组并评论 |
| 目标实体无法解析 | `PEER_INVALID` | 请重新同步账号群聊/运营目标后再试 |

后端异常映射需要覆盖 Telethon 讨论区解析错误，例如 `GetDiscussionMessageRequest`、`DiscussionMessage` 和 “message ID used in the peer was invalid”。这类错误不能泛化为未知失败，也不能自动重试刷量；应写入 action result、任务详情、运营数据和风控/账号建议。

### Task 状态

| 状态 | 含义 | 可用操作 |
| --- | --- | --- |
| draft | 草稿 | 启动、编辑、删除 |
| running | 运行中 | 暂停、停止、编辑、详情 |
| paused | 已暂停 | 继续、停止、编辑、删除 |
| stopped | 已停止 | 启动、删除 |
| failed | 失败 | 重试、编辑、删除 |
| completed | 已完成 | 详情、重置、删除 |

### Action 状态

| 状态 | 含义 | 自动重试 |
| --- | --- | --- |
| pending | 等待领取 | 是 |
| claiming | 已被 worker 预领取，尚未拿齐运行资源 | claim 超时后恢复 |
| executing | 已领取并执行中 | 否 |
| success | 成功 | 否 |
| failed | 明确失败 | 按失败策略 |
| skipped | 策略跳过 | 否 |
| unknown_after_send | 已进入 TG 调用边界但本地结果未知 | 否，需人工或补偿确认 |

---

## 3.7 监听中心

### 页面目标

查看群、频道、讨论组的监听状态，确认源事件和上下文是否正常流转。

### 页面内容

- 监听对象：群、频道、讨论组。
- 监听账号列表。
- 关联任务。
- 事件积压。
- 最近事件。
- 最近错误。
- 备用账号。

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 刷新 | 重新拉取监听汇总 |
| 切换监听账号 | 重新分配指定来源的监听账号，写审计 |
| 展开详情 | 查看监听账号、关联任务和最近事件 |

### Listener 数据流

```text
Listener claim source
  -> 读取 listener_source_state
  -> 使用监听账号拉取 TG 消息 / 评论 / 事件
  -> 按唯一键去重
  -> 写 group_context_messages / channel_messages / source_media_assets
  -> 更新水位 last_remote_message_id / last_event_at
  -> 唤醒依赖事件的任务
```

---

## 3.8 规则中心

### 页面目标

维护系统级规则集、规则版本、过滤、转换、路由、账号策略、限速、重试和规则测试。

### 核心概念

| 概念 | 说明 |
| --- | --- |
| 规则集 | 一组规则配置容器 |
| 规则版本 | 可发布、可回滚、可绑定任务的不可变版本 |
| 活动版本 | 当前默认生效版本 |
| 草稿版本 | 可编辑，不允许运行任务绑定 |
| 发布版本 | 任务可绑定 |
| 归档版本 | 历史留存，可复制或回滚 |

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 新建规则集 | 创建规则集和默认草稿 |
| 编辑规则配置 | 打开规则配置弹窗 |
| 保存并发布新版本 | 保存配置并生成发布版本 |
| 版本记录 | 查看版本列表 |
| 发布 | 把草稿发布成当前活动版本 |
| 复制 | 复制历史版本为草稿 |
| 回滚 | 从归档版本生成新发布版本 |
| 规则测试 | 输入样本文本，验证过滤、转换、路由和输出校验 |
| 加入不转发名单 | 从转发执行项把来源人加入当前任务的来源过滤配置 |

### 规则执行点

- AI 活跃群：输入上下文过滤、候选回复输出校验。
- 转发监听群：源事件过滤、内容转换、目标路由、账号策略、限速。
- 频道评论：AI 评论输出校验。
- 素材选择：规则版本固化素材策略，执行项再固化具体素材 ID 和资产版本。

AI 活跃群还需要额外经过质量规则：

- 语义去重：将“照片准 / 没照骗 / 真人没差”“态度稳 / 不催 / 不敷衍”“位置提前发 / 没绕路”“结束回访 / 下次安排”等近义表达识别为同一语义簇，同一轮和近 N 轮内限频。
- 幻觉拦截：候选内容提到具体经历、服务动作、价格、地址、穿着、到场时间、回访等事实时，必须能追溯到上下文、素材或运营配置；无法追溯时改写或丢弃。
- 上下文锚定：真人聊天正在围绕某个人名、@ 对象、价格、榜单、评价或问题展开时，AI 必须优先接当前对象，不能跳回泛化模板。
- 模板感拦截：连续出现“上次那个”“这点加分”“挺省心”等固定壳句时，按重复风险处理。

---

## 3.9 风控中心

### 页面目标

统一管理账号、代理、目标、内容、限流、冷却、处置队列和策略。

### 页面 Tab

| Tab | 内容 |
| --- | --- |
| 总览 | 当前风控等级、静默状态、处置队列 |
| 全局策略 | 抖动、批次间隔、静默时间、重试策略、账号小时/日上限 |
| 账号评分 | 账号健康、风险事件、发送容量 |
| 代理资源 | 代理新增、编辑、检查、禁用、绑定账号数 |
| 代理告警 | acknowledge、ignore、resolve |
| 命中记录 | 风控事件和命中原因 |

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 刷新 | 拉取最新风控汇总 |
| 编辑全局策略 | 打开策略编辑弹窗 |
| 新增代理 | 创建本地代理资源 |
| 编辑代理 | 修改代理名称、协议、端口、容量 |
| 检查代理 | 执行端口和 Telegram 连接探测 |
| 禁用代理 | 禁止该代理继续被调度 |
| 绑定代理 | 给账号绑定代理 |
| 批量绑定代理 | 多账号批量绑定代理 |
| 确认告警 | 标记已知晓 |
| 忽略告警 | 一段时间内不再提示 |
| 解决告警 | 标记恢复并写审计 |

### 风控进入任务链路

```text
任务创建预检
  -> 检查账号状态、目标能力、代理、小时/日上限、冷却、规则
  -> 预检提示 allow / warn / block
  -> Planner 规划时再次检查
  -> Dispatcher claim 时最终检查 token bucket、in-flight、代理、目标能力
  -> 执行失败写 risk event
  -> 风控中心生成处置项
```

---

## 3.10 素材库与 AI 内容

### 页面目标

统一维护 AI 供应商、提示词、黑话配置、素材和缓存状态。

### 功能点

- AI Provider 配置和健康检查。
- 平台 AI 默认供应商、模型、温度、token 和回退策略。
- Prompt Template 管理。
- AI 黑话配置作为任务创建中的选择项。
- AI 活跃群质量配置：接话优先级、空闲暖场间隔、事实锚点要求、语义重复窗口、低置信沉默策略。
- Material 上传、编辑、批量上传和缓存健康查看。
- 图片、表情包、自定义 emoji、组合消息等素材能力。

### 素材执行原则

- 平台不把图片原文件当作永久业务主数据，重点保存素材元数据、资产版本、缓存引用和可重发状态。
- 发送时采用 `download_reupload`，由发送账号重新上传。
- 规则版本绑定素材策略，执行项固化本次使用素材和资产版本。

---

## 3.11 归档、运营数据和审计

### 归档中心

| 功能 | 按钮 |
| --- | --- |
| 创建归档 | 选择群/目标，创建归档任务 |
| 查看详情 | 查看消息、成员、上下文 |
| 重新归档 | 对失败或过期归档重新运行 |
| 导出 | 导出 JSON / CSV 等格式 |

### 运营数据

统计维度：

- 任务维度。
- 账号维度。
- 目标维度。
- 规则维度。
- 素材维度。
- AI 用量维度。
- 失败类型维度。

### 审计记录

审计对象：

- 登录、退出。
- 账号新增、登录、移除、同步。
- 资料初始化、设置二步密码、清理登录设备、刷新安全状态。
- 开发者应用、AI、素材、规则、风控策略修改。
- 任务创建、启动、暂停、停止、重试、删除。
- 导出和敏感查看。

---

## 3.12 操作手册

### 页面目标

给平台管理员、运营主管和运营人员提供登录后可直接查看的操作说明，覆盖日常操作顺序、上线前检查、任务类型选择、最近更新功能、按菜单操作、异常处理、权限和审计要求。

### 页面结构

| 区块 | 内容 |
| --- | --- |
| 日常操作顺序 | 系统基础配置、接入 TG 账号、确认运营目标、配置规则与风控、创建并追踪任务 |
| 上线前检查 | TG 开发者应用、AI 服务、账号登录、同步资产、账号安全状态、运营目标、规则版本、风控异常 |
| 任务类型选择 | AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复的适用说明 |
| 最近更新功能 | 账号安全加固、批量资料初始化、任务内目标输入、账号-目标准入前置、手机号不脱敏展示 |
| 按菜单操作 | TG 账号管理、运营目标、消息发送、任务中心、监听中心、规则中心、风控中心、归档中心、运营数据与审计 |
| 异常处理速查 | 登录或账号不可用、任务预检查失败、监听无事件、内容被规则拦截、执行结果复盘、账号未满足目标准入 |
| 权限与审计要求 | 菜单和按钮权限、敏感操作审计、自动执行前置确认 |

### 最近更新功能说明

| 功能 | 前端展示要求 |
| --- | --- |
| 账号安全加固 | 展示账号详情的账号安全入口，可刷新设备和 2FA 状态、清理外部设备、设置 2FA，并查看最近安全批次结果 |
| 批量资料初始化 | 展示账号列表批量入口、命名风格提示和 AI / 本地兜底预览，可生成或手工编辑昵称、username、简介、头像等资料，展示生成来源和 warning，确认后创建批次执行 |
| 任务内目标输入 | 展示创建任务时可选择已有目标，也可粘贴群聊 / 频道 `@username`、公开链接、邀请链接或 peer id；编辑任务不展示新目标输入 |
| 账号-目标准入前置 | 展示频道任务会检查是否已关注；转发源群检查是否已加入 / 可读取；AI 活跃群和转发目标群检查是否可发言；未满足账号先按抖动节奏关注、加入或重新取得可发言能力，任务详情只展示状态和失败原因，失败账号不进入主互动 |
| 手机号展示 | 所有涉及账号或联系人的列表、联系人、消息发送、归档、风控、账号安全、审计和导出日志优先展示完整手机号；当前 `phone_masked` 仍是历史兼容兜底字段 |

### 完成标准

- 操作手册使用用户可理解的菜单和按钮口径，不展示内部表名、worker 细节或工程调试词。
- 最近更新功能必须和主设计文档、专项设计文档、任务中心和账号中心页面行为一致。
- 任务相关说明必须明确“先检查准入状态”：未关注 / 未加入账号先关注或加入；AI 活跃群和转发目标群还必须具备可发言能力，成功后才互动，且 0 个账号准入成功则不进入主互动。
- 资料初始化、设置二步密码、清理登录设备必须保持独立入口说明，不能重新合并成笼统的安全加固动作。
- 手机号展示说明必须和代码兼容字段分开：PRD 目标是不脱敏，所有涉及账号或联系人的前端展示、搜索和导出优先使用完整 `phone_number`，`phone_masked` 只表示历史兼容兜底。

---

## 4. 数据模型 PRD

### 4.1 核心表分组

| 分组 | 表 |
| --- | --- |
| 平台实例与后台用户 | `tenants`、`app_users`、`user_token_ledgers` |
| 账号与登录 | `telegram_developer_apps`、`tg_accounts`、`tg_login_flows`、`tg_verification_codes` |
| 账号同步资产 | `tg_groups`、`tg_group_accounts`、`tg_contacts`、`tg_account_sync_records`、`tg_account_profile_sync_records` |
| 账号安全 | `tg_account_security_snapshots`、`tg_account_authorization_snapshots`、`tg_account_security_batches`、`tg_account_security_batch_items`、`tg_account_profile_batch_rules` |
| 运营目标 | `operation_targets`、`channel_messages`、`channel_message_comments` |
| 新版任务中心 | `tasks`、`actions`、`execution_attempts` |
| 监听与运行 | `listener_source_state`、`worker_heartbeats`、`runtime_metric_snapshots`、`daily_runtime_stats`、`runtime_cleanup_audits` |
| 规则 | `rule_sets`、`rule_set_versions` |
| 风控与代理 | `account_proxies`、`account_proxy_bindings`、`proxy_alerts`、`proxy_health_checks` |
| AI 与素材 | `ai_providers`、`tenant_ai_settings`、`prompt_templates`、`ai_usage_ledgers`、`materials`、`material_asset_versions`、`material_tg_ref_versions` |
| 转发和媒体缓存 | `message_fingerprints`、`source_media_assets` |
| 手动发送与旧兼容 | `message_tasks`、`message_task_attempts`、`operation_tasks`、`operation_task_attempts`、`manual_operation_records`、`campaigns`、`ai_drafts` |
| 归档与审计 | `group_archives`、`archived_messages`、`archived_members`、`audit_logs` |

### 4.2 关键表说明

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `tg_accounts` | `tenant_id`、`display_name`、`username`、`status`、`session_ciphertext`、`developer_app_id`、`proxy_id`、`health_score`、`deleted_at` | TG 账号主表 |
| `account_pools` | `tenant_id`、`name`、`is_default` | 账号分组 |
| `operation_targets` | `target_type`、`tg_peer_id`、`title`、`username`、`can_send`、`auth_status` | 用户侧运营目标 |
| `tg_groups` | `tg_peer_id`、`group_type`、`auth_status`、`can_send` | 账号同步得到的群/频道资产 |
| `tg_group_accounts` | `group_id`、`account_id`、`can_send`、`is_listener` | 账号和群/频道能力关系 |
| `tasks` | `type`、`status`、`account_config`、`pacing_config`、`failure_policy`、`type_config`、`stats` | 新版任务中心任务 |
| `actions` | `task_id`、`action_type`、`account_id`、`status`、`claim_*`、`lease_*`、`payload`、`result`、`action_dedupe_key` | 可执行动作 |
| `execution_attempts` | `action_id`、`worker_id`、`attempt_no`、`status`、`gateway_call_started_at`、`result_snapshot` | 执行尝试和结果未知判断 |
| `listener_source_state` | `source_type`、`source_peer_id`、`account_id`、`lease_owner`、`last_remote_message_id` | 监听水位 |
| `rule_sets` | `name`、`status`、`task_types`、`active_version_id` | 规则集 |
| `rule_set_versions` | `version`、`status`、`filters`、`transforms`、`routing`、`account_strategy`、`rate_limits` | 规则版本 |
| `account_proxies` | `protocol`、`host`、`port`、`status`、`alert_status`、`max_bound_accounts` | 本地代理资源 |
| `tg_account_security_batches` | `action_types`、`status`、`profile_strategy`、`avatar_strategy`、`trace_id` | 安全/资料批次 |
| `tg_account_security_batch_items` | `batch_id`、`account_id`、`status`、`profile_status`、`username_status`、`failure_detail` | 单账号批次项 |
| `source_media_assets` | `source_peer_id`、`source_message_id`、`cache_status`、`cache_version`、`cache_message_id` | 转发源媒体临时缓存 |
| `worker_heartbeats` | `worker_id`、`process_type`、`status`、`last_seen_at`、`heartbeat_metadata` | worker 存活 |

### 4.3 主要关系

```text
平台实例（底层表 tenants）
  -> app_users
  -> telegram_developer_apps
  -> tg_accounts
      -> account_pool
      -> account_proxy
      -> tg_group_accounts -> tg_groups
      -> tg_contacts
      -> security snapshots / batches

operation_targets
  -> channel_messages
      -> channel_message_comments
  -> tasks.type_config.target_*

tasks
  -> actions
      -> execution_attempts

rule_sets
  -> rule_set_versions
  -> tasks.type_config.rule_set_version_id

listener_source_state
  -> group_context_messages / channel_messages / source_media_assets
```

---

## 5. 执行器 PRD

### 5.1 Worker 角色

当前代码已经提供 planner / dispatcher / listener / recovery / metrics 分角色 drain 入口；本节描述这些角色的长期契约。生产是否已稳定拆成多个独立 worker 进程、并发配额、容量面板、token 预留 / 退款是否完整落地，需要结合部署、心跳、指标和压测继续确认。

| Role | 输入 | 输出 | 禁止事项 |
| --- | --- | --- | --- |
| Planner | running tasks、规则、账号池、上下文 | pending actions、next_run_at、stats | 不调用 TG API |
| Dispatcher | due pending actions | action result、execution_attempts、账号状态、任务 stats | 不生成新业务 action |
| Listener | listener source、监听账号、源目标 | 上下文、监听水位、源媒体缓存、事件 | 不发送业务消息 |
| Recovery | 超时 claim、超时 lease、worker 失联、unknown | 恢复 action、任务错误摘要、审计 | 不调用 TG |
| Metrics | action、task、worker、账号、代理、Redis | runtime snapshots、daily stats | 不改变业务状态 |

### 5.2 Planner 规划要求

- 扫描 `running` 且 `next_run_at <= now` 的任务。
- 检查任务结束时间、每日上限、静默期、积压阈值。
- 计算 24 小时曲线强度和任务类型业务基线。
- 调用任务类型 plan builder。
- 用 `plan_batch_key` 标记本轮计划。
- 用 `action_dedupe_key` 做业务去重。
- 更新 `tasks.next_run_at` 和 `tasks.stats`。

### 5.3 Dispatcher 执行要求

领取分三段：

```text
DB 短事务 claim
  -> status = claiming
  -> 写 claim_owner / claim_token / claim_expires_at

事务外拿运行资源
  -> Redis token bucket
  -> account in-flight lock
  -> proxy / target / media quota

DB 短事务确认执行
  -> status = executing
  -> 写 lease_owner / lease_expires_at
  -> 创建 execution_attempt
  -> 调用 Telegram Gateway
  -> 回写 success / failed / skipped / unknown_after_send
```

要求：

- Telegram API 调用期间不能持有数据库事务。
- 同一账号默认只能有一个 executing action。
- 进入 Gateway 调用边界后结果未知，必须标记 `unknown_after_send`，不能自动重发。
- FloodWait、SlowMode、账号受限、代理异常、目标权限不足和内容拦截必须分类。

### 5.4 Listener 要求

- 对同一来源进行 source claim，避免多个 listener 重复拉取。
- 持久化水位。
- 允许短窗口回补。
- 按唯一键去重。
- 默认过滤 bot 消息。
- 相册和 media group 需要聚合。
- 编辑 / 删除事件需要记录版本或状态。

### 5.5 Recovery 要求

- `claiming` 超时恢复为 `pending`。
- 未进入 Gateway 的 `executing` 可按策略恢复或失败。
- 已进入 Gateway 的超时必须进入 `unknown_after_send`。
- worker heartbeat 失联时恢复其持有 action。
- 记录恢复原因并暴露在任务详情和运营数据中。

### 5.6 Metrics 要求

首期快照指标：

- `actions.pending.count`
- `actions.claiming.count`
- `actions.executing.count`
- `actions.oldest_pending_age_seconds`
- `actions.claimed_per_minute`
- `actions.success_per_minute`
- `actions.failed_per_minute`
- worker heartbeat。
- 账号/代理错误。
- FloodWait / SlowMode 次数。

---

## 6. 核心数据流

### 6.1 账号到目标

```text
tg_accounts 在线
  -> sync groups / contacts / targets
  -> tg_groups / tg_contacts
  -> operation_targets
  -> target detail 聚合关联账号能力
  -> 任务中心 / 消息发送消费 operation_targets
```

### 6.1.1 目标准备

```text
operation_targets
  -> 只可由创建任务时的 target_input 自动创建 / 复用
  -> 检查账号-目标关系
      已满足：记录为可直接使用
      可准备：生成关注频道 / 加入群聊前置动作
      不可准备：记录阻塞原因
  -> 前置动作执行成功
  -> 写入 tg_group_accounts 或目标账号关系
  -> 任务中心容量预检复用这批关系
```

准入准备以任务创建 / 任务启动为主触发。任务详情先只展示历史失败和账号状态；运营目标页后续统一承载重新准入 / 重试处理，但不作为任务创建前置。任务启动前仍必须重新检查，不能只相信前端预检。

### 6.2 任务创建到执行

```text
前端创建任务向导
  -> POST /api/tasks/precheck
      可传已有 target_id，也可在创建链路传 target_type + target_input + target_title
      返回已满足 / 可准备 / 不可准备账号
  -> allow / warn / block
  -> POST /api/tasks/{type}/create-and-start
      后端 upsert operation_targets
  -> tasks.status = running
  -> Planner 先补齐关注 / 加入前置动作
  -> Planner 生成 actions
  -> Dispatcher claim + execute
  -> actions.result / execution_attempts
  -> task stats / operation metrics / audit
```

### 6.3 AI 活跃群

```text
Listener 采集群上下文
  -> Planner 判断接话 / 暖场 / 沉默模式
  -> AI Gateway 基于事实锚点生成多账号候选
  -> 规则过滤、语义去重、幻觉风险和输出校验
  -> Dispatcher 按账号冷却发送
  -> 记录 AI generation、turn、账号画像、事实锚点、质量风险和执行结果
```

AI 活跃群的默认策略是“接话为主、低频暖场为辅”：

- 最近存在可用真人消息时进入接话模式。系统只围绕最近 3-8 条真人消息、被 @ 的对象、当前人名 / 话题 / 问题生成短句回复，优先追问、附和、吐槽、补充和轻量转场。
- 长时间没有可接真人消息且任务允许空闲续聊时进入低频暖场模式。暖场只允许少量账号抛轻量话题或延续任务主题，不能编造具体经历、位置、回访、准点、穿着、服务过程等没有锚点的事实。
- 上下文不足、重复风险高、事实锚点不足、规则命中或目标群当前话题不适合接入时进入沉默模式。本轮不生成发送 action，但要记录 `skip_reason`。
- 每条候选消息必须记录事实锚点，锚点可以是真人消息 ID、当前话题、素材 ID 或账号画像。没有锚点的具体事实必须被丢弃或改写为泛化追问 / 附和。
- 同一轮多个账号必须有角色分工，例如起哄、追问、补充、降温、观察，不允许多个托管账号连续表达同一语义。

### 6.4 转发监听群

```text
Listener 采集源群消息
  -> source event 去重
  -> 规则集版本过滤 / 转换 / 路由
  -> 素材缓存或 source_media_assets 等待
  -> 生成目标群发送 action
  -> Dispatcher 发送
  -> 转发批次、源事件、目标发送项和规则归因
```

### 6.5 频道互动

```text
选择已有频道目标或粘贴新频道入口
  -> 后端 upsert operation_targets
  -> 同步频道消息 / 评论
  -> 创建频道浏览 / 点赞 / 评论任务
  -> 检查候选账号关注状态
      已关注或已确认满足关注条件：进入主互动规划
      未关注：生成 ensure_target_membership action
  -> 已关注账号先执行主互动
  -> 关注成功账号追加进入后续主互动容量
  -> 0 已满足且 0 准入成功则主互动 blocked
```

### 6.6 安全与资料批次

```text
选择账号
  -> 资料初始化 / 设置二步密码 / 清理登录设备
  -> 按入口固定 action_types，不在抽屉内混选其他动作
  -> 资料初始化走 profile-preview，一次 AI 请求生成整批资料
  -> 设置二步密码和清理登录设备走安全预检
  -> 创建 tg_account_security_batches
  -> 创建 batch items
  -> drain_account_security_batches
  -> Telegram Gateway 执行
  -> 回写 snapshots、items、accounts、audit
```

---

## 7. 接口清单

### 7.1 账号与安全

- `GET /api/tg-accounts`
- `POST /api/tg-accounts`
- `DELETE /api/tg-accounts/{account_id}`
- `POST /api/tg-accounts/{account_id}/login/start`
- `POST /api/tg-accounts/{account_id}/login/verify`
- `POST /api/tg-accounts/{account_id}/login/qr/check`
- `POST /api/tg-accounts/{account_id}/sync-now`
- `POST /api/tg-accounts/{account_id}/sync-targets`
- `GET /api/tg-accounts/{account_id}/detail`
- `POST /api/tg-accounts/security-batches/precheck`
- `POST /api/tg-accounts/security-batches/profile-preview`
- `POST /api/tg-accounts/security-batches`

### 7.2 目标和消息

- `GET /api/operation-targets`
- `POST /api/operation-targets`
- `PATCH /api/operation-targets/{target_id}`
- `GET /api/operation-targets/{target_id}/detail`
- `POST /api/operation-targets/{target_id}/sync-messages`
- `GET /api/channel-messages`
- `GET /api/channel-comments`
- `POST /api/message-send-tasks`
- `POST /api/message-send-tasks/batch`

### 7.3 任务中心

- `GET /api/tasks`
- `POST /api/tasks/precheck`
- `POST /api/tasks/{type}`
- `POST /api/tasks/{type}/create-and-start`
- `GET /api/tasks/{task_id}`
- `PATCH /api/tasks/{task_id}`
- `PATCH /api/tasks/{task_id}/settings`
- `POST /api/tasks/{task_id}/start`
- `POST /api/tasks/{task_id}/pause`
- `POST /api/tasks/{task_id}/resume`
- `POST /api/tasks/{task_id}/stop`
- `POST /api/tasks/{task_id}/retry`
- `POST /api/tasks/{task_id}/reset`
- `GET /api/tasks/{task_id}/actions`

`POST /api/tasks/precheck`、`POST /api/tasks/{type}` 和 `POST /api/tasks/{type}/create-and-start` 必须同时支持：

- 已有目标字段：`target_channel_id`、`target_operation_target_id`、`target_operation_target_ids`。
- 创建专用新目标字段：`target_type`、`target_input`、`target_title`。
- 返回或写入 `target_resolution`，说明目标是新建、复用、无法解析还是缺少加入入口。
- 返回 `ready_account_count`、`preparable_account_count`、`blocked_account_count`、`estimated_membership_actions`、`membership_warnings`。
- 返回 `membership_subtask_preview`，用于创建确认页和任务详情初始化展示准入子任务的预计进度、预计耗时、预计完成时间和容量拆分。

`PATCH /api/tasks/{task_id}/settings` 不接收 `target_input`、`target_title` 或创建专用 `target_type` 字段。编辑任务只能使用已有目标 ID 和已有配置字段，避免编辑弹窗隐式创建新运营目标。

`GET /api/tasks/{task_id}` 或任务详情聚合接口必须返回准入子任务摘要：

- `membership_subtask.status`。
- `membership_subtask.progress_percent`。
- `membership_subtask.estimated_finish_at` 或 `membership_subtask.estimated_remaining_seconds`。
- `membership_subtask.ready_account_count`、`pending_account_count`、`running_account_count`、`success_account_count`、`failed_account_count`、`blocked_account_count`。
- `membership_subtask.current_phase` 和 `membership_subtask.warnings`。
- 账号级准入明细或可分页查询入口。

### 7.4 规则、监听、数据

- `GET /api/listeners/summary`
- `POST /api/listeners/{object_type}/{object_id}/switch`
- `GET /api/rule-sets`
- `POST /api/rule-sets`
- `PUT /api/rule-sets/{rule_set_id}/config`
- `POST /api/rule-sets/{rule_set_id}/versions/{version_id}/publish`
- `POST /api/rules/test`
- `GET /api/operation-metrics/summary`
- `GET /api/reports`

### 7.5 风控和系统

- `GET /api/risk-control/summary`
- `PATCH /api/risk-control/global-policy`
- `POST /api/risk-control/preflight`
- `GET /api/account-proxies`
- `POST /api/account-proxies`
- `PATCH /api/account-proxies/{proxy_id}`
- `POST /api/account-proxies/{proxy_id}/check`
- `GET /api/developer-apps`
- `GET /api/ai-providers`
- `GET /api/materials`
- `GET /api/audit-logs`
- `GET /api/audit-logs/export`

---

## 8. 验收标准

### 8.1 产品验收

- 主流程只围绕账号、运营目标、规则、风控、任务、执行、数据和审计。
- 前端所有可见按钮必须有后端接口或明确的只读行为。
- 任务创建必须经过预检确认。
- 任务创建必须支持选择已有目标，也支持直接粘贴群聊 / 频道入口并自动创建或复用运营目标。
- 任务创建不能只允许选择已关注 / 已加入账号；必须允许选择账号范围，并展示已满足、可准备、不可准备三类容量。
- 频道任务和群聊任务必须在主互动前检查准入状态；频道未关注账号先关注，转发源群只要求已加入 / 可读取，AI 活跃群和转发目标群必须加入且可发言，成功后才进入主互动。
- 任务详情必须展示准入前置子任务的状态、预计进度、预计完成、容量统计、账号级结果和失败原因。
- 准入前置不能阻塞已满足账号执行主互动；已满足账号先执行，准入成功账号后续追加进入主互动容量。
- 文本问题、简单算数题和固定问答类入群验证必须由 AI 尝试处理，失败结果必须留痕。
- 频道评论/回复必须把讨论区解析失败、消息 ID 非频道帖子、频道未绑定讨论组或账号无法评论归因为评论区不可用，并给出可操作提示。
- AI 活跃群必须优先接真人上下文，只有在空闲场景才低频暖场；重复风险高、事实无锚点或上下文不足时应沉默并留痕。
- 账号资料初始化必须支持整批 AI 预览、手工编辑和本地兜底。
- 设置二步密码和清理登录设备必须是独立入口，不能和资料初始化混在同一个默认动作里。
- 操作手册必须同步展示最近更新功能，并和前端真实菜单、按钮和异常处理口径一致。
- 规则绑定必须使用已发布版本。
- 高风险操作必须写审计。

### 8.2 技术验收

- Planner 幂等，重复运行不重复生成 action。
- Dispatcher 多 worker 下同一 action 不重复执行。
- 同一账号不被并发滥用。
- Redis 不可用时不 fail-open。
- Telegram 调用结果未知时进入 `unknown_after_send`，不自动重发。
- AI generation 和 action payload / result 必须记录接话 / 暖场 / 沉默模式、事实锚点、语义簇、重复风险、幻觉风险和跳过原因。
- Listener 压力不拖慢发送 action。
- Recovery 能恢复超时 claim 和 worker 失联。
- Metrics 能展示 pending、executing、失败、延迟和 worker 状态。

### 8.3 测试范围

| 范围 | 用例 |
| --- | --- |
| 账号登录 | 验证码、二维码、2FA、session 失效、重新登录 |
| 资料初始化 | AI 成功、AI 超时、无健康供应商、本地兜底、手工编辑、头像跳过、`custom_prompt` 命名风格、50 账号一次 AI 请求 |
| 设置二步密码 | 未设置账号成功托管、已设置账号跳过、离线账号阻断、失败审计 |
| 清理登录设备 | 平台 Session 保留、外部设备清理、新登录 24 小时等待、失败重试 |
| 频道任务 | 任务内粘贴新频道、已关注直接互动、未关注前置关注、全部失败阻断主互动、部分成功继续、运行时再次守卫关注状态 |
| 频道评论异常 | 频道消息 ID 无法解析讨论区、频道未绑定讨论组、账号不可进入讨论组、目标实体无效、异常映射为 `COMMENT_UNAVAILABLE` 或 `PEER_INVALID` |
| 群聊任务 | 任务内粘贴新群聊、未加入先加入、AI 处理入群问题、全部失败阻断主互动、部分成功继续 |
| AI 活跃群质量 | 真人上下文接话、空闲低频暖场、无锚点沉默、语义重复拦截、幻觉事实拦截、多账号角色分工、质量字段留痕 |
| 准入前置 | 全部已满足、全部未满足、部分失败、无邀请链接、peer id 无法主动加入、失败重试、任务创建补齐准备、详情展示预计进度和账号级状态 |
| 准入与主任务并行语义 | 已满足账号不等待未满足账号、准入成功账号追加进入后续主互动、全部失败时主互动保持阻断 |
| 任务创建 | 5 类任务、保存草稿、创建并启动、编辑并重新规划 |
| Dispatcher | claim、执行、失败、重试、unknown_after_send |
| Listener | source claim、水位、bot 过滤、源媒体缓存 |
| 规则中心 | 创建、编辑、发布、测试、回滚、任务绑定 |
| 风控中心 | 策略编辑、代理检查、处置队列、preflight |
| 审计 | 危险动作、导出、权限变更、任务生命周期 |

---

## 9. 后续实施优先级

### P0 文档与口径

- 主设计文档、PRD、专项设计文档保持同步。
- 旧 Campaign / review / 卡密 / 订阅套餐仅保留兼容说明，不进入新主线。

### P1 执行层

- 确认生产是否已按多 role worker 拆成独立进程，并保持代码入口、`WORKER_ROLE` 配置、compose 服务名和部署脚本一致。
- Planner 幂等和积压保护继续补强。
- Dispatcher 并发配额、Redis token bucket、token 预留 / 退款继续压测，并补跨进程账号 in-flight 边界。
- Listener 独立化需要结合生产进程和心跳确认，继续补 source shard 扩容和采集延迟观测。
- Recovery 和 Metrics 已有基础能力，继续补容量面板、运行快照和故障恢复验收。

### P1 产品体验

- 任务创建继续保持快速创建 + 高级设置折叠。
- 任务创建增加目标输入，支持已有目标和新群聊 / 频道入口二合一。
- 运营目标页只做管理、查看、同步和复盘，不作为任务创建前置步骤。
- 任务账号选择改为“账号范围 + 准入预检”，不再把未关注 / 未加入账号提前排除在可选范围之外。
- 资料初始化继续完善批次进度、失败重试、头像素材池、username 冲突处理、预览 warning 和操作手册说明；设置二步密码、清理登录设备、代理风控形成闭环。
- 规则中心取消独立关键词库旧口径。

### P2 数据和审计

- 素材、规则、账号、目标、任务多维归因。
- `unknown_after_send` 人工确认和补偿查询。
- 数据保留、清理和导出权限。

### P3 高级能力

- 更完整的素材媒体缓存。
- 更强账号画像和长期话题编排。
- 代理风险分析和容量预测。
- worker 自动扩容建议。

---

## 10. 需要持续同步的文档

- `docs/tg-ops-platform.md`：系统总纲。
- `docs/tg-ops-platform-prd.md`：完整 PRD。
- `docs/account-security-hardening-design.md`：账号安全和资料初始化专项。
- `docs/channel-membership-precondition-design.md`：频道关注前置专项。
- `docs/rules-center-design.md`：规则中心专项。
- `docs/risk-control-and-account-center-design.md`：账号中心和风控专项。
- `docs/material-library-design.md`：素材和媒体专项。
- `docs/capacity-and-dispatch-upgrade-plan.md`：容量和调度专项。
