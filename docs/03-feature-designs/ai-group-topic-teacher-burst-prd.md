# AI 活跃群话题、老师与连发模拟设计

## 背景

运营人员需要让每个 AI 活跃群任务围绕多个可配置主题和聊天对象老师展开，同时让同一账号偶尔连续发送 2-4 条短消息，模拟真人补充、追问或连续表达。

本设计只覆盖 `group_ai_chat`，不改变转发、频道浏览、频道点赞、频道评论等任务类型。

## 产品范围

- 任务配置新增 `topic_directions`、`teacher_targets`、`consecutive_message_*` 字段。
- Web 系统设置新增租户级 TG Bot 配置中心，支持 Bot Token、管理员 Chat ID、AI 活群 Bot 设置开关、测试发送和 webhook 配置状态。
- Web 任务详情页提供 AI 活跃群专项设置；创建 / 编辑任务表单同步支持，并以结构化表单作为主入口。
- TG bot 支持管理员在 bot 内通过按钮选择任务、查看设置、编辑话题方向、编辑老师、编辑连发参数并确认保存。
- 执行器每轮把选中的话题方向和老师写入 AI prompt、话题计划和 action payload。
- 同账号连发模拟生成多条独立 action，并受现有容量、硬目标、风控和质量过滤约束。

## 租户级 TG Bot 配置

TG bot 是运营空间级能力，不属于单个任务。Web 必须提供“系统设置 / TG Bot 配置”入口：

- `telegram_bot_token`：必填，保存后只显示已配置状态，不回显明文。
- `admin_chat_id`：必填，限制可操作 bot 的 Telegram chat。
- `ai_group_bot_enabled`：是否允许管理员通过 bot 修改 AI 活群任务配置。
- `telegram_bot_webhook_secret`：服务端生成，用于 webhook 路由和校验。
- `telegram_bot_webhook_status` / `telegram_bot_last_error`：展示 webhook 最近注册或入站错误。
- 测试发送：向 `admin_chat_id` 发送一条测试消息，失败时展示 Telegram 返回错误摘要。

只有 `telegram_bot_token`、`admin_chat_id` 和 `ai_group_bot_enabled` 都满足时，bot 内 AI 活群设置入口才可用。

## 配置契约

`topic_directions` 为数组，每项：

- `title`：必填，1-60 字。
- `description`：可选，最多 240 字。
- `weight`：必填，0.01-100，用于后续按权重挑选话题。

`teacher_targets` 为数组，每项：

- `name`：必填，1-60 字。
- `description`：可选，最多 240 字。
- `priority`：必填，1-100，数字越大越优先。

连发模拟：

- `consecutive_message_enabled` 默认 `false`。
- `consecutive_message_min` 默认 `2`，范围 2-4。
- `consecutive_message_max` 默认 `4`，范围 2-4。
- `consecutive_message_probability` 默认 `0.3`，范围 0-1。
- 若 `consecutive_message_min > consecutive_message_max`，配置必须失败。

旧 `topic_hint` 保留。若 `topic_directions` 为空且 `topic_hint` 有值，系统按一个临时话题方向处理，但不强制回写旧数据。

## 执行规则

- 每轮根据权重选择一个话题方向；若没有配置，回退到 `topic_hint` 或群目标方向。
- 每轮根据 priority 选择一个老师目标；若没有配置，老师目标为空。
- AI 生成提示必须明确“围绕话题方向”和“面向聊天对象老师”，但不得在群聊内容中暴露系统、任务或 AI。
- 连发触发后，选定同一个账号承接本轮连续窗口内的多条 action；窗口大小不超过本轮计划条数。
- 连发 action 必须写入同一个 `burst_id`，并分别写 `burst_index`、`burst_size`。
- 连发不得绕过 `allow_account_repeat=false` 的硬限制；当任务禁止账号重复发言时，连发不触发。

## Web 交互

AI 活群设置在创建、编辑和详情页复用同一套结构化控件：

- 话题方向：支持新增、编辑、删除 `title`、`description`、`weight`。
- 聊天对象老师：支持新增、编辑、删除 `name`、`description`、`priority`。
- 同账号连发：支持开启/关闭、2-4 条窗口和触发概率；当 `allow_account_repeat=false` 时显示“不触发连发”的提示。
- 高级 JSON 编辑只作为兜底入口，不能作为运营设置主入口。

任务详情页必须展示 TG bot 可用状态。Bot 未配置、管理员 Chat ID 缺失、AI 活群 Bot 设置关闭或 webhook 异常时，详情页应提示当前无法通过 bot 设置，并指向租户级 TG Bot 配置入口。

## TG bot 交互

Bot 内使用 inline keyboard，不要求管理员手写 JSON：

1. `/start` 或 `/ai_group` 显示主菜单。
2. “AI 活群任务”列出最近 20 个未删除的 AI 活群任务。
3. 选择任务后显示当前话题数、老师数、连发状态和操作按钮。
4. “设置话题方向”“设置聊天对象老师”“设置同账号连发”进入分步输入。
5. 输入内容先进入会话草稿，保存前展示摘要。
6. “确认保存”后复用后端配置校验写入任务 `type_config`；“取消”丢弃草稿。

会话草稿必须记录 `task_id`、编辑步骤、草稿配置和更新时间。草稿超时或任务被删除/停用时必须拒绝保存并提示重新选择。

## 数据流转

租户级：

`SystemConfigView / TelegramBotSettingsView -> PATCH /api/tenant-bot-settings -> update_tenant_bot_settings -> Tenant.telegram_bot_* / admin_chat_id / ai_group_bot_enabled`

Webhook：

`Telegram Bot API -> POST /api/telegram-bot/webhook/{tenant_id}/{webhook_secret} -> resolve tenant -> verify admin_chat_id -> handle_group_ai_bot_update`

任务级：

`Web 任务设置 / TG bot 确认保存 -> update_group_ai_chat_config -> Task.type_config -> group_ai_chat planner -> Action.payload`

## 验收标准

- 创建和更新任务可保存新字段；非法空标题、非法权重、连发窗口越界必须失败。
- 旧任务只有 `topic_hint` 时仍能生成，并在 payload 中看到回退话题方向。
- 开启连发且轮次足够时，同一账号生成 2-4 条连续 action，带完整 burst 元数据。
- Web 可配置 Bot Token、管理员 Chat ID、AI 活群 Bot 设置开关，并可测试发送。
- TG bot webhook 不依赖 update 体内业务 `tenant_id`，secret 错误、非管理员 Chat ID、未启用 AI 活群 Bot 设置时必须拒绝。
- TG bot 提供按钮式任务选择、设置查看、分步编辑和确认保存。
- Web 详情页和 TG bot 修改同一任务后，详情页读取到一致配置。
- QA 通过不等于生产恢复；本需求无需生产验证。
