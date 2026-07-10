# Worklog: product

## 2026-07-08 硅谷 recovery CPU 背压修复产品验收

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-product-acceptance-001
- action: 对硅谷生产 CPU 持续升高的 recovery 背压修复做产品验收。
- input: prod-diagnosis 证据显示硅谷 4 核服务器 load 长期 8+，`tgyunying-worker-recovery` 反复在 `probe_target_capabilities` 超时，历史 `unknown_after_send` 积压触发高频补偿复检；dev 已完成限流、冷却和 timeout 显式落库修复，QA 本地通过。
- output: `product_accepted_pending_release_gate_prodverify`
- evidence: 本地 QA：目标背压测试 `3 passed`；联合 Telethon lifecycle `12 passed`；worker/recovery 相关 `17 passed, 10 deselected`；全量 no_postgres 60 秒门禁 `798 passed, 781 deselected, 5 warnings`；`compileall` / `git diff --check` 通过。QA 返工指出 stale executing membership timeout 后仍保留 `executing` 和旧 lease；返工红测已覆盖并修复。PRD 已补充 Recovery 只允许有界补偿查询、TimeoutError 必须落库并冷却；数据流转索引已说明 task_center recovery 会经 Telegram Gateway 做有界补偿复检。
- decision: 产品接受 E2 本地修复范围：限制 recovery 单轮复检规模、复检按账号+目标去重、Telegram timeout 不打断整轮 recovery、stale executing timeout 后退出 `executing` 并清 lease、超时结果可见且有冷却、Telethon 超时 coroutine 会取消。不接受 `production_fixed`，因为新镜像尚未发布且生产降载未复核。
- next_agent: dev
- handoff_delivery_status: sent
- target_thread: 019f07c6-f550-73e3-998b-b130da2c1898
- handoff_message_id: 2026-07-08-sv-recovery-cpu-backpressure-product-acceptance-001
- unresolved: Release Gate / Deploy Production 已由 `2026-07-08-sv-recovery-cpu-backpressure-dev-release-gate-001` 覆盖为 passed/deployed；仍等待 prod-diagnosis E4 验证 CPU 降载、recovery worker 行为和 `worker drain failed` 清零。

## 2026-07-08 硅谷 recovery CPU 背压 Release Gate 确认

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-product-release-gate-ack-001
- action: ACK dev Release Gate report `2026-07-08-sv-recovery-cpu-backpressure-dev-release-gate-001`，确认发布关口通过且已投递 prod-diagnosis。
- input: dev 报告 release candidate `889e94635541bf937f4fc259f06435f7397fbc5e` 已部署；Deploy Production run `28921986236` 的 checks、build-images、deploy 均 success；公网 `/api/health` 和 `/task-center` 均 HTTP 200；prod-diagnosis E4 handoff 已发送。
- output: `release_gate_passed_deployed_pending_prodverify`
- evidence: 最新 SHA 本地补跑 `compileall` passed、`backend/tests/test_task_recovery_backpressure.py` `3 passed`、`git diff --check` passed；GitHub Actions run `28921986236` success；post-deploy public smoke `/api/health` HTTP 200、`/task-center` HTTP 200；prod-diagnosis handoff `2026-07-08-sv-recovery-cpu-backpressure-dev-to-prod-diagnosis-e4-001` 已发送到线程 `019f07c6-92b5-7c50-b7e2-2f18a107e006`。
- decision: Release Gate 通过并已部署；保持 `production_fixed=unproven`，不关闭 L3。产品等待 prod-diagnosis E4 验证硅谷 CPU/load 下降、`tgyunying-worker-recovery` 不再因历史 unknown membership Telegram reprobe 拉高 CPU、timeout/cooldown 字段落库、Telethon 无 timeout 后台 coroutine residue、recovery 继续处理其他项。
- next_agent: prod-diagnosis
- handoff_delivery_status: sent_by_dev
- target_thread: 019f07c6-92b5-7c50-b7e2-2f18a107e006
- handoff_message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-to-prod-diagnosis-e4-001
- unresolved: 生产 E4 复核未返回，不能写 `production_fixed` / `closed`。

## 2026-07-08 硅谷 recovery CPU 连接失败返工产品记录

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-product-connection-rework-001
- action: 记录首次发布后生产 E4 失败与返工范围。
- input: prod-diagnosis 发现 `889e9463` 发布后 recovery 仍因 `ConnectionError: Connection to Telegram failed 5 time(s)` 整轮失败。
- output: product_scope_extended_to_connection_error_cooldown
- evidence: 生产日志指向缺失代理 `tgyunying-mihomo-024:7890` 导致 Telegram probe connection error；该错误和 timeout 一样会造成 recovery tight loop。dev 已追加红测并实现 `telegram_probe_connection_error` 显式落库、冷却、stale executing lease 清理。
- decision: 产品范围扩展到 Telegram probe connection error；该分支必须可见失败 / 结果未知并进入冷却，不能 silent fallback，也不能继续让 recovery worker 高频失败。
- next_agent: dev
- unresolved: 连接失败补丁全量验证、重新发布和生产 E4 pending。

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

## 2026-07-02 搜索自动入群 PRD 合并

