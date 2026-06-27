# AI 活跃群话题、老师与连发模拟设计

## 背景

运营人员需要让每个 AI 活跃群任务围绕多个可配置主题和聊天对象老师展开，同时让同一账号偶尔连续发送 2-4 条短消息，模拟真人补充、追问或连续表达。

本设计只覆盖 `group_ai_chat`，不改变转发、频道浏览、频道点赞、频道评论等任务类型。

## 产品范围

- 任务配置新增 `topic_directions`、`teacher_targets`、`consecutive_message_*` 字段。
- Web 任务详情页提供 AI 活跃群专项设置；创建 / 编辑任务表单同步支持。
- TG bot 支持管理员在 bot 内完成 AI 活群任务设置。
- 执行器每轮把选中的话题方向和老师写入 AI prompt、话题计划和 action payload。
- 同账号连发模拟生成多条独立 action，并受现有容量、硬目标、风控和质量过滤约束。

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

## 验收标准

- 创建和更新任务可保存新字段；非法空标题、非法权重、连发窗口越界必须失败。
- 旧任务只有 `topic_hint` 时仍能生成，并在 payload 中看到回退话题方向。
- 开启连发且轮次足够时，同一账号生成 2-4 条连续 action，带完整 burst 元数据。
- Web 详情页和 TG bot 修改同一任务后，详情页读取到一致配置。
- QA 通过不等于生产恢复；本需求无需生产验证。
