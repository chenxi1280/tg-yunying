# AI 活群过期积压删除与放弃合同

## Intake 与授权

用户在核查旧未知救援记录后明确要求：“那些线上已经过期了的，可以删除放弃的直接删除放弃”。本次生产维护范围为当前对话核查的 10 项 group_ai_chat Task，保留 Task 本身、配置及当天有效工作。按当前生产逐条确认原业务期限，不以 scheduled_at 早于现在单独判定过期。

2026-09-10 13:34–13:40 只读生产核查：1,589 条开放 send_message Action 全属 2026-09-09 日账本，原期限已过，全部无 Gateway 调用、无远端回执、无远端事实、无 journal。1,588 pending、1 executing，租约均无或已过期；1,589 个原数量位仍 open，1,589 个节奏预约仍 bound。所有 Action 均有接管审计引用，共 3,178 条；918 个 Action 有 1,869 条未调用 Attempt。关联当前 GenerationJob 有 466 条 unknown，不能删除其 Provider 未决证据。

## 处置与安全边界

1. 无引用、达到现行留存政策且正式清理入口允许的终态明细才能物理删除；本批 1,589 条均有业务审计引用，采用逻辑放弃，保留原 Action/Attempt/接管审计/Provider 未决记录。
2. 对精确原 ID、tenant/Task/epoch、Action 版本、payload/result 指纹、原日账本/期限、Quantity/FOP/预约/生成所有权、全部 Attempt/Journal/Fact/Fence 做只读预览。生产 SHA、数量、行指纹或状态漂移时该批零写失败，重新读当前状态，不重放旧批。
3. 通过原 `settle_fact_first_action_before_gateway` 写未执行结算及释放原预约，`project_generation_shortfall` 将原 FOP 收口为 `terminal_shortfall`；原数量位 terminal，原 coverage abandoned。不重开原义务、不重建、不补发、不移动到今天。
4. 原 SourcePacingAdmission 使用正式发前释放入口；已有全历史未调用 Attempt 通过正式资源结算释放尚占用的租约/预算/Fence。任一调用或远端变更证据不明确的行不执行放弃。
5. 仅撤销与原 Action 同身份的生成发布权。无未决 Provider lineage 的开放生成 Job 取消；有未决 lineage 的 Job 保持 unknown，原请求、结果、费用、transport 证据不改，取消原发送资格，停止原 Job 重试和发布。窗口仅按原发前 slot 合同处理，gateway_bound 必须先证明全历史未调用再走原 retirement helper；不借窗口标签推断调用。
6. 原 1,496 条已关闭未知救援保持原身份和证据；未知结果到期仅可用已有 unknown deadline 入口收口，不能改为未调用。今天已排到原截止外的等待项仍以其真实期限和正式 owner 判断，不提前改日目标。

## 执行与验收

运维脚本组合当前已部署的正式领域函数，以有界批次锁定精确行后重验，单批事务提交，每批记录 actor=codex、当前对话授权引用、生产 SHA、原 ID/指纹、处置原因和数量。维护代码不进入常驻 worker，不修改生产应用源码或配置，不调用 Telegram/Provider，不手工触发 Planner。

验收：原候选全部退出 pending/claiming/executing/retryable_failed；FOP terminal_shortfall、数量位 terminal、原预约 missed、可释放 source admission cancelled_pre_gateway；原未决 Provider 证据和接管审计守恒；原 1,496 条未知救援不变；精确 ID readback 与审计计数相符。物理删除量与逻辑放弃量分开报告，清理成功不等于业务履约恢复。

设计反向检查已覆盖正式入口与当前表结构。使用最小中性 fixture 验证未调用放弃、过期要求、引用保留、Provider unknown 保留和漂移零写，之后才运行受审计 apply。design_status=complete。

## Apply 反查 resync

前三批共 52 条提交并通过独立回读；第 4 批 8 条在正式窗口退役入口触发 `ai_content_window_gateway_bound`，整个批次回滚。深查发现母清单中有 10 个当前 Job 为 `unknown / gateway_reconcile_required` 且窗口仍 `gateway_bound`；其中一个 Action 还残留 executing。该证据与简单“无 Gateway 时间戳”分类不一致，不能把它们降级为确定未执行。按现有窗口合同保留这 10 条 Action/Job/窗口/预约和原对账身份，不做未执行事实或重试。

