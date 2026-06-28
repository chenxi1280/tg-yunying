# AI 活跃群话题、讨论老师与连发模拟设计

## 背景

运营人员需要让每个 AI 活跃群任务围绕多个可配置主题和讨论老师展开，同时让同一账号偶尔连续发送 2-4 条短消息，模拟真人补充、追问或连续表达。

本设计只覆盖 `group_ai_chat`，不改变转发、频道浏览、频道点赞、频道评论等任务类型。

## 产品范围

- 任务配置新增 `topic_directions`、`teacher_targets`、`consecutive_message_*` 字段。
- Web 系统设置新增租户级 TG Bot 配置中心，支持 Bot Token、多个管理员 Chat ID、AI 活群 Bot 设置开关、测试发送和 webhook 配置状态。
- Web 任务详情页提供 AI 活跃群专项设置；创建 / 编辑任务表单同步支持，并以多行文本作为话题方向和讨论老师主入口。
- TG bot 作为轻量运营入口，支持管理员选择任务、查看设置摘要、查看话题 / 讨论老师摘要，并通过按钮用多行文本设置话题方向和讨论老师。
- 执行器每轮把选中的话题方向和讨论老师写入 AI prompt、话题计划和 action payload。
- 同账号连发模拟生成多条独立 action，并受现有容量、硬目标、风控和质量过滤约束。

## 租户级 TG Bot 配置

TG bot 是运营空间级能力，不属于单个任务。Web 必须提供“系统设置 / TG Bot 配置”入口：

- `telegram_bot_token`：必填，保存后只显示已配置状态，不回显明文。
- `admin_chat_id`：必填，支持每行、逗号、中文逗号或分号分隔多个 Telegram chat；任一配置 chat 都可操作 bot。
- `ai_group_bot_enabled`：是否允许管理员通过 bot 查看和轻量管理 AI 活群任务。
- `telegram_bot_webhook_secret`：服务端生成，用于 webhook 路由和校验。
- `telegram_bot_webhook_status` / `telegram_bot_last_error`：展示 webhook 最近注册、查询或入站错误；状态以 `tenant-tg-bot-webhook-registration-prd.md` 为准。
- 测试发送：向全部 `admin_chat_id` 发送一条测试消息，任一发送失败时展示 Telegram 返回错误摘要。测试发送只证明出站 `sendMessage`，不能替代 webhook 注册和入站健康。
- Webhook 注册：保存有效 Bot Token 和管理员 Chat ID 后必须自动调用 Telegram Bot API `setWebhook`，失败时前端必须显示 bot 不可用和错误摘要，不能只显示“已保存”。

只有 `telegram_bot_token`、`admin_chat_id` 和 `ai_group_bot_enabled` 都满足时，bot 内 AI 活群轻量管理入口才可用。

## 配置契约

`topic_directions` 为内部数组，每项：

- `title`：必填，1-60 字。
- `description`：可选，最多 240 字。
- `weight`：必填，0.01-100，用于后续按权重挑选话题。

`teacher_targets` 为内部数组，每项：

- `name`：必填，1-60 字。
- `description`：可选，最多 240 字。
- `priority`：必填，1-100，数字越大越优先。

Web 创建 / 编辑表单和 TG bot 配置会话不要求用户手写数组或 JSON。运营人员只需要输入普通多行文本：

- 话题方向：每行一个话题，第一行权重最高，系统按行顺序写入 `weight`。
- 讨论老师：每行一个对象、小姐、老师称呼或描述，第一行优先级最高，系统按行顺序写入 `priority`。

运营人员可以把任务内的多行话题方向和讨论老师合称为该任务的“话题包”。当前阶段话题包随任务保存，不新增跨任务模板表；后续如需要复用到多个任务，再单独设计模板中心、版本和发布流程。

连发模拟：

- `consecutive_message_enabled` 默认 `false`。
- `consecutive_message_min` 默认 `2`，范围 2-4。
- `consecutive_message_max` 默认 `4`，范围 2-4。
- `consecutive_message_probability` 默认 `0.3`，范围 0-1。
- 若 `consecutive_message_min > consecutive_message_max`，配置必须失败。

旧 `topic_hint` 必须通过迁移写入 `topic_directions`，迁移后移除旧字段。若任务已经有 `topic_directions`，迁移只移除旧 `topic_hint`，避免同一任务出现两套话题来源。迁移完成后 AI 活群 schema、设置白名单、TG bot 摘要和执行器都不得再读取 `topic_hint`；未配置话题方向时只回退到目标群运营方向。

## 执行规则

- 每轮根据权重选择一个话题方向；若没有配置，回退到群目标方向。
- 每轮根据 priority 选择一个讨论老师；若没有配置，讨论老师为空。
- AI 生成提示必须明确“围绕话题方向”和“面向讨论老师”，但不得在群聊内容中暴露系统、任务或 AI。
- 连发触发后，选定同一个账号承接本轮连续窗口内的多条 action；窗口大小不超过本轮计划条数。
- 连发 action 必须写入同一个 `burst_id`，并分别写 `burst_index`、`burst_size`。
- 连发不得绕过 `allow_account_repeat=false` 的硬限制；当任务禁止账号重复发言时，连发不触发。