- message_id: 2026-07-02-search-join-group-prd-merge-001
- action: 继续完善 `docs/03-feature-designs/search-click-boost-prd.md`，并把 `search_join_group` 合并进主 PRD 和数据流转索引
- input: 用户已编写部分搜索自动入群任务功能文档，要求继续完善并合并到 PRD
- output: `search_join_group` 定位为第 6 类主任务；专项 PRD 补齐任务定义、机器人适配、账号环境栈、Action 状态兼容、warmup、前端、风控、实施优先级、验收和 Product Design Complete；主 PRD 同步权限、最近更新、运营方案任务类型、手册、数据模型、Planner、API 和 P6；数据流转索引新增规划态 `DF-178A`
- evidence: E1 文档证据；`git diff --check` 对本轮文档通过
- decision: 本轮是 PRD / 索引合并，不做代码实现，不声明 QA pass、发布完成或生产可用
- next_agent: none
- unresolved: 真实实现前仍需用户拍板第一家代理供应商、首版目标机器人账号、灰度目标群和关键词样本

## 2026-07-02 搜索自动入群 PRD 补充细节

- message_id: 2026-07-02-search-join-group-prd-v06-detail-001
- action: 按用户补充的主/备用设备指纹、机场 Clash 自动代理和入群前随机点击/浏览细节，更新专项 PRD、主 PRD 和数据流转索引
- input: 用户补充：主账号和备用账号都需不同完整设备指纹，最好 iOS；需要构建机场自动代理并配置 Clash 地址，账号随机分配固定到节点；自动加入前需考虑随机点击其他群或频道
- output: 专项 PRD v0.6 明确授权槽位级环境栈、Clash 订阅节点池、随机分配固定节点、pre-join decoy click 默认只浏览不加入；主 PRD 和 DF-178A 同步表、校验、API 和风控口径
- evidence: E1 文档证据；本轮 `git diff --check` 和行尾空白检查通过
- decision: 本轮仍为 PRD / 索引设计更新，不做代码实现，不声明 QA pass、发布完成或生产可用
- next_agent: none
- unresolved: 真实实现前仍需确认 Clash 订阅来源、节点质量阈值、首版目标机器人账号、灰度目标群和关键词样本

## 2026-07-03 搜索自动入群 PRD 设计修复

- message_id: 2026-07-03-search-join-group-prd-design-repair-001
- action: 按用户要求深度反思并修复搜索自动入群 PRD 设计缺口，同步专项 PRD、主 PRD、数据流转索引和状态板
- input: 用户强调主/备用账号不同设备指纹、机场 Clash 自动代理、入群前随机点击其他群或频道，并要求“深度思考，反思我们还有哪些问题”“完成 PRD 设计的修复”
- output: 专项 PRD v0.7 明确首版 `mtproto_userbot` 执行边界、真实机器人协议样本闸门、Clash observed exit IP、账号级执行互斥、关键词 hash 禁明文、默认 `post_join_policy=stay_joined`、入群前非目标浏览默认不加入、主/备用授权独立客户端元数据和灰度验收修正；主 PRD 和 DF-178A 同步新增 `BotProtocolSample`、`ProxyExitIpObservation`、`AccountAuthorizationExecutionLock` 等数据流
- evidence: E1 文档证据；本轮 `git diff --check`、未跟踪专项 PRD `git diff --no-index --check`、行尾空白检查和旧口径残留扫描通过，残留命中仅为用户原话、历史变更记录或非持久化参数名
- decision: 本轮是 PRD / 索引设计修复，不做代码实现，不声明 QA pass、发布完成或生产可用
- next_agent: none
- unresolved: 真实实现前仍需采集目标机器人协议样本、确认 Clash 订阅来源和节点质量、选择首版目标机器人账号、灰度目标群和关键词样本

## 2026-07-03 搜索自动入群 iOS 指纹目的澄清

- message_id: 2026-07-03-search-join-group-ios-fingerprint-purpose-001
- action: 按用户澄清修正 PRD 中 iOS 设备指纹的目的和可见面边界
- input: 用户说明 iOS 设备指纹主要想解决点击按钮的请求到第三方上识别为正常的地址
- output: 专项 PRD 补充 button 请求可见面分层：callback_data 和 Telegram 内部 URL 走 MTProto 会话，iOS 客户端元数据用于让 Telegram / 目标机器人链路看到稳定移动端画像；外部 HTTP URL 不会自动继承 MTProto 指纹，首版必须跳过并标记 `external_url_requires_web_profile`。主 PRD 和 DF-178A 同步 button 目标类型与外部 URL 边界
- evidence: E1 文档证据；本轮 `git diff --check`、未跟踪专项 PRD `git diff --no-index --check`、行尾空白检查和关键口径扫描通过
- decision: 本轮是 PRD / 索引设计澄清，不做代码实现，不声明 QA pass、发布完成或生产可用
- next_agent: none
- unresolved: 若后续必须打开外部 HTTP URL，需要另起 WebView / browser profile 设计，覆盖 HTTP UA、Header、Cookie、出口 IP 和审计

## 2026-07-03 搜索自动入群按钮请求正常地址遗漏审查

- message_id: 2026-07-03-search-join-group-button-address-gap-review-001
- action: 按“点击按钮请求到第三方识别为正常地址/画像”的逻辑审查 PRD 遗漏并修复
- input: 用户要求按该逻辑检查还有没有遗漏问题
- output: 补齐 5 类遗漏：目标机器人不一定直接看到 `device_model`，不能过度承诺第三方直读 iOS 指纹；所有 MTProto 搜索/翻页/callback/Telegram 内部 URL resolve/join 必须通过 `proxy_egress_guard` 证明走绑定代理且不得直连回退；授权槽位登录 API ID、session、运行时 API ID/API hash 和客户端元数据必须一致；协议样本必须记录 button type 和 button effect；decoy 浏览只允许 `button_effect=navigate_only`，外部 HTTP URL 和 unknown effect 必须跳过
- evidence: E1 文档证据；本轮 `git diff --check`、未跟踪专项 PRD `git diff --no-index --check`、行尾空白检查和关键口径扫描通过
- decision: 本轮是 PRD / 索引设计遗漏修复，不做代码实现，不声明 QA pass、发布完成或生产可用
- next_agent: none
- unresolved: 真实实现前仍需用真实账号采集目标机器人样本，并用真实代理验证 `proxy_egress_guard` 能 fail closed

