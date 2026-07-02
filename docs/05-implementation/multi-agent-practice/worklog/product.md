# Worklog: product

## 2026-07-02 托管 2FA 密码受控查看产品口径

- message_id: 2026-07-02-managed-2fa-reveal-product-001
- action: 按用户要求先更新 PRD / 专项设计，再交付实现
- input: 账号详情需要支持查看新托管 2FA 密码，便于人工复制登录
- output: PRD 明确账号详情“托管 2FA”面板可按需 reveal 当前托管密码；必须具备 `accounts.security.credential_manage` 并写审计，查看不需要填写原因，账号详情不默认回显
- evidence: `docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/account-security-hardening-design.md`
- decision: L2；需要代码实现和本地 QA，release_gate=pending，production_verification=false
- next_agent: dev
- unresolved: 未发布生产；QA 线程真实投递未完成

## 2026-06-27

- message_id: 2026-06-27-docs-practice-plan-001
- action: 将 Incident Report 转成文档级修复任务
- input: 2026-06-27-docs-practice-incident-001
- output: 要求 dev 建立四 Agent 协作材料和一次完整演练记录
- evidence: `docs/README.md` 指定 `05-implementation` 是当前代码到 PRD 的实施清单入口
- decision: 本次只做文档级实践，不改业务代码，不触发发布流程
- next_agent: dev
- unresolved: 产品线程已返回 Product Handoff；开发线程已返回 Development Complete；QA 线程正在验收

## 2026-06-27 index responsibility supplement

- message_id: 2026-06-27-index-maintenance-product-001
- action: 补充产品 Agent 的产品模型和数据流转索引责任
- input: 管理 / 产品 Agent 需要把产品设计、数据流程设计、数据流转索引沉淀下来
- output: 新增 `index-maintenance.md`，并将 `project-dataflow-index.md` 纳入 product 工作区
- evidence: `docs/00-index/project-dataflow-index.md`
- decision: product 交接给 dev 前必须说明 PRD/设计/数据流转索引是否更新
- next_agent: dev
- unresolved: 本次只补协作规则，不重建全量数据流转索引

## 2026-06-28 AI 活群话题老师连发配置

- message_id: 2026-06-28-ai-group-topic-teacher-burst-product-001
- action: 将用户新需求重新按 product 流程登记为 Intake + Triage + Product Handoff
- input: 每个 AI 活群任务支持多个话题方向、聊天对象老师、同账号 2-4 条连发模拟，并支持 Web 详情页和 TG bot 内设置
- output: L2 标准流程，投递 dev；已有 `codex/ai-group-topic-teacher-burst` 分支只能作为 dev draft 输入，不作为完成结论
- evidence: PRD / 专项设计 / 数据流转索引已存在草稿变更；本线程曾误执行实现，需要 dev 重新复核
- decision: production_related=false，release_gate_required=true，production_verification_required=false
- next_agent: dev
- unresolved: 等待 dev 输出 Development Complete，再投递 QA；QA pass 后回 product 做 product acceptance

## 2026-06-28 AI 活群话题老师连发配置产品验收

- message_id: 2026-06-28-ai-group-topic-teacher-burst-product-acceptance-001
- action: 对 QA pass 的 AI 活群话题、老师、连发和 TG bot 设置能力做产品验收
- input: 2026-06-28-ai-group-topic-teacher-burst-qa-001
- output: product_accepted
- evidence: QA E2 覆盖 schema、planner payload、同账号连发、TG bot 权限与保存、Web build；PRD / 专项设计 / 数据流转索引 / 结构索引已同步
- decision: 产品接受本地功能范围；release_gate=pending，等待 master/release 推送和 CI/deploy
- next_agent: dev
- unresolved: CI / release deploy / production verification unproven

## 2026-06-28 hard-hourly min 10 产品验收

- message_id: 2026-06-28-hard-hourly-min-10-product-acceptance-001
- action: 对 AI 活群每小时硬目标默认/最低值 10 做产品验收
- input: 2026-06-28-hard-hourly-min-10-qa-001
- output: product_accepted
- evidence: QA E2 覆盖 schema、迁移、前端常量、PRD 和 ops 文档
- decision: 产品接受本地变更范围；release_gate=pending，等待 master/release 推送和 CI/deploy
- next_agent: dev
- unresolved: CI / release deploy unproven