## Web 交互

AI 活群设置在创建、编辑和详情页复用同一套输入规则：

- 话题方向：主入口为多行文本，每行一个话题，越靠前权重越高。
- 讨论老师：主入口为多行文本，每行一个对象，越靠前优先级越高。
- 同账号连发：支持开启/关闭、2-4 条窗口和触发概率；当 `allow_account_repeat=false` 时显示“不触发连发”的提示。
- 不得把 JSON 或数组字段作为运营设置主入口。

任务详情页必须展示 TG bot 可用状态。Bot 未配置、管理员 Chat ID 缺失、AI 活群 Bot 设置关闭或 webhook 异常时，详情页应提示当前无法通过 bot 查看或设置轻量配置入口，并指向租户级 TG Bot 配置入口。

## TG bot 交互

Bot 内使用 inline keyboard，但只保留轻量运营能力：

1. `/start` 或 `/ai_group` 显示主菜单。
2. `/admin` 显示管理员菜单、webhook 状态和 AI 活群轻量管理开关状态；若 AI 活群设置未启用，仍必须回复“bot 已连接但 AI 活群设置未启用”。
3. “AI 活群任务”列出最近 20 个未删除的 AI 活群任务。
4. 选择任务后显示当前话题数、讨论老师数、连发状态、全账号日覆盖状态和 Bot 可设置范围。
5. “查看话题摘要”只展示前若干条话题方向和讨论老师，便于在移动端快速核对。
6. “设置话题方向”进入一次性多行输入会话，用户发送每行一个话题后立即保存为 `topic_directions`。
7. “设置讨论老师”进入一次性多行输入会话，用户发送每行一个对象后立即保存为 `teacher_targets`。
8. “打开 Web 编辑”跳转到 Web 任务中心；若服务端没有配置公网地址，必须回复“请到 Web 任务中心编辑该任务”。
9. 旧按钮或旧命令 `/ai_group_set <json>` 不得作为主入口；复杂字段写入必须返回可见说明：TG bot 仅支持查看摘要，以及配置话题方向和讨论老师，其它完整配置请到 Web 任务详情编辑。

TG bot 只为话题方向和讨论老师创建配置会话，不再展示“确认保存”。如果数据库中存在无法识别的旧草稿，用户再次发送普通文本时必须明确提示旧草稿已取消。

## 数据流转

租户级：

`SystemConfigView / TelegramBotSettingsView -> PATCH /api/tenant-bot-settings -> update_tenant_bot_settings -> setWebhook -> getWebhookInfo -> Tenant.telegram_bot_* / admin_chat_id / ai_group_bot_enabled / webhook_status`

Webhook：

`Telegram Bot API -> POST /api/telegram-bot/webhook/{tenant_id}/{webhook_secret} -> resolve tenant -> parse/verify admin_chat_id list -> handle_group_ai_bot_update`

任务级：

`Web 任务设置 -> update_group_ai_chat_config -> Task.type_config -> group_ai_chat planner -> Action.payload`

TG bot 入站轻量配置：

`Telegram Bot API -> POST /api/telegram-bot/webhook/{tenant_id}/{webhook_secret} -> resolve tenant -> parse/verify admin_chat_id list -> handle_group_ai_bot_update -> 任务摘要 / 话题方向配置 / 讨论老师配置 / Web 编辑入口`

## 验收标准

- 创建和更新任务可保存新字段；非法空标题、非法权重、连发窗口越界必须失败。
- 旧任务只有 `topic_hint` 时，迁移后必须在 `topic_directions` 中看到同名话题，且 `topic_hint` 被移除。
- 开启连发且轮次足够时，同一账号生成 2-4 条连续 action，带完整 burst 元数据。
- Web 可配置 Bot Token、多个管理员 Chat ID、AI 活群 Bot 设置开关，并可向全部管理员测试发送。
- 保存 Bot Token 和管理员 Chat ID 后必须自动注册 webhook，并能查询 Telegram 当前 webhook URL；注册失败或 URL 不匹配时页面显示不可用，测试发送成功不能覆盖该失败。
- TG bot webhook 不依赖 update 体内业务 `tenant_id`，secret 错误、Chat ID 不在管理员列表、未启用 AI 活群 Bot 设置时必须拒绝。
- `/start`、`/admin`、`/ai_group` 对授权管理员必须有可见回复；AI 活群设置未启用时回复状态说明，不允许静默无响应。
- TG bot 提供按钮式任务选择、设置摘要、话题摘要、话题方向配置、讨论老师配置和 Web 编辑入口；不提供复杂字段分步编辑、确认保存或 JSON 设置主入口。
- Web 详情页修改任务后，TG bot 再次查看同一任务时读取到一致摘要。
- QA 通过不等于生产恢复；本需求无需生产验证。