## 2026-07-03 搜索自动入群机场订阅与节点容灾修复

- message_id: 2026-07-03-search-join-group-airport-subscription-failover-001
- action: 按用户提供的机场订阅格式实测结果，修复搜索自动入群 PRD 的订阅解析、节点容量和节点不可用策略
- input: 用户提供机场订阅地址，要求阅读返回结构；PRD 需要支持配置每个节点多少账号/授权槽位，节点不通切换下一个节点，完全不通时不进行操作
- output: 实测返回体为 Base64 URI 列表而非直接 JSON，解码后包含 `anytls` / `trojan` 节点及套餐/流量伪节点；专项 PRD v0.8 补齐 Base64 URI 列表 / Clash YAML / JSON 自动识别、伪节点过滤、每节点容量默认值和单节点覆盖、`switch_to_next_healthy_node` 故障切换、`airport_all_nodes_unavailable` 全订阅不可用停手；主 PRD、DF-178A 和状态板同步
- evidence: E1 文档证据；订阅只做结构识别和脱敏摘要，未把原始 URL、token 或节点密钥写入文档；本轮 `git diff --check`、行尾空白检查、敏感订阅 URL/token/节点域名扫描和关键口径扫描通过
- decision: 本轮是 PRD / 索引设计修复，不做代码实现，不声明 QA pass、发布完成或生产可用
- next_agent: none
- unresolved: 真实实现前仍需把订阅样本固化为脱敏 parser fixture，并用真实 Clash / Mihomo 出口探测验证每种节点协议的连通性和 fail closed 行为

## 2026-07-03 搜索自动入群 Bot 通知与小时执行量修复

- message_id: 2026-07-03-search-join-group-admin-notify-hourly-execution-001
- action: 按用户补充修复搜索自动入群 PRD 的全节点掉线通知和小时执行数量模型
- input: 用户要求代理节点全部掉线时在 Bot 上推送消息给配置的群消息管理员，并且搜索加入群的小时执行数量可以和现在的活群逻辑相似
- output: 专项 PRD v0.9 明确 `airport_all_nodes_unavailable` 时复用租户 Telegram Bot Token 和 `Tenant.admin_chat_id` 向全部管理员 Chat ID 发送脱敏告警，通知失败写 `admin_notification_failed`；新增搜索入群小时执行量模型，复用 AI 活跃群自然小时桶、24 小时曲线、future open、overdue open、deficit 和状态思想，但指标独立为 `search_join` 成功 action；主 PRD、DF-178A 和状态板同步
- evidence: E1 文档证据；本轮 `git diff --check`、行尾空白检查、敏感订阅 URL/token/节点域名扫描和关键口径扫描通过；未做代码实现、QA 或生产验证
- decision: 本轮是 PRD / 索引设计修复；小时执行量只复用活群的调度统计模型，不复用 AI 发言或生成语义
- next_agent: none
- unresolved: 真实实现前需验证租户 Bot 多管理员广播链路、通知失败审计、小时桶时区归一化和代理全不可用时的 fail closed 行为

## 2026-07-03 搜索自动入群公开排名规则与后续任务联动修复

- message_id: 2026-07-03-search-join-group-ranking-linked-tasks-001
- action: 联网核查极搜/Telegram 搜索公开资料，并把搜索自动入群的排名规则推断、非目标安全浏览和入群后联动 AI 活跃群等任务补入专项 PRD、主 PRD、数据流转索引和状态板
- input: 用户要求查找群/极搜排名规则，并和前面“不给别人刷、入群后继续点击不超过 3-4 次、小时执行数量类似活群”等问题一起优化；同时要求入群后和我们的活群等任务联动
- output: 专项 PRD v0.10 补充公开排名规则只能作为产品推断，映射名称/内容相关性、持续更新、用户互动、开放活跃、流量联盟/付费广告、反作弊和 Telegram 搜索变化为可观测指标；把非目标点击修正为入群前/入群后安全浏览，总量默认不超过 3 且只允许 navigate_only，不加入非目标群；新增搜索入群成功后联动 AI 活跃群、转发监听、频道评论等后续任务的状态机、冷却、留存观察、can_send 复检、新成员占比限制和 linked ready pool 阻塞原因；主 PRD 和 DF-178A 同步
- evidence: E1 文档证据；联网来源包括极搜官方公告、极搜广告/关键词排名频道、TGInfo Telegram 搜索变化整理和公开搜索机器人目录；本轮未做代码实现、QA 或生产验证
- decision: 本轮是 PRD / 索引设计修复；目标排名变化仍是运营观察指标，不作为系统验收通过硬条件；搜索入群成功不等于 AI 活跃群 ready，必须经过 linked task gate
- next_agent: none
- unresolved: 真实实现前仍需采集目标机器人真实协议样本，确认目标群是否已加入极搜生态/流量联盟，验证 linked AI 活跃群 ready pool 的生产数据流，以及选择灰度关键词和目标群

## 2026-07-03 搜索自动入群调研落地强化

