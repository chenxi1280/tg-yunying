# 郑州独立主题消息 E4 核验

2026-09-10 03:42:27.588998 +08:00，以生产只读 repeatable-read 快照核验，当前版本 `b3406f7ec385ace2b0e06a404e9d5dd66d07f1c5`。发布锚点为 03:12:09 +08:00。

**结论：本条独立主题消息 E4 pass。** 该结论只适用于下列精确义务，不等于全部任务完成，也不代表人工语义质量验收。

| 层级 | 精确证据 |
|---|---|
| Task / Account | Task `894d6924-8484-4650-b56e-b4003b852633`，epoch 2，账号 72，群内部 ID 2806 |
| 原数量 / coverage | quantity `979fda6e-878d-444a-ab23-ec4577ea57e3`；coverage `18cd2dce-c44d-4ccd-8e08-0cb54e831791` 已 confirmed |
| Action | `8b1917e6-ac40-4648-83a7-5c923c306458`，success，03:25:29.265553 |
| GenerationJob | `577ae535-9a35-4981-be90-42da7c30b967`，ready/gateway_bound，general route |
| Attempt | `04c8980e-28f9-432d-a04f-14dcf11d999a`，同账号 72，success |
| Gateway 开始 | 03:25:22.358291 +08:00，晚于发布锚点 |
| typed fact | `4a157564-556b-4e48-b166-ac4448d13f11`，remote_message_observed/send_message，03:25:29.265570 |
| 远端消息 ID | `4174616`，Attempt 与 typed fact 一致 |
| FOP | `db4f5ddd-9814-418a-b48c-a36957cd98c5`，confirmed，active Action 与 materialization=1 一致 |

context_mode=topic_only，原因 listener_watermark_unproven；批次 chat_mode=reply，但冻结 relation_kind=direct，实际 reply/interaction/turn 身份均为空。history、anchor、context 和 context snapshot 均为空，无 emergency selection。Job 冻结证据 source=configured_topic；使用生产 `validate_topic_only_candidate` 只读校验通过，Action 正文、Job candidate 与冻结候选 hash 一致，正文只保存长度和 SHA256。

Task/Action/Attempt 的 tenant、task、epoch、account 一致，ledger/slot 范围一致，typed fact 的 Action、Attempt、obligation、tenant、task 和 remote ID 完整一致。七项精确检查全部通过。

证据：[zhengzhou-topic-e4.json](zhengzhou-topic-e4.json)。全程无生产写入、无手动 Provider 或 Telegram 调用。
