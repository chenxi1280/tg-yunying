# 转发监听来源过滤升级方案

## 1. 背景与目标

当前转发监听任务默认屏蔽机器人消息，这个方向本身合理，但不能一直作为不可配置的固定规则。不同运营场景会有差异：有的源群机器人公告需要转发，有的群主或管理员消息不希望被搬运，有的普通成员需要长期加入不转发名单。

本次升级目标：

- 把机器人消息过滤改成任务级配置，默认仍保持开启。
- 增加“不转发群主和管理员消息”配置，默认关闭。
- 增加“来源不转发名单”，支持从最近来源发言人中选择，也支持手动粘贴。
- 保持旧任务兼容：旧任务未配置新字段时，等同于继续屏蔽机器人消息。

## 2. 产品交互

### 2.1 任务创建与编辑

转发监听群任务的高级设置增加“来源过滤配置”：

- 屏蔽机器人消息：开关，默认开启。
- 不转发群主和管理员消息：开关，默认关闭。
- 来源不转发名单：支持勾选和手动粘贴两种方式。

名单录入口径：

- 从最近来源发言人中选择：展示昵称、`@username`、`sender_peer_id`、最近消息预览、是否机器人、是否群主或管理员。
- 手动粘贴 `@username` / `sender_peer_id` / 昵称：一行一个，保存时尽量匹配最近来源发言人。

### 2.2 任务详情

转发批次 / 源事件区域展示来源信息：

- 源群。
- 发送者昵称。
- `@username`。
- `sender_peer_id`。
- 来源身份：普通成员、机器人、管理员、群主。
- 最近消息预览。

每条来源事件提供“加入来源不转发名单”操作。操作只更新当前转发任务配置，不影响其他任务。

## 3. 后端配置字段

建议在 `group_relay` 任务配置中增加：

```json
{
  "filter_bot_messages": true,
  "filter_admin_messages": false,
  "excluded_sender_peer_ids": [],
  "excluded_sender_usernames": [],
  "excluded_sender_names": []
}
```

字段说明：

- `filter_bot_messages`：是否屏蔽机器人消息。默认 `true`。
- `filter_admin_messages`：是否不转发群主和管理员消息。默认 `false`。
- `excluded_sender_peer_ids`：稳定来源 ID，不转发名单优先使用。
- `excluded_sender_usernames`：手动导入或解析出的 `@username`，作为次级匹配。
- `excluded_sender_names`：昵称兜底匹配，存在同名误伤风险。

监听采集需要保留来源身份字段：

- `sender_peer_id`。
- `sender_name`。
- `sender_username`。
- `is_bot`。
- `sender_role`，取值建议为 `member`、`admin`、`owner`、`unknown`。

## 4. 执行过滤顺序

转发任务在生成目标群发送项前执行来源过滤：

1. 如果 `filter_bot_messages=true` 且来源是机器人，跳过。
2. 如果 `filter_admin_messages=true` 且来源是群主或管理员，跳过。
3. 如果 `sender_peer_id` 命中 `excluded_sender_peer_ids`，跳过。
4. 如果 `@username` 命中 `excluded_sender_usernames`，跳过。
5. 如果昵称命中 `excluded_sender_names`，跳过，并在详情里标记“昵称兜底命中”。
6. 未命中来源过滤时，再进入规则集过滤、转换、路由和发送账号选择。

来源过滤只决定是否生成转发发送项；监听中心仍应记录来源事件和最近来源发言人，方便后续补充名单。

## 5. 兼容策略

- 旧任务没有 `filter_bot_messages` 字段时，按 `true` 处理。
- 旧任务没有 `filter_admin_messages` 字段时，按 `false` 处理。
- 旧任务没有来源不转发名单字段时，按空名单处理。
- 不改变规则集版本绑定、媒体处理、去重、路由和账号容量逻辑。
- 不使用“白名单”命名，因为本功能语义是“不转发名单”，不是允许通过名单。

## 6. 测试验收

后端验收：

- 旧任务未配置新字段时，机器人消息仍不生成转发发送项。
- `filter_bot_messages=false` 时，机器人消息可以进入转发过滤、转换和路由流程。
- `filter_admin_messages=true` 时，群主和管理员消息不生成转发发送项。
- `excluded_sender_peer_ids` 命中时，不生成转发发送项。
- `excluded_sender_usernames` 命中时，不生成转发发送项。
- `excluded_sender_names` 命中时，不生成转发发送项，并保留昵称兜底命中说明。
- AI 活跃群仍默认不使用机器人和平台托管账号消息触发续聊。

前端验收：

- 新建转发监听群任务时，“屏蔽机器人消息”默认开启。
- 编辑旧任务时，可以看到默认值并保存。
- 最近来源发言人可以被勾选加入来源不转发名单。
- 手动粘贴 `@username` / `sender_peer_id` / 昵称可以保存并回显。
- 任务详情中的“加入来源不转发名单”只影响当前任务。

文档验收：

- `tg-ops-platform.md` 不再把“过滤 bot 消息”写成不可配置固定原则。
- 本方案可以直接交给实现人员，不需要再决定字段、默认值或交互入口。
- 文档保持新版 TG 运营管理平台口径，不引入旧 Campaign、多租户 SaaS、卡密订阅等旧主线。