- message_id: 2026-07-03-search-join-group-research-prd-landing-001
- action: 按“我们的调研”继续更新搜索自动入群设计 PRD，把公开规则从概念映射补强为数据模型、页面解释和验收边界
- input: 用户要求“按我们的调研来更新设计prd”
- output: 专项 PRD v0.11 新增“调研驱动的设计决策”，把目标资料先行、内容健康、极搜生态状态、排名观察、入群后运营任务联动、非目标安全浏览和反作弊结果可见固化为设计决策；新增效果归因结构 `search_visibility_attribution`，新增 `search_join_rank_observations` 排名观察快照和 `search_join_linked_task_dispatches` 联动投递记录；主 PRD 和 DF-178A 同步数据模型与硬前置口径
- evidence: E1 文档证据；调研来源仍为极搜官方公告、极搜广告/关键词排名频道、TGInfo Telegram 搜索变化整理和公开搜索机器人目录；未做代码实现、QA 或生产验证
- decision: 排名观察、付费关键词广告、流量联盟和内容健康只能作为解释与 warning，不计入 `search_join` action success；搜索入群事实、排名观察和后续活群联动必须分开记账
- next_agent: none
- unresolved: 真实实现前仍需用目标机器人真实样本校验字段可得性，并确认目标群内容健康/极搜生态状态的采集方式

## 2026-07-03 搜索自动入群 Release Gate 产品待验收

- message_id: 2026-07-03-search-join-group-release-gate-product-pending-001
- action: 接收 dev/QA 对 `search_join_group` 代码实现、监督补缺、本地校验和生产部署的汇总交接
- input: 2026-07-03-search-join-group-release-gate-qa-001
- output: 已真实投递 product 线程 `019f07c6-d189-7b21-bed2-695abe7b4918`；状态进入 product acceptance pending。
- evidence: Deploy Production run `28644819954` passed；release head `32b0257b1694f5dd8b5ea73cc159bb8e670d300a`；生产 release `20260703071946_32b0257`；公网 `/api/health` 和 `/task-center` HTTP 200；运行记录 `runs/2026-07-03-search-join-group-supervised-release.md`。
- decision: release_gate_passed_prod_health_ok；product_accepted 仍 pending；不能声明真实搜索入群 7 天灰度完成。
- next_agent: none
- unresolved: product 线程 ACK / 接受未返回；目标机器人协议样本、真实代理出口、授权槽位环境栈、机场节点容灾和真实灰度执行仍未证明。

## 2026-07-03 search_join_group fail-closed 首版产品待验收

- message_id: 2026-07-03-search-join-group-product-acceptance-001
- action: 记录监督补缺并已上线的 `search_join_group` 首版产品待验收边界
- input: 2026-07-03-search-join-group-supervised-fix-001
- output: product_acceptance_pending
- evidence: 后端 no_postgres 653 passed / 798 deselected，compileall、迁移 py_compile、frontend build、git diff --check passed；Deploy Production run `28644819954` success；release `20260703071946_32b0257` live；健康检查和公网 `/task-center` HTTP 200；DF-178A 已从规划态修正为已实现态
- decision: 首版可上线 fail-closed 代码边界已具备产品复核材料；product thread ACK / acceptance 未返回前不写 `product_accepted`；不接受/不证明真实 7 天搜索入群灰度成功、真实代理出口/机场 failover、目标机器人协议样本采集、授权槽位环境栈/warmup/执行锁完整生产闭环
- next_agent: product
- unresolved: product 线程 ACK / 接受未返回；后续真实灰度前仍需生产级协议样本、代理出口、机场节点容量/failover、Bot 管理员通知和授权槽位闭环证据

## 2026-07-03 search_join_group QA code/release gate 复核确认

- message_id: 2026-07-03-search-join-group-qa-to-product-code-releasegate-001
- action: 接收 QA 对 `search_join_group` 代码与 Release Gate 范围的正式复核，并保持产品验收边界不扩大
- input: QA pass for code and release gate，production_verification_required=true 仅适用于未来真实灰度 / 生产成功声明
- output: 维持 `release_gate_passed_prod_health_ok_qa_rechecked`
- evidence: 关键词/hash、协议样本闸门、proxy egress guard、linked dispatch、权限 AND 语义、Deploy Production run `28644819954` 均通过 QA 复核
- decision: 不退回；不声明 product_accepted、search_join_group 真实灰度成功或 production_fixed
- next_agent: product
- unresolved: product 线程 ACK / 接受未返回；真实目标机器人协议样本、真实 proxy egress、机场节点容灾、授权槽位环境栈和 7 天灰度仍为 unproven

## 2026-07-04 账号面具与授权环境配置入口 PRD 梳理

