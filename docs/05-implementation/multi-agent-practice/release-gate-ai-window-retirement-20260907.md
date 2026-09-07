# AI 已终结未发送内容槽回收修复 Release Gate

- intake: AI-GROUP-LOW-FULFILLMENT-20260907；L3；用户要求继续完成修复。
- base: d7ba60d82d41f532d97eb2d6082eef018961cd19；该版本已于 2026-09-07 22:25:10 北京时间正式上线，20 个容器健康、0227 两索引 valid。
- design_status: complete（ready/reviewing/candidate_ready 残留子项）；业务恢复仍 unproven。
- status: local_qa_passed；CI、生产发布及新版本业务验收 pending。

## 根因与修改

生产三个 AI worker 均报 ai_content_window_concurrent_conflict。20 个只读样本为旧 Action skipped/pacing_claim_deadline_exceeded，Attempt failed/已结束/零 Gateway/零 remote ID，typed fact safely_not_executed；旧 Job ready/reviewing 和 candidate_ready slot 未释放。现有回收逻辑仅支持 terminal Job 或 ready/gateway_bound 状态对。

本候选新增精确 candidate_ready + ready/reviewing 状态对复用同一正面未执行证据路径。旧 Action/Job/Attempt/fact 不改，唯一索引不放宽；unknown、已发送、未结束或归属漂移不回收。无需迁移，无批量生产数据操作。

## 审查与 QA

- 新状态对 4 个失败反例先红；修复后窗口单元测试 44 项通过。
- 窗口/生成绑定/历史 reconcile/fencing/runtime 组合 100 项通过。
- 真实隔离 PostgreSQL 8 项通过，原/新状态对均覆盖无 Attempt、在途 Attempt、安全未执行事实和 unknown 事实；测试使用独立 tg_yunying_test 和测试 fixture advisory lock。
- 全部后端测试以 backend/.venv 运行、subprocess 60 秒硬超时。diff check 和变更函数/文件度量通过。

## 发布与验收

按 master -> release -> Deploy Production；只提交本候选文件，保留正在变动的 channel_membership.py/test_task_account_pool.py 外部修改。无 schema 变更；回滚应用保留审计数据和有效索引，不恢复 unknown。

上线后独立核对全部角色 SHA/current/健康，以发布完成时刻为界检查旧槽冲突是否继续新增、生成 ready/成功消息、四类 typed facts。已有 pacing_claim_deadline_exceeded 样本的账号当天最后活动窗口确已结束（如 21:48，实际领取 22:35），不更改活动窗口、不批量补发欠量；只对仍有合法窗口和来源机会的工作判断吞吐恢复。日目标仍须持续观察，不能用 healthy 或零新异常替代履约。
