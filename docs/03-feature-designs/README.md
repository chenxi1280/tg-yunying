# 03-feature-designs：专项 PRD 与设计

本目录保存专项能力文档。本轮实现只以标记为“当前”的总合同、闭合合同和任务专项为准；主 PRD/主数据流索引已同步当前产品合同，项目结构索引仍须在代码实现完成后按真实入口更新。`historical_do_not_implement` 只保留事故和取证，不得进入开发或 QA。

| 文件 | 定位 |
| --- | --- |
| [account-security-hardening-design.md](account-security-hardening-design.md) | 账号安全、设备清理、托管 2FA、资料初始化 |
| [account-standby-auto-authorization-prd.md](account-standby-auto-authorization-prd.md) | 备用授权自动补齐专项 PRD |
| [task-fulfillment-classified-recovery-prd.md](task-fulfillment-classified-recovery-prd.md) | **当前总合同**：C1-C8 分类、Telegram 不可发送当日放弃、C2 连续 30 秒无提示通过、义务物化、独立搜索、并发生成与 Recovery |
| [task-fulfillment-contract-closure-prd.md](task-fulfillment-contract-closure-prd.md) | **当前闭合合同**：分阶段资源空闲即执行、唯一事实+单行 CAS、搜索持久 phase、prepared 新 Task直接 canary→route epoch切换→旧 Task异步删除、unknown 终态、单 Provider key、OCR 和 E4 验收 |
| [ai-group-all-accounts-daily-coverage-prd.md](ai-group-all-accounts-daily-coverage-prd.md) | `historical_do_not_implement`：旧冻结全账号分母方案，仅保留历史取证 |
| [ai-group-admission-quantity-slot-convergence-prd.md](ai-group-admission-quantity-slot-convergence-prd.md) | `historical_do_not_implement`：旧 ContentMix 数量槽方案，仅保留历史取证 |
| [ai-group-daily-fulfillment-remediation-prd.md](ai-group-daily-fulfillment-remediation-prd.md) | `historical_do_not_implement`：旧冻结分母日履约修复，仅保留历史取证 |
| [ai-group-daily-group-target-redesign-prd.md](ai-group-daily-group-target-redesign-prd.md) | **当前 AI 群日专项**：每日总量、任务内动态必达账号、资源空闲即执行、账号任务日唯一签到、面具与内容合同 |
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