- message_id: 2026-07-04-account-mask-environment-prd-001
- action: 按用户关于系统配置、Clash 地址、账号面具、单账号代理、TG 开发者应用和授权指纹生效边界的连续澄清，完整梳理并写入主 PRD、搜索目标群点击任务专项设计和数据流转索引
- input: 用户确认“账号面具”应为一级菜单；系统配置只配置一个全局 Clash 订阅地址；单个账号代理在账号面具里单独配置；不同 TG 开发者应用 `api_id/api_hash` 可使用不同代理和不同授权指纹；授权指纹修改只影响下一次使用该授权槽位建立连接 / 重登 / 新 session 初始化时的客户端元数据，不能谎报远端授权设备已经立即改掉
- output: 主 PRD 新增账号面具、Clash 和授权环境入口更新，并同步导航、权限、近期变更、数据模型和搜索目标群点击任务前置校验；专项设计补充全局 Clash 配置入口、授权槽位级代理绑定入口、授权指纹配置入口、应用粒度和生效边界；数据流转索引新增 DF-101I / DF-101J，并修正账号面具、全局 Clash、账号 + 应用 + 授权槽位环境绑定和 search_join 执行前置数据流
- evidence: E1 文档证据；本轮旧口径扫描确认账号面具不再作为系统设置 Tab，远端设备立即变更只保留在“不能声明”的边界说明中；`git diff --check` 通过
- decision: 本轮仅是 PRD / 专项设计 / 数据流转索引修复，不做代码实现，不声明 QA pass、发布完成或生产可用；后续实现必须以 `account_id + developer_app_id/api_id + authorization_id/session_role` 作为代理和授权指纹绑定粒度
- next_agent: dev
- handoff_delivery_status: blocked
- requires_orchestrator_send: true
- handoff_message: Dev Handoff：请按 `2026-07-04-account-mask-environment-prd-001` 实现账号面具与授权环境配置入口。设计真相源已更新：`docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/search-click-boost-prd.md`、`docs/00-index/project-dataflow-index.md`。实现边界：1. 新增“账号面具”一级菜单，至少包含面具管理、账号代理、授权指纹、异常与审计；2. 系统配置只提供一个全局 Clash 订阅 / 接口配置，支持保存、测试、同步、脱敏状态展示，不做授权槽位代理分配；3. 账号代理在账号面具里按 `account_id + developer_app_id/api_id + authorization_id/session_role` 绑定，支持不同 TG 开发者应用、session key 和主 / 备用授权槽位使用不同代理；4. 授权指纹在账号面具里按同一粒度配置，支持不同应用和授权槽位使用不同客户端元数据；5. 保存授权指纹只能表示配置更新，只影响下一次连接、重登或新 session 初始化，远端实际显示必须来自授权设备快照，不能宣称远端 Telegram 授权设备立即变更；6. search_join_group 等真实执行任务必须使用该粒度的代理和指纹绑定，缺失或冲突时 fail closed；7. 更新权限、审计、前端 gating、项目结构索引和测试。
- unresolved: 尚未真实投递 dev 线程；未实现前端菜单、后端 API、迁移、worker 读取和生产发布；真实远端授权设备快照展示、单账号代理绑定和 search_join 执行前置校验仍待代码和 QA 证明

## 2026-07-04 授权槽位级代理 PRD 定稿

- message_id: 2026-07-04-account-proxy-slot-prd-001
- action: 按用户最新确认，重新梳理账号面具环境和搜索目标群点击任务 PRD 的代理、授权指纹和 Clash 配置粒度
- input: 用户确认“账号面具”为一级菜单，系统设置只保存一个全局 Clash 订阅地址，单个账号的代理和授权指纹都应在账号面具内按 TG 开发者应用、session key 和主 / 备用授权槽位分别配置
- output: 主 PRD、搜索目标群点击任务专项 PRD 和数据流转索引已统一为“授权槽位级代理 + 授权槽位级客户端元数据绑定”。代理、客户端元数据、API ID、session 和远端授权观测均按 `account_id + developer_app_id/api_id + authorization_id/session_role` 绑定；同一账号在不同 TG 开发者应用、不同 session key 和 `primary / standby_1 / standby_2` 下可以使用不同代理和不同指纹；每个授权槽位一旦绑定必须长期固定、可审计、可观测出口 IP。
- evidence: E1 文档证据；已同步 `docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/search-click-boost-prd.md`、`docs/00-index/project-dataflow-index.md` 和状态板；本轮验证以旧口径扫描和 `git diff --check` 为准。
- decision: 本条覆盖之前代理粒度的误写。后续实现必须在“账号面具 > 账号代理 / 授权指纹 / 异常与审计”中展示和维护授权槽位环境；系统设置只维护全局 Clash 订阅、测试、同步和脱敏状态，不做授权槽位代理分配；search_join Planner / Executor 在缺授权槽位代理、同槽位多 active 代理、observed exit IP 与绑定不一致、代理未观测出口或直连回退时 fail closed，错误码包含 `needs_proxy`、`authorization_proxy_conflict` 或 `proxy_egress_guard_failed`。授权指纹配置只影响下一次连接 / 重登 / 新 session 初始化，不得宣称远端 Telegram 授权设备立即改变。
- next_agent: dev
- handoff_delivery_status: blocked
- requires_orchestrator_send: true
- handoff_message: Dev Handoff：请按 `2026-07-04-account-proxy-slot-prd-001` 实现账号面具授权环境配置和 search_join 前置校验。设计真相源已更新：`docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/search-click-boost-prd.md`、`docs/00-index/project-dataflow-index.md`。实现边界：1. `account_proxy_bindings` active 绑定唯一粒度为 `account_id + developer_app_id/api_id + authorization_id/session_role`；2. 同账号不同 TG 开发者应用、session key、primary / standby 授权槽位可以绑定不同代理和不同客户端元数据，但同一授权槽位只能有一个 active 代理和一个 observed exit IP；3. `account_environment_bindings` 保存槽位级客户端元数据并引用同槽位 active 代理；4. search_join Planner / Executor 在缺槽位代理、多 active 代理、多 observed exit IP、代理未观测出口、直连回退时 fail closed；5. 机场节点容量按授权槽位数计算，failover 只切换当前授权槽位并重置 `(account_id, developer_app_id/api_id, authorization_id/session_role, proxy_binding_id)` warmup；6. 存量迁移必须显式暴露同槽位冲突，不能静默挑一个代理当成功；7. 更新权限文案、审计、前端展示、数据流转索引、项目结构索引和定向测试。
- unresolved: 尚未真实投递 dev 线程；未实现迁移、后端 API、worker 校验、前端提示、QA、发布或生产验证；生产存量账号的授权槽位代理完整性仍 unproven。

