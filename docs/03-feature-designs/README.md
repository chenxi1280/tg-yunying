# 03-feature-designs：专项 PRD 与设计

本目录保存专项能力文档。本轮实现只以标记为“当前”的总合同、闭合合同和任务专项为准；主 PRD/主数据流索引已同步当前产品合同，项目结构索引仍须在代码实现完成后按真实入口更新。`historical_do_not_implement` 只保留事故和取证，不得进入开发或 QA。

| 文件 | 定位 |
| --- | --- |
| [production-release-batch-single-deploy-prd.md](production-release-batch-single-deploy-prd.md) | **2026-08-30 Product Design Complete、待 workflow QA**：取消每次 push release 自动生产部署；master 汇总、release 一次冻结、从 release ref 显式 dispatch，并在完整 CI 前校验 master/release/checkout 同一不可变 SHA |
| [ai-group-chat-quality-and-token-optimization-prd.md](ai-group-chat-quality-and-token-optimization-prd.md) | **2026-08-27 Product Design Complete、生产 7 大群 12 场景基准通过、待代码与配置落地**：AI 活群质量彻底根治、行业暗语拟真注入、机器人广告垃圾清洗、后置拦截调优、7 大任务专属话题库与 Token 控耗闭环方案 |
| [github-actions-release-performance-optimization-prd.md](github-actions-release-performance-optimization-prd.md) | **2026-08-27 两个 P0 本地修复、定向 QA 与 0168 幂等回归通过、标准 PG blank-DB 全链待 Actions 证明**：不减少发布次数与测试覆盖；Backend 3+2 确定性分片、三镜像并行构建、Python 依赖层缓存、frontend 历史 hash hard-link 复用；takeover 识别 fact-first quantity-only 合同并以常数级批量事实快照完成 preview/drift/apply 复核 |
| [account-batch-post-login-full-initialization-prd.md](account-batch-post-login-full-initialization-prd.md) | **2026-08-27 Product Design Complete、本地实现与定向 QA 通过、待发布/生产验证**：批量登录 `new_account/already_authorized/relogin` 统一 create-or-attach账号级 durable full-init；真实 fixed 2FA、姓名/头像 readback、ABC owner去重和完整 A/B/C/E4 成功边界 |
| [account-security-hardening-design.md](account-security-hardening-design.md) | 账号安全、设备清理、托管 2FA、资料初始化 |
| [account-profile-group-style-initialization-prd.md](account-profile-group-style-initialization-prd.md) | **2026-08-19 新账号审计修正已实现、待发布/生产验证**：按精确批量登录 batch 集合中“本批新建且登录成功”的账号并集初始化名字+头像，排除已有账号重登，群样本仅形成匿名风格分布、不复制真实身份或头像，protected preview/apply/readback + 300 账号 E4 |
| [account-standby-auto-authorization-prd.md](account-standby-auto-authorization-prd.md) | 备用授权自动补齐专项 PRD |
| [account-malaysia-standby-session-dr-prd.md](account-malaysia-standby-session-dr-prd.md) | **v2.21 当前产品合同，Product Design Complete**：固定 A/SV primary、B/SV standby_1、C/MY standby_2；新增全部在线账号动态 N、A 唯一码源、A -> B -> C 编排、账号+B/C 三重守恒、同一 10 账号与 24 小时观察窗。既有迁移核心已部署，`complete_online_abc` 尚未实现 |
| [account-malaysia-standby-session-dr-implementation-contract.md](account-malaysia-standby-session-dr-implementation-contract.md) | **v2.21 规范性实施与验收合同**：冻结 API/schema、码源与 observer fence、B/C 资格边界、unknown stop、10 账号/全量 Release Gate；继续覆盖设备清理、SV-to-MY 迁移、双副本恢复、local activate/restore SV pair、任务代次、回滚和 DEV 交接 |
| [account-login-group-navigation-recovery-prd.md](account-login-group-navigation-recovery-prd.md) | **当前**：账号验证码 / QR / 2FA 登录 flow、已有账号重登分组语义、账号分组导航与服务端分页修复合同 |
| [account-batch-auto-login-prd.md](account-batch-auto-login-prd.md) | **本地已实现、定向 QA 通过、默认 mode=off、未发布/生产未证明**：批量自动登号专项（失败/超时/远端未解跳行，账号 UUID 接码备注映射，独立对账与 initial/correction 提醒，地址刷新和目标分组归一合同） |
| [login-user-permission-gating-prd.md](login-user-permission-gating-prd.md) | **当前**：平台登录账号按权限加载首屏资源，系统运行时诊断仅限 `system.view` |
| [task-fulfillment-classified-recovery-prd.md](task-fulfillment-classified-recovery-prd.md) | **当前总合同**：C1-C8 分类、Telegram 不可发送当日放弃、C2 连续 30 秒无提示通过、义务物化、独立搜索、并发生成与 Recovery |
| [task-fulfillment-contract-closure-prd.md](task-fulfillment-contract-closure-prd.md) | **当前闭合合同**：分阶段资源空闲即执行、唯一事实+单行 CAS、搜索持久 phase、prepared 新 Task直接 canary→route epoch切换→旧 Task异步删除、unknown 终态、单 Provider key、OCR 和 E4 验收 |
| [ai-group-all-accounts-daily-coverage-prd.md](ai-group-all-accounts-daily-coverage-prd.md) | `historical_do_not_implement`：旧冻结全账号分母方案，仅保留历史取证 |
| [ai-group-admission-quantity-slot-convergence-prd.md](ai-group-admission-quantity-slot-convergence-prd.md) | `historical_do_not_implement`：旧 ContentMix 数量槽方案，仅保留历史取证 |
| [ai-group-daily-fulfillment-remediation-prd.md](ai-group-daily-fulfillment-remediation-prd.md) | `historical_do_not_implement`：旧冻结分母日履约修复，仅保留历史取证 |
| [ai-group-daily-group-target-redesign-prd.md](ai-group-daily-group-target-redesign-prd.md) | **数量/覆盖 owner 当前参考，内容 fallback 受 v1.2 补正**：每日总量与动态必达账号继续有效；统一签到只属于未接管 legacy，current v2 禁止静态补量 |
| [production-due-backlog-containment-prd.md](production-due-backlog-containment-prd.md) | **2026-08-11 生产止血交接**：修复 current AI open owner 错计、AI/浏览当前 due 二次排期与跨日 pre-Gateway 积压；不替代长期 stable obligation/due-unit 接管 |
| [search-dispatch-release-fence-hotfix.md](search-dispatch-release-fence-hotfix.md) | **2026-08-19 `70523382` 已发布**：`034216e4` Stage B 真实收口 2 条搜索 Action且未重发；最终版统一 fact-first unknown 投影与 legacy deadline 收口，27 项定向 QA、两次 Release Gate、最终 SHA 搜索/群发目标守恒通过。普通 stale-worker 自然 E4 仍为 unproven |
| [hourly-random-pacing-and-ai-humanization-prd.md](hourly-random-pacing-and-ai-humanization-prd.md) | **v8 本地第一阶段实现并通过定向 QA、待生产证明**：四类 stable due、跨 Task source capacity、curve-aware pairwise gap、replacement headroom、逐 slot 守恒与 typed shortfall；禁止 0→1 和 current v2 静态签到补量 |
| [ai-content-routing-and-quality-upgrade-prd.md](ai-content-routing-and-quality-upgrade-prd.md) | **v1.2 主产品合同、本地第一阶段实现完成**：task direction、adult route/mode、单调 context revision、可替换 WindowPlanSlot、MessageBrief claim-evidence、短 Prompt、逐 slot 质量链；shadow/canary 与 Telegram E4 仍未证明 |
| [ai-content-routing-and-quality-upgrade-runtime-contract.md](ai-content-routing-and-quality-upgrade-runtime-contract.md) | **v1.2 规范性运行附录、本地实现**：Provider purpose/priority、legacy selector 迁移、状态机/lease、数据模型、API/UI、逐 owner shortfall 与禁止 static fallback |
| [ai-content-routing-and-quality-upgrade-evaluation-release-contract.md](ai-content-routing-and-quality-upgrade-evaluation-release-contract.md) | **v1.2 规范性验收附录、脚本已实现**：真实分层集、rubric anchors、pairwise coverage/tie、调用/延迟/成本预算、QA、canary、数量守恒与 Telegram E4；120+ reviewer 与 canary 待运行 |
| [production-planner-pacing-and-memory-remediation-prd.md](production-planner-pacing-and-memory-remediation-prd.md) | **v2 本地第一阶段实现并通过定向 QA**：保留 Listener/wake/资源治理，新增跨 Task aggregate source capacity、headroom、pairwise admission、start feasibility 与数量 E4；当前 `production_fixed=unproven` |
| [production-stability-and-fulfillment-remediation-prd.md](production-stability-and-fulfillment-remediation-prd.md) | **2026-08-17 当前生产 CPU/内存总合同**：多根因资源归因、Planner/Dispatcher 查询与物化治理、OCR 隔离、宿主资源 SLO，以及 Provider、点赞/浏览、登录、日志/Telethon、Gateway/retry 和 Release E4；Planner 与跨批排期细节由专项补正，不改 typed remote fact 语义 |
| [ai-group-hard-hourly-target-prd.md](ai-group-hard-hourly-target-prd.md) | `retired/historical_do_not_implement`：旧每小时硬目标 |
| [ai-group-provider-fallback-and-safe-prompt-design.md](ai-group-provider-fallback-and-safe-prompt-design.md) | **legacy/pre-v2 参考**：仅保留输入安全、统一输出和 Provider adapter；single-active 与统一签到不适用于已接管 current v2 task |
| [ai-account-mask-initialization-reliability-prd.md](ai-account-mask-initialization-reliability-prd.md) | **部分当前**：仅账号面具初始化/恢复状态机；任务发送和签到以当前群日合同为准 |
| [ai-conversation-humanization-and-group-bot-admission-prd.md](ai-conversation-humanization-and-group-bot-admission-prd.md) | **部分历史**：只保留真人化内容参考；C2 配置频道字段、Task 专属投影 + Task 无关四类远端事实、提示绑定和并发准入以当前总合同/闭合合同为准 |
| [all-task-fulfillment-recovery-prd.md](all-task-fulfillment-recovery-prd.md) | `historical_do_not_implement`：旧 C1–C8 合订，只保留事故证据 |
| [channel-membership-precondition-design.md](channel-membership-precondition-design.md) | 频道 / 群聊任务准入前置阶段 |
| [dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md](dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md) | 硅谷 Dispatcher OOM 的精简 P0（固定 OCR 槽、统一 deadline、SIGTERM 优雅回收）与 P1（单 OCR worker、最小 request 状态）根治方案 |
| [shared-dispatch-and-ai-fulfillment-recovery-prd.md](shared-dispatch-and-ai-fulfillment-recovery-prd.md) | `historical_do_not_implement`：旧共享 Dispatcher/中央分配恢复方案 |
| [group-relay-source-filter-upgrade-plan.md](group-relay-source-filter-upgrade-plan.md) | 转发监听来源过滤升级 |
| [material-library-design.md](material-library-design.md) | 素材库、图片、媒体、表情包和素材缓存 |
| [operation-login-drop-rate-prd.md](operation-login-drop-rate-prd.md) | 账号分组登录掉号比例 |
| [risk-control-and-account-center-design.md](risk-control-and-account-center-design.md) | 风控中心与账号中心 |
| [rules-center-design.md](rules-center-design.md) | 规则中心 |
| [search-click-daily-fulfillment-remediation-prd.md](search-click-daily-fulfillment-remediation-prd.md) | **当前纯搜索专项**：单 Task 单目标、独立连续 search lane、assignment/极搜页面 phase 持久执行、双 OCR、transport 事实链与 `target_click_observed` |
| [tenant-tg-bot-webhook-registration-prd.md](tenant-tg-bot-webhook-registration-prd.md) | 租户 TG Bot webhook 自动注册、健康检查、状态回写、可见错误和入站命令回复 |

重复处理：

- 账号中心规则分散在 PRD、风控账号中心、账号安全文档中；全局流程以 PRD 为准，设备/2FA/资料细节以账号安全文档为准。
- AI 群日验收以当前总合同、闭合合同和群日专项为准；hard-hourly 文档与运维记录只作历史排障证据。
- 素材相关能力以素材库文档为准，PRD 只保留跨模块引用。
