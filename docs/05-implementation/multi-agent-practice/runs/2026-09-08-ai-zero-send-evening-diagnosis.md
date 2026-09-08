# 2026-09-08 AI 活群零发送复查（19:11–19:17）

- intake_id: ai-zero-send-20260908-evening
- from_agent: prod-diagnosis
- level: L3
- severity: P1
- status: reproduced; business_failed; production_fixed=false
- source: 用户追问“10 个 AI 任务已到期 14,430 条、确认完成 0 条，为什么还是没成功发送？”
- 范围：10 个 running group_ai_chat 任务；此次仅只读诊断，没有修改运行配置、重试、补发或数据清理。
- 生产代码：9d32d5a9777a1ca8be2333c553ec3f0dd42d2ad9；部署完成于北京时间 18:38:47。

## 结论

业务仍未恢复。19:11 数据库快照累计应发 14,999 条、确认完成 0 条；当天成功 remote_message_observed 为 0。应发数量来自日目标，不等于发起过同等数量的 Telegram 请求。18:38:47–19:16:37 的发送 Attempt 共 110 条：105 条 pacing_claim_deadline_exceeded、5 条 pacing_source_not_before，Gateway 调用均为 0。

## 分任务证据

下表日应发及 ready 数量锚定 19:11:08；活动时段分类锚定 19:12:39，动态期间新增/状态迁移会产生少量计数差异。活动时段开放仅证明时段条件通过，不替代其他发送准入条件。所有任务 confirmed=0、成功消息事实=0。

| 任务 / ID | 日累计应发 | ready 待发 | 已到计划时间的 ready | 活动时段开放 | 等未来时段 | 今日再无时段 |
|---|---:|---:|---:|---:|---:|---:|
| 三亚 / 562662d2-45e3-42b6-a58a-04899944b9d5 | 1462 | 254 | 164 | 24 | 92 | 48 |
| 天津一品楼 / 8d6cfb4d-9d39-45fc-80bc-102d989d0bfe | 1434 | 120 | 51 | 12 | 26 | 14 |
| 天津音乐 / 1c20106a-1ed8-4df3-a67e-da88789e329e | 1773 | 34 | 16 | 2 | 10 | 4 |
| 成都怡红院 / e5882928-35b3-41c2-88e0-fa6520df83e8 | 1496 | 191 | 151 | 19 | 97 | 35 |
| 美美备用 / 4a5d721a-ea09-49ac-95ea-dae1508a0b22 | 885 | 0 | 0 | 0 | 0 | 0 |
| 西安天上人间 / 5063e30f-9094-4c9f-8635-641626372bfe | 1629 | 375 | 257 | 42 | 153 | 62 |
| 郑州大学 / 11f3591a-cbcb-410a-a90e-0752ef5659dd | 1571 | 416 | 270 | 47 | 147 | 76 |
| 郑州学生会 / 8d64449d-994e-4d46-969e-9349f49066ba | 1686 | 396 | 247 | 45 | 141 | 62 |
| 郑州师范 / caed73c4-16d6-440e-8d0d-109c6fa1f184 | 1550 | 342 | 235 | 41 | 147 | 49 |
| 郑州楼凤 / 894d6924-8484-4650-b56e-b4003b852633 | 1513 | 345 | 234 | 43 | 130 | 60 |

## 已定位的断点