## 2026-06-28 BB-P0-A duplicate-send-runtime 产品验收

- message_id: 2026-06-28-bug-batch-product-acceptance-bb-p0-a-001
- action: 对 QA `qa_pass` 的 BB-P0-A duplicate-send-runtime 做 Product Acceptance
- input: 2026-06-28-bug-batch-qa-to-product-bb-p0-a-001
- output: product_accepted
- evidence: QA E2 覆盖 runtime reservation、action dedupe、membership admission snapshot、unknown_after_send retry/recovery；数据流转索引和结构索引已覆盖对应口径
- decision: 产品接受本地修复范围；L3 仍不能关闭，release_gate=pending，E3/E4 unproven
- next_agent: dev
- unresolved: 等待 Release Gate / CI / 部署后，再交 prod-diagnosis 做 production verification

## 2026-06-28 AI 活群全账号日覆盖模式产品设计

- message_id: 2026-06-28-ai-active-all-accounts-product-design-001
- action: 将 dev 转交的原始需求整理为 Intake + Triage + Product Design Complete，并真实投递 dev
- input: 每个 AI 活群任务在 24 小时内让每个账号发 1-2 条消息，用于拉高群活跃度；整体类似现有 AI 活群，但关键是每个账号都要发言
- output: L2 标准流程，设计完成并投递 dev；产品裁决为复用 `group_ai_chat`，新增 `all_accounts_daily` 覆盖模式，不新增独立 task_type
- evidence: `docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/ai-group-all-accounts-daily-coverage-prd.md`、`docs/00-index/project-dataflow-index.md`
- decision: production_related=false，release_gate_required=true，production_verification_required=false
- next_agent: dev
- unresolved: dev 已回传 Development Complete 并真实投递 QA；等待 QA 独立验收

## 2026-06-28 AI 活群全账号日覆盖模式开发完成通知

- message_id: 2026-06-28-ai-active-all-accounts-dev-complete-product-001
- action: 接收 dev Development Complete，确认下一阶段应由 QA 独立验收
- input: dev 已实现 `all_accounts_daily`、1-2 条日覆盖、24 小时固定窗口、Web / TG Bot 设置和 Planner payload 审计
- output: 状态推进为 `ready_for_validation`，current_agent=qa；product 不做产品接受，不声明完成
- evidence: dev 回传 E2：compileall passed；目标后端测试 16 passed, 2 deselected；frontend build passed；git diff --check passed
- decision: release_gate=pending，product_acceptance=pending，production_verification=unproven
- next_agent: qa
- unresolved: 等待 QA 回传 `qa_pass` / failed / blocked 后，product 再按验收模板做 product acceptance

## 2026-06-28 AI 活群全账号日覆盖模式返工完成通知

- message_id: 2026-06-28-ai-active-all-accounts-dev-to-product-rework-complete-001
- action: 接收 dev 对 QA 返工项的完成通知，确认仍由 QA 独立复验
- input: coverage remaining 优先级高于 memory priority；补齐详情 coverage 投影和 Web 展示；新增 `account_coverage.py`
- output: 状态保持 `ready_for_validation`，handoff 指向 QA 返工完成消息；product 不做提前接受
- evidence: dev 回传 E2：compileall passed；返工最小回归 2 passed；定向后端集合 19 passed, 2 deselected；frontend build passed；git diff --check passed
- decision: release_gate=pending，product_acceptance=pending，production_verification=unproven
- next_agent: qa
- unresolved: 等待 QA 对返工范围回传 `qa_pass` / failed / blocked

## 2026-06-28 AI 活群全账号日覆盖模式第二轮返工完成通知

