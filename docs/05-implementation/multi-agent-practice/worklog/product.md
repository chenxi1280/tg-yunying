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