本轮实际可放弃集合缩为原母清单减去这 10 条，共 1,579 条。已提交 52 条不重放，剩余 1,527 条在当前生产版本重新生成精确预览后续作。此修订不放宽窗口退役条件，不吞掉异常，也不将未知结算改成成功。design_status=complete；resync=true。

完整性复查补充 84 条原 9 月 9 日日账本、但 scheduled_at 被推到 9 月 10 日午夜后的开放 Action：全量无 Attempt/Gateway/Fact，FOP/数量位 open，原期限均已过，另有 168 条接管审计引用。初筛 scheduled_at 会遗漏此类跨午夜积压，正式资格仅依赖原账本/期限与调用证明。使用同一逻辑放弃合同另建精确预览，不与原 1,579 条重叠；若全部通过，逻辑放弃总量应为 1,663，保留未知对账 10 条。当前日账本即使排期旧也不在处置范围。

数据库锁争用续作：Task 与账号 FK 锁在正常线上执行期间可能被占用。仅 PostgreSQL 明确 `55P03/40P01` 且当前事务已回滚的批次可记录为 conflict，先处理其他无交集批次；冲突集合必须重新预览后续作。任何其他异常仍立即失败，不把 lock conflict 算成功，不杀事务、不重启 worker、不放宽未知证据条件；每个成功批次仍必须独立回读。

Task 只读取身份/配置，使用与正式生成入口一致的 `FOR SHARE` 保护；它仍阻止 Task UPDATE/DELETE，但与其他相同共享读取兼容。Task 锁遵循已有 2 秒 lock_timeout 短暂等待正常事务交接；Action、Attempt、FOP、预约、生成窗口等实际写对象继续 `FOR UPDATE NOWAIT`。此调整不放宽版本/指纹守卫，原已提交批次不重放。

## 最终执行与独立验收

2026-09-10 14:19:11 +08:00 完成全量生产只读回读，生产 SHA 为 `093930751c1d90e2e177aebbbe211264f55d1a2f`。本次使用既有正式领域函数处置数据，没有应用代码发布。`disposal_status=verified`，`physical_deletion_count=0`；此结论仅指本合同范围的过期积压处置，不改变完整业务恢复验收结论。

最终母清单 1,673 条，精确去重后 1,663 条可放弃、10 条未决对账保护；72 个成功批次各有独立回读和持久化 AuditLog，成功 ID 无重复、无遗漏。失败批次全量回滚，重新预览后仅处理尚未提交的原 ID。

| 核对对象 | 最终结果 |
|---|---|
| 原 Action | 1,663 条全部 skipped，领取/执行租约为空，生成发布资格 cancelled |
| 原 FOP、数量位、账号预约 | 各 1,663 条，分别为 terminal_shortfall、terminal、missed |
| 来源准入 | 910 条 cancelled_pre_gateway |
| 执行与事实 | 原 1,852 条未调用 Attempt 保留；新增 1,663 条正式未执行结算 Attempt 和 safely_not_executed 事实；全部 Gateway 调用量为 0 |
| 关联当前 Job | 1,193 条 cancelled（含原已取消 2 条），456 条仍 unknown；全部无活动领取/发布租约或重试时间，原 Provider 未决证据保留 |
| 接管与维护审计 | 原 3,326 条接管审计保留；新增 72 条维护 AuditLog 全部按实际 ID 回读 |
| 原 1,496 条未知救援 | 原 ID、状态、结果 hash、Gateway 次数全部不变 |
| 保护集合 | 10 条 Action/Job/窗口/预约及相关行与原预览逐行 hash 一致；没有执行放弃 |
| 当前 Task | 10 项仍 running，config_revision=1、task_lifecycle_epoch=2 |

按原账本期限全量复查，剩余开放旧工作恰为上述 10 条保护集合，可处置残留为 0。额外终态留存预览的 4,643 条过期明细均有保护性关联，允许物理删除量为 0；本次保留审计数据并停止可放弃积压继续占用和重试。

验证包含 7 项中性定向测试与真实 PostgreSQL 共享锁/配置更新互斥检查，后端命令均设 60 秒硬超时。每批事务及最终 readback 校验采用生产真实数据，不能由测试结果替代。

完整执行凭据存放在 `/tmp/ai-group-live-check-20260910/`：`expired-disposal-complete-receipts.json`、`expired-disposal-final-readback.json`、`expired-disposal-protected-readback.json`、`expired-disposal-summary.json` 与版本化脚本 hash/原预览/失败及成功日志。易读报告为 `expired-disposal-result-20260910.md`。
