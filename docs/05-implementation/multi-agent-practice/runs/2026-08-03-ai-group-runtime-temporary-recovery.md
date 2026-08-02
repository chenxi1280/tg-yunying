# 2026-08-03 AI 活群运行批次临时恢复

## Mini Bug Card

- 现象：郑州楼凤新自然日有 766 个 ready coverage、766 个 open 数量槽、零 ContentMix，Planner 仍立即写 `quantity_slots_unavailable`。
- 影响：群日发送 Action 为 0，远端确认无法增长。
- 临时目标：不改每日 800 个冻结账号目标、不改 coverage/admission/数量槽，只把单轮 `60/12` 临时收敛为 `1/1`，验证单生成项能绑定单 coverage 槽。
- 安全边界：apply 必须匹配 preview 状态哈希，且当前任务没有 open Action、没有已开始 Gateway 的 open Action、没有当日 ContentMix。
- 回滚：永久修复发布后，通过同一受审计入口把运行批次恢复为 PRD 配置；不得直接删除 Action、Attempt、ContentMix 或群日账本。
- 升级条件：`1/1` 模拟仍不能完整对齐、apply 后 10 分钟没有非空 `remote_message_id`、出现 unknown/重复发送或当日 ContentMix 已存在时，停止临时写入并进入正式 L3 修复。

## 证据

- 生产 release：`8d79024010d0735236c9e4624cd86087146ff7fd`。
- 2026-08-03 00:00 账本：郑州楼凤 `effective_message_target=800`、`ready=766`、`open account_coverage slots=766`、`actions=0`、`content_mix=0`、`last_error=quantity_slots_unavailable`。
- 对照：郑州师范同一时点已创建 1 条 pending Action；郑州大学运行路径正常。
