# Task Account Coverage Display Design

## Goal

AI 活跃群、频道浏览、频道点赞、频道评论任务需要在任务列表和任务详情展示“今日账号参与覆盖比例”，让运营能看到当天账号是否被更均匀地拉起来。

## Scope

- 覆盖任务类型：`group_ai_chat`、`channel_view`、`channel_like`、`channel_comment`。
- 覆盖动作类型：
  - AI 活跃群：`send_message`
  - 频道浏览：`view_message`
  - 频道点赞：`like_message`
  - 频道评论 / 回复：`post_comment`
- 覆盖状态：`pending`、`executing`、`success` 都算作当天已参与，因为这些账号已经被本任务占位。

## Data Contract

后端在任务 `stats` 中增加：

```json
{
  "account_coverage": {
    "covered_count": 18,
    "eligible_count": 30,
    "coverage_rate": 0.6,
    "coverage_percent": 60,
    "action_types": ["send_message"],
    "statuses": ["pending", "executing", "success"]
  }
}
```

`eligible_count` 使用任务账号配置下当前可选的账号数，不受 `max_concurrent` 截断，不做容量可用性过滤。它表达“这个任务今天应该尽量覆盖的账号池”，不是瞬时可发送容量。

## UI

- 任务列表的“执行统计”下展示：`账号覆盖 18/30，60%`。
- 任务详情顶部展示完整项：`今日账号参与覆盖`。
- AI Cycle 表增加“账号覆盖”列，展示该 cycle 的去重账号数 / turn 数。
- 频道消息分组表增加“账号覆盖”列，展示单条消息下的去重账号数 / action 数。

## Testing

- 后端测试覆盖同日去重账号统计、跨日不计入、其他动作类型不计入。
- 前端静态测试覆盖列表和详情页包含覆盖文案。
