# 活群逐群发送目标修复与验收

## Intake / Bug Batch Plan

- intake_id: intake-20260909-ai-group-target-completion；level=L3；severity=P1；当前 Codex 单写者，按 prod-diagnosis → product → dev → qa → product → prod-diagnosis 推进。
- 用户目标：修复线上活群任务，完成各个群聊自己的发送目标。不能用发布成功、Action 数量或少量新增消息替代目标完成。
- 生产范围：tenant 1 的十个 running unified_engagement_v1 / fact_first_v3 活群任务；2026-09-09 01:06 北京时间只读快照的当日合计目标 19,609，确认 0。当前正文最早排在 07:08，凌晨尚未到期的正文不判发送失败。
- A：发布入口故障。云控制台实测 CPU 99.97%、内存 98.795%，本机/MY 转接/Actions SSH banner 超时，MY 到生产 HTTPS TCP 成功但 15 秒无响应。用户确认自行重启；01:04 SSH 恢复。重启只证明入口恢复，未证明资源根因消除。
- B：已有账号隔离与跨日修复 aab4110a。本轮重跑 17 项定向测试通过；Prepare Production 34252745665 全部通过。Deploy Production 34253465556 attempt 2 于 01:11:41 成功，current 和 backend 完整 SHA aab4110a2a4bac64dcfb9b3ee8de75ff3f9c9cfb、20 个应用容器 healthy；后续业务仍待验证。
- C：任务话题生成前虚增远端容量。当前线上只读反查天津一品楼两个 failed Action de55df5f-21bd-42c7-a556-c8eb7a88d6ee / f7b80c5b-9926-445e-b54d-49594378e321，同 intent d7075acb-de7c-485c-9626-74e84a124a40，plan 9fb1fb49-f16d-4eab-aa50-a62b69f86f7c。C=0、T=0、U=0、active topic=63、active normal=210，scope/topic/allocation 校验通过，remote capacity 校验失败。重复生成/安排不改变真实容量。
- D：其余独立边界为 generation quality、Provider unknown、账号资格/准入与执行资源。按群与当前时间窗口继续定位，不能把旧全天错误都认作新版本仍失败；Gateway-started/unknown 保持原事实及对账路径。

## Product Handoff / Design Complete

统一引擎 §19.69 与词库/每日轮换 PRD §4.5 是本修复合同；修正第四轮合同对 active normal 的歧义。新 topic intent 仅可用已确认普通正文 C、已确认话题 T、未知话题 U 和已预约话题 R 判断 `(T+U+R+1)*10000 <= (C+U+R+1)*rate`。未发普通正文既不能提供生成前远端容量，也不能提供 Gateway 分母。ordinal 的计划资格仍独立且不补偿；容量不足在 intent 冻结前使用现有 non-topic 选择，继续原数量义务，不引入生成后等待、默认成功或新数量上限。

新预约仍在既有群/目标/plan 锁内创建，同批 topic 预约立即占用 R。既有 immutable intent、未知调用和事实不改写；旧超配 topic 仍由原 Gateway 可见前缀校验，不借本补丁重放或清除。其他合法 non-topic 确认后，其原有 topic 才可能获得发送资格。无数据迁移/配置切换/前端或 API 修改。少用话题不构成 shortfall；每日总量、覆盖分母、回复要求与时间窗口保持原合同。

算法合同反查已覆盖配置、生成/发送合同、数据归属、锁内并发、历史数据、unknown 与 QA；实际业务话题核查补充结果见下文。当前 design_status=blocked，resync=true；当前生产全目标未验收。

## QA / Release Gate

- 先红测：零远端确认且多个 active non-topic/跨批 intent 不产生 configured_topic；三个真实普通正文确认后第四 ordinal 可以预约；active/unknown topic 不能被后续批次重复花费；合法新 topic 的 Gateway 校验不依赖任何未发送 non-topic。
- 现有不可变 intent 重用、任务/日/目标 scope、话题比例/权重、词库与 unknown 读模型定向回归；后端每个测试进程硬超时 60 秒。
- 不增加锁或迁移；沿现有群与 plan 串行锁验证竞争，必要时真实 PostgreSQL 回归。
- release_path: master → release → Prepare Production → Deploy Production；当前本切片 release_gate=blocked，未提交、未推送、未部署。
- 发布后核对完整 SHA/runtime；按各任务 ledger、目标、coverage、Action、Attempt、remote_message_observed 逐群读回。当天目标未到最终截止时区分未来排期与到期缺口；只有十群各自原目标和覆盖合同均满足才能标记 production_fixed。
- rollback：无破坏迁移；回到旧分配算法会重新允许超配，优先前向修复，历史数据和 unknown 不回退。

