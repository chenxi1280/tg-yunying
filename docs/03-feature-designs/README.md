# 03-feature-designs：专项 PRD 与设计

本目录保存专项能力文档。本轮实现只以标记为“当前”的总合同、闭合合同和任务专项为准；主 PRD/主数据流索引已同步当前产品合同，项目结构索引仍须在代码实现完成后按真实入口更新。`historical_do_not_implement` 只保留事故和取证，不得进入开发或 QA。

| 文件 | 定位 |
| --- | --- |
| [account-security-hardening-design.md](account-security-hardening-design.md) | 账号安全、设备清理、托管 2FA、资料初始化 |
| [account-profile-group-style-initialization-prd.md](account-profile-group-style-initialization-prd.md) | **2026-08-19 新账号审计修正已实现、待发布/生产验证**：按精确批量登录 batch 集合中“本批新建且登录成功”的账号并集初始化名字+头像，排除已有账号重登，群样本仅形成匿名风格分布、不复制真实身份或头像，protected preview/apply/readback + 300 账号 E4 |
| [account-standby-auto-authorization-prd.md](account-standby-auto-authorization-prd.md) | 备用授权自动补齐专项 PRD |
| [account-login-group-navigation-recovery-prd.md](account-login-group-navigation-recovery-prd.md) | **当前**：账号验证码 / QR / 2FA 登录 flow、已有账号重登分组语义、账号分组导航与服务端分页修复合同 |
| [account-batch-auto-login-prd.md](account-batch-auto-login-prd.md) | **本地已实现、定向 QA 通过、默认 mode=off、未发布/生产未证明**：批量自动登号专项（失败/超时/远端未解跳行，账号 UUID 接码备注映射，独立对账与 initial/correction 提醒，地址刷新和目标分组归一合同） |
| [login-user-permission-gating-prd.md](login-user-permission-gating-prd.md) | **当前**：平台登录账号按权限加载首屏资源，系统运行时诊断仅限 `system.view` |
| [task-fulfillment-classified-recovery-prd.md](task-fulfillment-classified-recovery-prd.md) | **当前总合同**：C1-C8 分类、Telegram 不可发送当日放弃、C2 连续 30 秒无提示通过、义务物化、独立搜索、并发生成与 Recovery |
| [task-fulfillment-contract-closure-prd.md](task-fulfillment-contract-closure-prd.md) | **当前闭合合同**：分阶段资源空闲即执行、唯一事实+单行 CAS、搜索持久 phase、prepared 新 Task直接 canary→route epoch切换→旧 Task异步删除、unknown 终态、单 Provider key、OCR 和 E4 验收 |
| [ai-group-all-accounts-daily-coverage-prd.md](ai-group-all-accounts-daily-coverage-prd.md) | `historical_do_not_implement`：旧冻结全账号分母方案，仅保留历史取证 |
| [ai-group-admission-quantity-slot-convergence-prd.md](ai-group-admission-quantity-slot-convergence-prd.md) | `historical_do_not_implement`：旧 ContentMix 数量槽方案，仅保留历史取证 |
| [ai-group-daily-fulfillment-remediation-prd.md](ai-group-daily-fulfillment-remediation-prd.md) | `historical_do_not_implement`：旧冻结分母日履约修复，仅保留历史取证 |
| [ai-group-daily-group-target-redesign-prd.md](ai-group-daily-group-target-redesign-prd.md) | **当前 AI 群日专项**：每日总量、任务内动态必达账号、资源空闲即执行、账号任务日唯一签到、面具与内容合同 |
| [production-due-backlog-containment-prd.md](production-due-backlog-containment-prd.md) | **2026-08-11 生产止血交接**：修复 current AI open owner 错计、AI/浏览当前 due 二次排期与跨日 pre-Gateway 积压；不替代长期 stable obligation/due-unit 接管 |
| [hourly-random-pacing-and-ai-humanization-prd.md](hourly-random-pacing-and-ai-humanization-prd.md) | **本地实现完成、定向 QA 通过、默认 flag 关闭、生产未证明**：四类互动 stable due slot/确定性分层随机节奏，以及 AI 活群/频道评论 MessageBrief、独立语义 reviewer、账号声线 v3、质量闸和盲评合同；AI 节奏局部适配沿用现有 `TaskGroupDailyMessageSlot`，不外推为 current-owner 总迁移完成 |
| [production-planner-pacing-and-memory-remediation-prd.md](production-planner-pacing-and-memory-remediation-prd.md) | **2026-08-19 `f49353fd` 已发布并通过目标守恒/事件唤醒/锁序短窗 E4**：Planner 有界投影、Listener wake、四类跨批 source cursor、Gateway 前 fail-closed admission、发送目标指纹与 PSS/cgroup 自采样均已上线；最终行为 24h、完整自然日、暂停 comment/like 与历史绝对 swap 线未闭环，`production_fixed=unproven` |
| [production-stability-and-fulfillment-remediation-prd.md](production-stability-and-fulfillment-remediation-prd.md) | **2026-08-16 当前生产 CPU/内存完整修复**：多根因资源归因、精确搜索停流量与 OCR 安全停启、per-source adaptive OCR、Planner/Dispatcher 查询与物化治理、宿主资源 SLO，以及 Provider、点赞/浏览、登录、日志/Telethon、Gateway/retry 和 Release E4；不改 typed remote fact 与履约合同语义 |
| [ai-group-hard-hourly-target-prd.md](ai-group-hard-hourly-target-prd.md) | `retired/historical_do_not_implement`：旧每小时硬目标 |
| [ai-group-provider-fallback-and-safe-prompt-design.md](ai-group-provider-fallback-and-safe-prompt-design.md) | **部分当前**：仅输入安全、统一输出和 Provider adapter；运行时为单 active key、多模型独立并发、统一签到 |
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