1. **候选派发没有随账号活动时段及时推进。** 生产普通派发实际并发为 13；`_run_dispatcher_batch` 用同样的上限取候选，`_claim_rows` 对不合适的候选结算/顺延后跳过，本轮不继续取队列后面的可发工作。AI 发送、浏览和入群共用此通道，候选按任务内旧计划时间排名；批次等待所有执行完成后才返回。观察窗口内入群失败调用平均约 16 秒、最高 24 秒，邀请未知结果约 15 秒。代码和线上排序共同支持其为派发延迟来源；尚未逐批记录全部等待耗时，不能声称所有耗时都由入群造成。
2. **错过活动时段被记作截止超时。** 19:12 对 99 条失败的只读反算使用真实 `_window_not_before` 函数和持久化活动计划：69 条是当天再无活动时段，30 条是真实业务截止已过。例：郑州大学 Action `317dcf3d-d62b-4ef9-a81a-50c23acfe95e` 原定 17:14，最后活动时段 17:14–17:56，到 19:11 才处理；业务截止仍为次日 00:00，却返回 pacing_claim_deadline_exceeded。该错误码混合了两个不同原因。
3. **仍在时段内的消息没有被取到。** 19:12:39 的 1,628 条已到计划时间 ready 中，275 条时段开放，943 条等待未来时段，410 条今日已无时段。19:16:37 复查：郑州楼凤 Action `6b966b3c-70dc-444b-9bd2-b2274c17bde5`、郑州学生会 Action `edfb626d-d926-496e-b9a4-f5f11f2044e1` 均 pending、Attempt=0，在实际分片候选查询中分别排 425、575；对应活动时段为 19:10–19:47、18:48–19:17。说明“全是账号不在活动时间”不能解释零发送。
4. **美美备用存在独立生成合同冲突。** 任务处于 unified 合同且 `ai_content_route_v2_enabled=false`；生成绑定函数因该开关跳过时限快照，provider 超时计算却要求所有 unified 调用都具备快照。当天 173 次 ai_generation_failed 均包含 generation_timing_snapshot_missing，ready=0。
5. **其他覆盖缺口并存。** 美美备用还有未入群覆盖；成都怡红院、郑州师范存在 voice_profile_missing；其余任务有生成失败/等待准入等状态。它们影响各账号覆盖，不能用来解释已有 ready 消息为何未派发。

6. **延长观察仍出现另一条锁环。** 数据库 deadlocks 由 18:44 的 1,887 增至 19:11 的 1,888；Planner 日覆盖物化路径 `ensure_task_daily_coverage` 在 `tg_groups` 相关事务锁上再次报 deadlock。它不是上一轮已调整的内容互斥入口，说明短窗口无报错不能外推为所有锁问题已解决。当前材料不足以判定锁环对每个任务延迟的贡献比例。

## 代码定位

- `backend/app/services/task_center/service.py`: `_run_dispatcher_batch`、`_dispatcher_concurrency`。
- `backend/app/services/task_center/direct_action_claims.py`: `_candidate_rows`、`_candidate_order`、`_claim_rows`。
- `backend/app/services/task_center/account_pacing_guard.py`: `revalidate_action_pacing_before_claim`、`_claim_session_floor`、`_settle_claim_pacing`。
- `backend/app/services/task_center/generation_timing_binding.py`: `bind_generation_timing_config`。
- `backend/app/services/task_center/generation_invocation_budget.py`: `provider_invocation_timeout`。

## 证据与限制

所有 SQL 使用 READ ONLY、20 秒 statement_timeout、2 秒 lock_timeout。未调用 claim、planner drain 或生成 provider；活动时段反算只运行读取持久化窗口的纯函数。无消息正文、提示词、手机号、凭据输出。候选排名为只读瞬时排名，不是永久位置。

- `/tmp/ai-zero-send-current.jsonl` / `.py`：10 任务、覆盖、生成、队列、事实及真实候选查询。
- `/tmp/ai-zero-send-session-proof.jsonl` / `.py`：活动时段与截止失败分类。
- `/tmp/ai-zero-send-dispatch-proof.jsonl` / `.py`：批次候选位置、Attempt、耗时和生成错误聚合。
- `/tmp/ai-zero-send-deadlocks.jsonl`：部署后延长观察的锁错误路径。

修复验收需分别证明：活动时段内 ready 工作及时进入真实 Gateway；不同截止原因明确记录；美美备用生成配置合同一致；每个任务最终出现同账号/同 Action/Attempt 关联的成功消息事实。当前仅完成诊断，未作业务恢复声明。
