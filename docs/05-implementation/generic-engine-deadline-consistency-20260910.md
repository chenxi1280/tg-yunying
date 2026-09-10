# 通用引擎截止一致性：本地交付记录

- Intake：generic-engine-deadline-consistency-20260910；L2。
- 用户范围：通用引擎、中性测试数据、本地验证；不部署。
- 基线：047625b4d38b6390c0fa738ca24560d7a117c164。
- 分支：codex/generic-engine-consistency-20260910。
- 设计：[截止一致性合同](../03-feature-designs/generic-engine-deadline-consistency-design.md)。
- 阶段：product/design_complete → dev → self_review → qa_pass（本地）。

## 修改结果

1. 频道类型的来源准入同时检查原预约时间和实际调用时间；恰好截止也拒绝调用。无法执行的预约不推进游标，取消本次占位后在原来源行锁内按其他预约和原调用事实重算尾部。
2. 准备截止耗尽使用 ValueError 子类明确表达。评论准备阶段仅捕获该类型，并接入现有 GenerationWait 的欠量结算；其他异常保持暴露，worker finally 不再把已结算终态重新置 pending。
3. 生成欠量结算同步原 Job、Action、评论义务/通用投影及预约；结算前锁定义务，核对租户、任务、代次及 Provider/Gateway 未决证据。已确认或未知状态不能被覆盖，冲突整体回滚。

不变范围：原数量、来源间隔算法、账号窗口、领取排序；活群专用游标与生成处理入口；已存在的真实/未知调用事实。未实现历史排期重建或历史数据清理，不据此声明先前所有业务问题均已修复。

## 反例与验证

- 修复前：9 个中性来源截止反例全部失败，包含三种频道动作、截止时刻/之后以及重复预约。
- 修复前：3 个评论准备过期/受保护状态反例失败，原异常导致本轮退出。
- 汇总验证：**139 passed, 1 warning，35.45 秒**。使用项目 backend/.venv，每次测试通过 subprocess timeout=60 强制限时。
- PostgreSQL：本机一次性实例，仅 Unix socket，独立 tg_yunying_test 数据库；包括行锁、其他合法预约保留、评论结算原子提交/回滚。测试没有连接生产数据库。
- 新增测试：test_source_deadline_consistency.py、test_source_deadline_consistency_postgres.py、test_comment_preparation_expiry.py、test_comment_preparation_expiry_postgres.py。
- 活群与共享回归：source_pacing_admission、source_pacing_owner_reuse、source_pacing_rebooking、source_pacing_late_admission、source_pacing_gap（含 PostgreSQL）、generation_deadlines、generation_timing_binding、generation_timing_non_v2。
- 评论回归：comment_generation_claim_failures、comment_generation_unknown_lineage、channel_comment_generation_postgres、channel_comment_generation_phases_postgres。
- 唯一 warning：既有 Alembic path_separator 弃用提示，不是业务测试失败。
- git diff --check、Python AST、变更代码文件不超过 500 行/函数不超过 50 非空行检查通过。

## 自审结论及边界

- 明确过期与未知错误分开；没有吞掉未知异常、生成假成功或缩短既有间隔。
- 新状态提交只发生在原准备流程确认过期且原身份可结算后；失败回滚由真实 PostgreSQL 用例验证。
- 活群分支不进入频道截止新增逻辑；共享类型异常仍兼容原 ValueError 处理。
- 未推送、未合并 master/release、未部署、未操作线上任务。qa_pass 仅表示上述本地范围通过。
