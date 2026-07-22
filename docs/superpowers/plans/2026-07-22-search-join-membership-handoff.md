# 搜索命中后的群聊准入交接计划

## 背景与根因

`search_join` 当前在极搜命中目标后直接调用 `JoinChannelRequest`。这把“搜索命中”和“实际成员关系”混在同一 action 中：审批群只会返回 `join_request_pending`，既没有复用 AI 活跃群的准入子动作，也没有独立的成员复核和回写入口。

不能直接创建现有 `ensure_target_membership` action：它读取 `TgAccount.session_ciphertext` 和通用账号凭据，可能绕过搜索任务绑定的授权槽位、代理和客户端元数据。因此要新增搜索专属准入 action，但复用 AI 活跃群的可观察状态语义。

## 成功标准

1. 极搜在任意可达结果页发现配置目标后立即停止翻页，并只记录 `target_found`。
2. 系统为该 source `search_join` 创建唯一的 `search_join_membership` 子 action；它使用 source 的授权槽位、开发者应用、代理与客户端 metadata 自己提交申请。
3. 子 action 只有获得 Telegram 成员关系复核后，才把 source 回写为 `membership_observed` 并计入 `daily_target_count`、创建后续 AI 活群联动。
4. `join_request_pending` 可见地保持 source 占位，并在未解决前阻止同任务、同账号、同目标再次申请；不得伪造成功或退回主 session。
5. 到 `scheduled_end` 前未得到成员关系时，任务保持真实未完成事实；到期后停止所有尚未派发的搜索及准入动作，且不计数成功。

## 实施步骤

1. 更新产品、专项设计、数据流和结构索引，定义 source/child 状态、审批等待、计数与运行时授权约束。
   - 验证：文档明确 `target_found -> search_join_membership -> membership_observed`，并明确不允许通用 session 回退。
2. 为 adapter/gateway/dispatcher 写失败测试：目标命中不直接加入、子 action 使用 scoped credentials、pending 申请不计数且不重复、成员观察回写 source 并触发联动。
   - 验证：先运行定向 pytest，确认新增断言在实现前失败。
3. 增加 `SearchJoinMembershipPayload`、child action factory 和搜索专属 Telegram adapter/gateway；搜索 adapter 改为只负责搜索命中。
   - 验证：gateway fixture 覆盖目标命中、申请 pending 和成员 probe。
4. 在 Dispatcher 创建并执行 child action；子 action 复用 source runtime authorization，定时只做成员复核，成功时原子回写 source 与 linked dispatch。
   - 验证：dispatcher fixture 检查 authorization session、proxy id、metadata 均来自 source slot，且主账号 session 不被调用。
5. 扩展 daily target progress、pacing、deadline cleanup 和 action sync，使 `membership_pending` 占位、`membership_observed_at` 按观察时刻计数、未解决申请跨日不重复。
   - 验证：planner/progress fixtures 覆盖日目标、审批等待、跨日和截止时间。
6. 运行定向测试、相关完整搜索任务测试和静态检查；review diff；提交并按 `master -> release -> Deploy Production` 发布。
   - 验证：本地测试均绿、GitHub Actions 全绿、生产镜像 SHA/worker health 与一次真实任务 action 事实分别核验。

## 发布后验收边界

- 代码和 worker 部署可由 CI、镜像 SHA 和健康状态证明。
- 已申请但外部群管理员尚未批准时，任务必须显示 `join_request_pending`；这不是可由代码保证完成的状态，不能计入 80。
- 只有真实 `membership_observed` action 才能证明一条日目标完成；80/80 需要真实生产 action 复核。