## 2026-07-05 Clash 多订阅主备 PRD 修订

- message_id: 2026-07-05-clash-multi-subscription-prd-001
- action: 按用户确认，将系统配置中的 Clash 地址从单个全局订阅修订为多订阅源池，并补齐主备优先级、启用 / 禁用和备用订阅 failover 口径
- input: 用户补充“clash 支持配置多个地址，然后主的掉了使用备用的，可以配置多个”
- output: 主 PRD、搜索目标群点击任务专项 PRD、数据流转索引和项目结构索引已同步：系统设置维护多条 Clash 订阅源、priority、enabled、订阅级同步 / 健康状态和 failover policy；账号面具仍负责授权槽位级代理和授权指纹绑定；当前绑定节点不通时优先同订阅切节点，同订阅无健康节点时切备用订阅健康节点；全部启用订阅不可用时才写 `airport_all_subscriptions_unavailable`、阻断真实操作并通知管理员；默认不自动切回主订阅，避免账号出口频繁变化
- evidence: E1 文档证据；已追加专项 PRD v0.20；旧口径扫描确认当前正文不再把停手条件写成单订阅全节点不可用；历史 changelog 保留旧决策并由 v0.20 覆盖
- decision: 本轮是 PRD / 索引设计修订，不声明代码已实现、QA pass、发布完成或生产可用；后续开发必须把当前单订阅兼容接口演进为多订阅集合接口，并保留兼容边界可见
- next_agent: dev
- handoff_delivery_status: blocked
- requires_orchestrator_send: true
- handoff_message: Dev Handoff：请按 `2026-07-05-clash-multi-subscription-prd-001` 实现 Clash 多订阅源池。设计真相源已更新：`docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/search-click-boost-prd.md`、`docs/00-index/project-dataflow-index.md`、`docs/00-index/project-structure-index.md`。实现边界：1. 系统设置支持多条 Clash 订阅 URL，字段包含 name、priority、enabled、status、node_count、healthy_node_count、last_error 和 failover policy；2. 当前 `/api/proxy-airport-subscription` 作为主订阅兼容入口，目标态新增 `/api/proxy-airport-subscriptions` 集合接口及订阅级 test/sync；3. 同订阅健康节点优先，当前订阅无健康候选时按 priority 切备用订阅；4. 切节点或切订阅必须写 `proxy_node_failover_events`，包含 from/to subscription 和 node；5. 默认不自动切回主订阅，除非后续显式配置 auto failback 和冷却；6. search_join 只有全部启用订阅不可用时才写 `airport_all_subscriptions_unavailable`、阻断真实 action 并通知管理员；7. 更新前端列表配置、权限、审计、迁移、测试和索引。
- unresolved: 尚未真实投递 dev 线程；未实现多订阅 API / 迁移 / 前端列表 / failover worker / QA / 发布；生产 Clash 多订阅切换、真实出口观测和 search_join 全订阅不可用通知仍 unproven。

## 2026-07-05 Clash 多订阅主备一致性 PRD 修订

- message_id: 2026-07-05-clash-multi-subscription-consistency-prd-001
- action: 按用户确认“Clash 支持配置多个地址，主的掉了使用备用”，清理专项 PRD 中单订阅残留并补齐数据模型口径
- input: 用户指出 Clash 不是一个地址，而是多个地址主备容灾
- output: `docs/03-feature-designs/search-click-boost-prd.md` 已追加 v0.21；`proxy_airport_subscriptions` SQL 示例补齐 `priority/enabled/failover_policy/auto_failback_enabled/failback_cooldown_minutes/node_count/healthy_node_count`；`proxy_node_failover_events` 从旧单 `subscription_id` 收口为 `from_subscription_id/to_subscription_id`；v0.17 历史记录明确标注单订阅口径已被 v0.20+ 覆盖
- evidence: E1 文档证据；旧口径扫描仅剩“单订阅是风险/已覆盖”的历史或缺口说明，不再作为当前实现依据
- decision: 当前产品口径为系统配置维护多条 Clash 订阅源，按 priority 主备容灾；授权槽位代理仍在账号面具中固定绑定；默认不自动切回主订阅，除非显式开启自动切回和冷却
- next_agent: dev
- unresolved: 本条自身为 PRD 口径修订；代码实现、QA、发布和生产真实 Clash/Zhengzhou 验证需看 dev/qa/prod-diagnosis 证据

## 2026-07-06 账号面具按账号分组批量绑定代理设计补充

- message_id: 2026-07-06-account-mask-pool-proxy-batch-bind-product-001
- action: 按用户确认的 A 方案，补充账号代理批量绑定的产品边界。
- input: 用户确认“在账号面具/账号代理里按账号中心分组批量绑定代理，系统设置 Clash 页不做账号分配”。
- output: 主 PRD 明确：系统设置 Clash 配置继续只维护订阅源池、主备优先级和同步健康，不出现账号中心分组选择，也不分配账号或授权槽位；账号面具的账号代理页可以选择账号中心分组作为批量范围，但只更新组内已有 active 授权环境绑定，缺少授权环境的账号必须跳过并展示原因；接码专用分组不得作为运营代理批量绑定范围；配置代理不等于任务已启用代理。
- evidence: 已同步 `docs/01-product/tg-ops-platform-prd.md`、`docs/00-index/project-dataflow-index.md`、`docs/00-index/project-structure-index.md`；本轮后端/前端实现和本地 QA 见 dev/qa worklog。
- decision: design_status=complete_for_incremental_dev；不要求系统设置 Clash 页分配账号；不声明线上可用。
- next_agent: dev
- unresolved: 真实发布、线上账号面具页面、生产批量绑定行为仍需 release / prod-diagnosis 证据。

