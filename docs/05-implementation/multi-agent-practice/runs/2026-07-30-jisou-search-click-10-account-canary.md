# 极搜搜索点击 10 账号完整逻辑 canary（2026-07-30）

## 目标与完成门

本次只验证极搜搜索点击的真实 Telegram 执行逻辑，不写生产
Task、Action、Attempt、obligation 或 click 账本。

单账号只有同时满足以下条件才计通过：

1. 真实账号向极搜发送关键词“郑州”；
2. 若出现图片验证码，提交精确命中的 callback_data 答案；
3. 在关键词结果页点击 row=0/col=0 的 callback_data `👥`；
4. 只用 callback_data 下一页控件翻页；
5. 从正文 `MessageEntityTextUrl` 精确匹配 `t.me/zzxshxc`；
6. 由同一账号执行 `channels.GetFullChannelRequest`；
7. 返回实体 `id=3298633687`、标题“河南郑州学生会”、
   username `zzxshxc`；
8. 未调用 join、request-to-join 或其他成员关系变更 RPC。

## 结果

| 账号 ID | 验证码分支 | 群结果页数 | 目标实体序号 | 结果 |
| ---: | --- | ---: | ---: | --- |
| 99 | 是 | 4 | 2 | pass |
| 183 | 否 | 4 | 2 | pass |
| 220 | 否 | 4 | 1 | pass |
| 167 | 否 | 4 | 2 | pass |
| 254 | 否 | 4 | 2 | pass |
| 168 | 否 | 4 | 1 | pass |
| 169 | 是 | 4 | 2 | pass |
| 152 | 否 | 4 | 1 | pass |
| 248 | 否 | 4 | 1 | pass |
| 231 | 否 | 4 | 2 | pass |

汇总：10 个不同账号，10/10 完成完整逻辑；验证码与无验证码
两条路径均覆盖。全部目标详情 RPC 返回同一目标实体，全部保持
`membership_side_effect=none` 和
`membership_mutating_rpc_invoked=false`。

## 根因结论

1. 验证码正确答案提交后返回的 `hot_list_page` 已保留原关键词
   结果和 `👥` 分类控件。旧逻辑重发关键词会再次触发验证码，
   形成验证循环。
2. 无验证码账号也会直接返回相同的带 `👥` 页面；将所有
   `hot_list_page` 直接判失败会丢弃可执行账号。
3. 群结果项不是 inline keyboard URL 按钮，而是正文
   `MessageEntityTextUrl`。只解析 `message.buttons` 永远无法命中
   目标。
4. 第 2 页以后含广告且按钮变为 `🔄 … ⏮️/⬅️/➡️`，旧协议
   指纹会误判为 `search_category_page`。
5. Telethon `message.click()` 不支持正文实体；纯点击证据必须由
   实体精确匹配和远端目标详情 RPC 构成，不能伪造按钮 row/col。
6. callback 常编辑同一 message ID；必须等待内容/实体/媒体修订，
   不能把 message ID 未变化当作页面未变化。

## 证据边界

- 临时脚本通过 SSH 在生产容器运行，使用账号 Redis in-flight 锁，
  并在执行前拒绝已有 claiming/executing Action 的账号。
- 生产期间有并行 release 替换容器。账号 99、183 的原始 JSONL
  随容器替换消失，但完成 stdout 已在当前 Codex 任务记录；
  其余 8 个账号在每次完成后立即复制到本地临时目录。
- 原始 JSONL 含机器人正文，不进入仓库；本文件只保留脱敏后的
  账号 ID、相位、页数、实体序号和完成判据。
- 这是执行逻辑 canary，不代表生产任务
  `fdb48029-4fda-4801-818d-0c509da37ea3` 已新增 10 条 confirmed
  obligation。