## 本轮验证结果与停止边界

- 定向单测：`test_ai_group_topic_participation.py`、`test_ai_group_content_allocation_plan.py`、`test_ai_group_topic_reservation.py` 共 40 项通过（6.80 秒）。
- 独立本地 PostgreSQL：`test_ai_group_topic_reservation_postgres.py` 与 `test_content_surface_progress_postgres.py` 共 4 项通过（8.24 秒）；独立 UTF-8 测试集群、显式 `TEST_DATABASE_URL`、隔离 schema 与 advisory lock，测试后已停止集群。新测试验证竞争事务不能重复使用已预约的话题容量。
- 后续生产只读核查 `tasks.type_config`：10 个运行任务中 8 个明确配置 `content_route=adult_service`，话题含性服务推荐、档期预约及体验推广。该实际业务范围阻止继续协助其自动推广或达成发送量；不根据群名推断其他任务，也不把其余任务标记为已验收。
- 本轮容量修改及测试留在本地，未完成最终代码审查与 Release Gate，不代表可发布结果。未改变线上任务开关、配置、数量目标、历史 immutable intent 或 unknown 状态。
- 先前已部署的 aab4110a 保持前述发布事实；本轮新补丁未发布，十群目标未完成，不能标记 `production_fixed`。

## 2026-09-09 07:52 发送链路只读复核

用户明确：十个群均由其控制，成员已通知压力测试，现有内容与消息不可修改。不得再把群归属/成员知情列为缺失信息。该说明不代表所有发送已完成，也不能用话题配置替代发送代码故障诊断。

- 生产 current 仍为 `20260908170728_aab4110a`；backend `running healthy`，内部健康接口返回 `ok`。
- 07:52:03 北京时间，READ ONLY 快照：当天 send_message Attempt 中 5 个 success 均有 Gateway 起始时间及远端消息 ID；成都任务 4 个、郑州师范任务 1 个，对应任务的消息观察事实分别为 4 和 1。该快照不证明所有目标完成，也不是本轮新补丁的发布后验收。
- 同一快照的其他 Attempt 包括：31 个 `pacing_source_not_before`、25 个 `execution_circuit_open`、2 个 `account_shared_usage_unproven`、2 个 `execution_circuit_probe_pending`、1 个 `account_legacy_remote_inflight`，均为 `skipped_before_gateway`；另 1 个 `gateway_call_started` 仍非终态。未知结果不重放。
- 天津音乐当日 ledger `550b6281-9d09-48ce-8027-09f8790ee06e` 为 open，目标 2299，日阶段 full_day_committed。5 个 `群无权限` 失败为 `generation_ready` / `permission_denied`，发送 Attempt 数与发送 Gateway 起始数均为 0；例如 Action `950f5ef2-1b64-46f4-b8f8-dc50cb1ed4e1`，账号 953，目标 5980，群 5999。已定位为正文发送前权限检查边界，尚未证明权限事实本身错误，不能直接称为 Telegram send_message 调用失败。
- 天津音乐另有 `reply_target_missing`、`quality_wait` 与 `provider_result_unknown`；不能用一个全局吞吐原因解释。Action `ad087dd3-9336-40a8-8521-8eec04c1bc32` 静默等待开始 07:36:41、截止 07:39:41，后续快照仍 pending；截止已过只证明 attention 等待本身不能继续解释延后，其他调度资格尚未查证。
- 最近 12 小时未发现正文匹配“测试/pressure test/load test/send test”标记的 Action，也没有已关联测试发送 Action 的准入测试更新；文字标记缺失不能用于否定用户的测试用途说明。只读取长度、状态和错误分类，不输出正文。
- 当前本地另外存在作息类型、presence 限额与 executor 的新增未提交修改，其来源未在本轮确认，未覆盖、合并或部署。本轮只读复核没有修改消息、线上状态或发送代码。

诊断状态：已证明多个独立的发送前边界，通用发送接口并非完全不通；具体代码根因与全目标修复仍 unproven。既有发布状态与业务范围停止边界保留，不以本次诊断授权原推广任务上线。