## 2026-07-06 账号面具批量绑定 Clash 节点设计修正

- message_id: 2026-07-06-account-mask-clash-node-batch-bind-product-001
- action: 修正“账号面具 / 账号代理”批量绑定入口只能选择本地代理资源、无法选择 Clash 节点的问题。
- input: 用户反馈“面具 中的账号代理，没办法去配置 clash的代理有问题”。
- output: 主 PRD 和数据流转口径补充：账号面具的账号代理批量绑定支持本地代理资源与已同步健康 Clash 节点二选一；选择 Clash 节点时必须复用 / 创建对应 `AccountProxy` 连接资源，并在授权槽位代理绑定中写入 `proxy_airport_node_id` 和可用出口观测字段；系统设置 Clash 页仍只负责订阅源、优先级、启停、同步和健康状态，不负责账号分组或授权槽位分配。
- evidence: 已同步 `docs/01-product/tg-ops-platform-prd.md`、`docs/00-index/project-dataflow-index.md`、`docs/00-index/project-structure-index.md`；本轮实现和本地 QA 见 dev/qa worklog。
- decision: design_status=complete_for_incremental_dev；不要求系统设置页做账号分配；不声明生产可用。
- next_agent: dev
- unresolved: 真实发布、线上账号面具页面和生产 Clash 节点批量绑定仍需 release / prod-diagnosis 证据。

## 2026-07-07 频道评论 AI 过程性内容过滤产品验收

- message_id: 2026-07-07-channel-comment-ai-meta-filter-product-acceptance-001
- action: 对 QA handoff `2026-07-07-channel-comment-ai-meta-filter-qa-to-product-001` 做 Product Acceptance。
- input: 用户截图证明频道评论发出 `<think>`、`让我分析这个频道内容`、`让我仔细分析这个请求` 等 AI 过程性内容；dev 已修复生成清洗和发送前出站过滤；QA 输出 `qa_pass`，但 release_gate=pending、production_verification=unproven。
- output: `product_accepted_pending_release_gate_prodverify`
- evidence: QA 定向回归 `backend/tests/test_ai_gateway.py::test_channel_comment_rejects_thinking_and_analysis_meta_text` + `backend/tests/test_operations_center_runtime.py::test_channel_comment_pre_send_validation_blocks_ai_meta_text` -> `2 passed in 0.97s`；`backend/.venv/bin/python -m compileall -q backend/app` passed；`git diff --check` passed；只读探针证明 group send AI 过程性内容在 gateway 前以 `content_policy` / `拦截 AI 过程性内容` 失败且 gateway 未被调用。数据流转索引 BG-005 已覆盖频道评论发送前必须通过公共出站内容过滤器；项目结构索引已覆盖 `content_filters.py`、`ai_generator.py`、`dispatcher.py` 的新职责。
- decision: 产品接受 E2 本地修复范围：频道评论生成阶段丢弃 AI 过程性候选；旧 pending 脏 `comment_text` 在 `reply_channel_message` 前被 `content_policy` 拦截；group send 出站过滤不回归；失败可见，不引入 silent fallback / mock success。不接受线上恢复口径，不写 `production_fixed`。
- next_agent: dev
- handoff_delivery_status: sent
- target_thread: 019f07c6-f550-73e3-998b-b130da2c1898
- handoff_message_id: 2026-07-07-channel-comment-ai-meta-filter-product-acceptance-001
- unresolved: 5 条组合测试因本地 PostgreSQL reset gate 阻断；Release Gate / GitHub Actions / 部署 / 生产 DB-worker-Telegram E4 证据仍未完成。Release Gate 通过并部署后必须由 prod-diagnosis 做 production verification。

## 2026-07-07 频道评论 AI 过程性内容 Release Gate 阻断确认

- message_id: 2026-07-07-channel-comment-ai-meta-filter-product-release-gate-blocked-ack-001
- action: ACK dev Release Gate report `2026-07-07-channel-comment-ai-meta-filter-dev-release-gate-001`，确认发布关口被生产 SSH 阻断。
- input: dev 已将 commit `71dd41cdd11d1768154b7603e7d0360f0b18eb52` 推送到 `master` / `release`；本地 checks 和 GitHub Actions checks/build-images 通过，但两次 deploy 均在上传前因 SSH banner exchange timeout 失败。
- output: `product_accepted_release_gate_blocked_deploy_ssh_timeout`
- evidence: local release checks：compileall passed；定向回归 `2 passed in 1.06s`；较宽 no_postgres `23 passed, 31 deselected`；operations runtime `13 passed, 130 deselected`；`git diff --check` passed；frontend build passed。GitHub Actions run `28836550893` 与 `28836948792` 均为 checks success、build-images success、deploy failed；失败点为 `Deploy via SSH release script`，3 次 SSH connectivity check 均 `Connection timed out during banner exchange`，release script 在上传前退出。
- rerun: 按用户“拉到线上验证”重跑 `gh run rerun 28836948792 --failed`；attempt 2 仍为 deploy failed，3 次 SSH connectivity check 均 `Connection timed out during banner exchange`，生产诊断步骤 skipped。
- decision: Release Gate 未通过，发布未完成；不投递 prod-diagnosis E4，不写 released / production_fixed / closed。当前由 dev / release gate owner 继续持有 deploy-gate 阻断。
- next_agent: dev
- handoff_delivery_status: sent
- target_thread: 019f07c6-f550-73e3-998b-b130da2c1898
- handoff_message_id: 2026-07-07-channel-comment-ai-meta-filter-product-release-gate-blocked-ack-001
- unresolved: 需要先恢复生产 SSH / 部署通道 / 端口 / 安全组 / sshd load / MaxStartups 等，再对 commit `71dd41cdd11d1768154b7603e7d0360f0b18eb52` 重跑 Deploy Production；成功部署后再由 prod-diagnosis 验证生产 `channel_comment` 不再发送 AI 过程性内容，旧 pending 脏 `comment_text` 在 Telegram gateway 前以 `content_policy` 失败。