- message_id: 2026-06-28-ai-active-all-accounts-dev-to-product-rework-complete-002
- action: 接收 dev 对第二轮 QA 阻断项的完成通知，确认仍由 QA 独立复验
- input: manual / auto `messages_per_round` 均保持本轮 turn 上限；全账号日覆盖不再用 coverage 抬高 `turn_count`
- output: 状态保持 `ready_for_validation`，handoff 指向 QA 第二轮返工完成消息；product 不做提前接受
- evidence: dev 回传 E2：compileall passed；QA 阻断核心回归 4 passed；定向后端集合 21 passed, 2 deselected；frontend build passed；git diff --check passed
- decision: release_gate=pending，product_acceptance=pending，production_verification=unproven
- next_agent: qa
- unresolved: 等待 QA 对第二轮返工范围回传 `qa_pass` / failed / blocked

## 2026-06-28 AI 活群全账号日覆盖模式产品验收

- message_id: 2026-06-28-ai-active-all-accounts-product-acceptance-001
- action: 对 QA 第二轮 `qa_pass` 的 AI 活群全账号日覆盖模式做 Product Acceptance，并真实投递 dev 进入 Release Gate
- input: 2026-06-28-ai-active-all-accounts-qa-to-product-002
- output: product_accepted
- evidence: QA E2 覆盖 manual / auto `messages_per_round` 不被 coverage 抬高、未覆盖账号在 Turn 上限内优先、详情投影和 blocked reasons 仍可见；目标测试和 frontend build 通过
- decision: 产品接受 E2 范围；release_gate=pending，production_verification_required=false
- next_agent: dev
- unresolved: 等待 Release Gate；Release Gate pending 时不能写发布完成

## 2026-06-28 租户 TG Bot 线上无响应 Incident 产品设计

- message_id: 2026-06-28-tenant-tg-bot-webhook-product-design-001
- action: 将 prod-diagnosis 的 L3 线上无响应 Incident 转为 Product Design Complete 和 dev 修复任务
- input: 线上 Bot Token 和 Admin Chat ID 已配置，真实 Telegram `/start`、`/admin` 已读但无 bot 回复；初步根因为未注册 Telegram webhook
- output: L3 标准流程，要求 dev 补齐 `setWebhook/getWebhookInfo/deleteWebhook`、状态回写、前端可见错误和命令可见回复
- evidence: prod-diagnosis E4_partial 用户路径；PRD / 专项设计 / 数据流转索引已补 webhook 注册口径
- decision: production_related=true，release_gate_required=true，production_verification_required=true
- next_agent: dev
- unresolved: 等待 dev ACK 和 Development Complete；QA 后必须回 product，再交 prod-diagnosis 做生产复验

## 2026-06-28 租户 TG Bot webhook 修复开发完成通知

- message_id: 2026-06-28-tenant-tg-bot-webhook-dev-to-product-complete-001
- action: 接收 dev Development Complete，确认下一阶段由 QA 独立验收
- input: dev 已补齐 `setWebhook -> getWebhookInfo`、refresh/delete API、Web 状态展示、命令可见回复、非管理员拒绝和审计
- output: 状态推进为 `ready_for_validation`，current_agent=qa；product 不做产品接受，不声明生产恢复
- evidence: dev 回传 E2：compileall passed；backend targeted 23 passed, 8 deselected；frontend build passed；git diff --check passed
- decision: release_gate=pending，product_acceptance=pending，production_verification=required_after_release_gate
- next_agent: qa
- unresolved: 等待 QA 回传 `qa_pass` / failed / blocked；QA 后 product acceptance，Release Gate 后必须回 prod-diagnosis 做 E4 复验

## 2026-06-28 租户 TG Bot webhook 产品验收

- message_id: 2026-06-28-tenant-tg-bot-webhook-product-acceptance-001
- action: 对 QA `qa_pass` 的 TG Bot webhook 注册修复做 Product Acceptance，并真实投递 dev 进入 Release Gate
- input: 2026-06-28-tenant-tg-bot-webhook-qa-to-product-001
- output: product_accepted
- evidence: QA E2 覆盖 setWebhook/getWebhookInfo、状态可见、test-message 出站语义、Web 刷新/删除、命令可见回复、非管理员拒绝、文档和索引
- decision: 产品接受 E2 范围；L3 仍不能关闭，release_gate=pending，production_verification_required=true
- next_agent: dev
- unresolved: 等待 Release Gate / 部署；部署后必须交 prod-diagnosis 做真实 Telegram `/start`、`/admin` E4 复验
