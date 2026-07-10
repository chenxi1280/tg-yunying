# 03-feature-designs：专项 PRD 与设计

本目录保存专项能力文档。专项文档可以细化主 PRD，但不能悄悄改变全局产品口径；如果专项验收口径发生变化，需要回写 `01-product/tg-ops-platform-prd.md`。

| 文件 | 定位 |
| --- | --- |
| [account-security-hardening-design.md](account-security-hardening-design.md) | 账号安全、设备清理、托管 2FA、资料初始化 |
| [account-standby-auto-authorization-prd.md](account-standby-auto-authorization-prd.md) | 备用授权自动补齐专项 PRD |
| [ai-group-all-accounts-daily-coverage-prd.md](ai-group-all-accounts-daily-coverage-prd.md) | AI 活跃群“全部账号”每日真实发言履约、增量账号同步、日账本、容量证明和生产验收专项 PRD |
| [ai-group-hard-hourly-target-prd.md](ai-group-hard-hourly-target-prd.md) | AI 活跃群每小时硬目标专项 PRD |
| [channel-membership-precondition-design.md](channel-membership-precondition-design.md) | 频道 / 群聊任务准入前置阶段 |
| [group-relay-source-filter-upgrade-plan.md](group-relay-source-filter-upgrade-plan.md) | 转发监听来源过滤升级 |
| [material-library-design.md](material-library-design.md) | 素材库、图片、媒体、表情包和素材缓存 |
| [operation-login-drop-rate-prd.md](operation-login-drop-rate-prd.md) | 账号分组登录掉号比例 |
| [risk-control-and-account-center-design.md](risk-control-and-account-center-design.md) | 风控中心与账号中心 |
| [rules-center-design.md](rules-center-design.md) | 规则中心 |
| [tenant-tg-bot-webhook-registration-prd.md](tenant-tg-bot-webhook-registration-prd.md) | 租户 TG Bot webhook 自动注册、健康检查、状态回写、可见错误和入站命令回复 |

重复处理：

- 账号中心规则分散在 PRD、风控账号中心、账号安全文档中；全局流程以 PRD 为准，设备/2FA/资料细节以账号安全文档为准。
- AI 活跃群硬目标的产品验收以 PRD 为准，排障和上线动作以 `04-ops/ai-group-hard-hourly-target-ops.md` 为准。
- 素材相关能力以素材库文档为准，PRD 只保留跨模块引用。