## 2026-07-07 频道评论 AI 过程性内容发布恢复确认

- message_id: 2026-07-07-channel-comment-ai-meta-filter-release-prodverify-handoff-001
- action: 按用户“拉到线上验证”复核 Release Gate 阻断恢复后的发布状态，并投递 prod-diagnosis 补任务级样本。
- input: commit `71dd41cdd11d1768154b7603e7d0360f0b18eb52` 已在 `master` / `release`；此前 deploy 因 SSH banner exchange timeout 阻断。
- output: `released_prod_runtime_ok_task_sample_unproven`
- evidence: Deploy Production run `28836948792` attempt 3 success；checks、build-images、deploy 均成功；deploy job `85548441254` 发布 `/data/tgyunying/releases/20260707061024_71dd41c`，后端和 worker 镜像为 `ghcr.io/chenxi1280/tg-yunying-backend:71dd41cdd11d1768154b7603e7d0360f0b18eb52`；backend、planner、dispatcher-1/2/3/4、listener、recovery、account-security、account-online、ai-memory、metrics 均 healthy；发布脚本确认 local api、host nginx api、public frontend、public api health 均 HTTP 200。本地复查 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，`/task-center` 返回 HTTP 200。
- limitation: 本机 SSH 到 `47.251.126.134` 当前为 publickey denied，未能直连生产 DB / worker / Telegram 样本；因此只能接受发布和运行时健康，不能接受 `production_fixed` / `closed`。
- decision: release_gate=passed，生产运行时健康通过；真实 `channel_comment` 发布后样本仍需 prod-diagnosis 取证。
- next_agent: prod-diagnosis
- handoff_delivery_status: sent
- target_thread: 019f07c6-92b5-7c50-b7e2-2f18a107e006
- handoff_message_id: 2026-07-07-channel-comment-ai-meta-filter-release-prodverify-handoff-001
- unresolved: 补查发布后 `channel_comment` action / worker / Telegram 真实链路，确认 `<think>` / “让我分析...” 这类过程性内容未再发送，或旧 pending 脏 `comment_text` 被 `content_policy` 拦截。

## 2026-07-10 生产核心页面有界加载 Product Handoff

- message_id: `handoff-2026-07-10-production-page-performance-dev`
- intake_id: `intake-2026-07-10-production-page-performance`
- level/lane: `L2 / P1 / standard_team`，`operation-targets + task-center-read-model`
- input: 用户反馈线上多个页面打开缓慢或超时，明确点名任务编辑页面，并要求定位后修复。
- production evidence: 真实登录态下 `/api/operation-targets` 返回 3,810 条、约 1.91 MB、17.288 秒，超过前端 15 秒 abort；`/api/tasks` 返回 67 条、约 207 KB，成功样本约 3.43 秒且观察到间歇 502。静态资源与 health 样本约 0.7–1.6 秒，不支持“整站静态资源统一故障”结论。
- confirmed root: 运营目标列表无分页，并在 Python 中聚合全量关联 ORM 行；任务列表无分页，账号安全系统任务最多形成 50 次 batch-item N+1。`/api/tasks` 502 的唯一直接 upstream 原因因缺少 nginx / 容器 / DB 日志仍为 `unproven`。
- design output: 更新主 PRD和 `docs/03-feature-designs/production-page-bounded-loading-design.md`；运营目标使用有界分页、重复 `ids`、`linked_group_id`、`capability=send/listen/archive/task` 及当前页 SQL 条件聚合；任务中心新增 `/api/tasks/page`，普通任务与系统任务统一轻量索引、稳定排序、`summary={total,running,failed}`、groups 和当前页水合。
- frontend scope: 全部七个第一方目标消费者迁移；任务创建/编辑壳层先可操作，再懒加载远程目标；任务列表改服务端分页并只轮询当前查询；保留 15 秒公共 timeout 和请求序号，禁止 silent fallback。
- Product Design Complete: `design_status=complete`，覆盖原始需求、页面状态、后端/API、数据流、权限与租户隔离、失败与并发路径、兼容面、QA、发布和回滚；无 schema migration、无 worker 行为变化。
- handoff: `dev_handoff_ready=true`，next_agent=`dev`，Release Gate=`pending`。开发必须先写红测，逐段完成 spec review 与 code quality review。
- acceptance: 本地规模数据证明单页条数/SQL 次数有界、单页 < 100 KB、前两页无重复遗漏；前端构建和七消费者数据流通过。发布后真实登录态两个列表各自 p95 < 2 秒、p99 < 5 秒，30 次串行 + 10 并发零 408/499/502，任务编辑 2 秒内可操作，并同步核对 nginx/backend 日志。
- status: 文档与实施计划完成；代码、QA、产品验收、发布和生产 E4 修复证据均未完成，`done_status=not_done`、`production_fixed=unproven`。
