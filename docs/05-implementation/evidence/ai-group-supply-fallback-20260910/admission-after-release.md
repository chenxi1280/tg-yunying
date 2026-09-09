# 修复发布后入群事实与 canonical 投影只读验收

快照：2026-09-10 03:17:57.167547+08:00；首修发布锚点：2026-09-10 01:31:26+08:00；最终候选 b3406f7e 部署完成锚点：2026-09-10 03:12:09+08:00。

数据库事务：transaction_read_only=on；isolation=repeatable read。仅查询指定10个tenant=1任务的已持久化事实，无Telegram请求、重试、回填或生产修改。

|任务|首修后成功Action|满足新事实证据链|canonical投影一致|最终部署后Gateway启动且投影一致|membership投影结论|
|---|---:|---:|---:|---:|---|
|郑州师范|0|0|0|0|unproven|
|美美备用|0|0|0|0|unproven|
|郑州楼凤|9|9|9|2|pass|
|郑州学生会|104|102|102|9|pass|
|郑州大学|63|63|63|7|pass|
|三亚|80|77|77|6|pass|
|天津音乐|28|28|28|1|pass|
|西安天上人间|133|131|131|11|pass|
|天津一品楼|0|0|0|0|unproven|
|成都怡红院|0|0|0|0|unproven|
|合计|417|410|410|36|6任务有新证据，4任务无新证据|

证据链同时要求：Action为ensure_target_membership且success、executed_at在首修后；typed fact为membership_observed且observed_at在首修后；fact关联的同一ExecutionAttempt为success且Gateway在首修后启动；租户、任务、Action、Attempt账号归属一致；Action payload目标与任务OperationTarget一致；Gateway结果membership_peer_ref等于任务目标引用或规范化后为同一公开用户名；canonical TgGroup引用与任务目标一致；canonical TgGroupAccount存在且can_send；结果指定的GroupBotAdmission与同一租户、群组、账号、membership Action一致。

最终部署后36条使用Gateway启动时间不早于03:12:09，避免把切换前已开始工作算作新版本独立执行。

所有410条新membership事实投影一致；未使用修复前74条旧事实证明本次结果，未修改旧记录。

边界：pass仅表示该任务本窗口新membership事实的canonical投影证据一致，不表示机器人准入已完成、账号当前可调度或AI发言已履约。TgGroupAccount.can_send持久化值不是独立发言远端事实。无新membership的4任务为unproven，不等于失败。

|GroupBotAdmission当前状态|事实数|
|---|---:|
|observation_stale|204|
|group_bot_policy_unresolved|166|
|awaiting_group_bot_rule|32|
|observation_open|8|

安全派生原始证据：admission-after-release.json。逐事实保留Action/Attempt/fact ID、时间、canonical group/admission ID和布尔校验；未导出公开或私有目标引用、凭据、会话或消息正文。

## 未计入新版本证据的7条成功Action

补充只读快照：2026-09-10 03:19:52.071417+08:00。7条均有成功Attempt及membership_observed；全部因Gateway在首修部署完成锚点01:31:26之前启动而排除，均于锚点之后完成。这7条不是缺少事实或投影失败，不能归入首修发布后的新启动执行。

|任务|Action ID|Gateway启动时间|Action完成时间|
|---|---|---|---|
|西安天上人间|ef729ab5-6ac5-47f3-9d37-4184ea62c078|2026-09-10 01:31:23.178167+08:00|2026-09-10 01:31:37.861816+08:00|
|西安天上人间|6cf059bd-6b5b-40ce-a5de-70611e264c28|2026-09-10 01:31:24.774999+08:00|2026-09-10 01:31:49.356516+08:00|
|三亚|41eb2b82-c578-4453-87ad-d83336fe16f0|2026-09-10 01:31:12.053763+08:00|2026-09-10 01:31:26.879755+08:00|
|三亚|1dec7f35-d41e-4425-b93a-dbd5f9045b24|2026-09-10 01:31:24.869339+08:00|2026-09-10 01:31:35.871566+08:00|
|三亚|a9af117b-2213-476c-a2e5-d78fd5cd3747|2026-09-10 01:31:23.078784+08:00|2026-09-10 01:31:49.357027+08:00|
|郑州学生会|40691159-9038-48c7-969c-49fafd84ce6b|2026-09-10 01:31:12.187459+08:00|2026-09-10 01:31:34.260722+08:00|
|郑州学生会|9d544521-595a-4968-b29f-33758aefb47c|2026-09-10 01:31:24.853349+08:00|2026-09-10 01:31:49.368641+08:00|

补充证据：admission-excluded-actions.json。
