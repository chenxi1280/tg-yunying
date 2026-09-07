# 频道历史积压工具补齐发布

- intake_id: channel-backlog-tool-release-20260908
- level: L2
- release_owner / rollback_owner: current Codex task
- 用户授权：提交现有遗留代码并按标准流程部署。
- 范围：维护脚本、专项回归、PRD 和结构索引；无生产 apply，无迁移，无前端或 worker 路由变化。
- product：频道关注 PRD §14.7 完成设计和反向检查；重复锁定查询全部候选条件，复用正式远端证据判定与结算。
- dev/review：移除锁后仅复验 status 的缺口，使用 populate_existing 刷新实体；批次结算独立函数，均不超过 50 个非空行。
- QA：脚本 6 项回归和正式结算重放 1 项通过；覆盖真实 safely_not_executed fact、preview、重复执行、Gateway/截止/范围/状态排除及重新排程。
- Release Gate：定向测试及编译/diff check 通过后推送 master、快进 release、dispatch Deploy Production；CI 完成后独立核对 current、容器 SHA、health 和脚本 --help。
- rollback：本次无 schema/data 变化，代码可通过后续 revert 发布撤回；不撤销其他提交。
- business_evidence：本轮未执行清理，积压处理结果不属于发布验收。
- status: pending deployment
