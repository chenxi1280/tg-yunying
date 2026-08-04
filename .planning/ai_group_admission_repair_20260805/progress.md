# Progress

- 2026-08-05：读取项目 AGENTS、相关技能和 planning-with-files 说明；确认本轮需要 PRD、代码、QA、发布和线上 E4 闭环。
- 2026-08-05：执行 session catchup、git status、远端 fetch；确认生产 release `9b0d8802`，当前本地 HEAD `8b67d006`，工作树只有历史 planning/docs 未提交改动。
- 2026-08-05：复核线上学生会：配置频道存在；796 是日分母；1490/1594 关注事实成功；104 条 pending 被失败/过期 Action 身份占住；新旧 C2 表不一致。
- 2026-08-05：定位代码候选根因：planner 跳过已有 action_id，失败收口不做可恢复 follow CAS；新 Task 初始 readiness 仍读取 legacy 表。
- 2026-08-05：完成 Product Design Complete：更新闭合履约 PRD、产品 PRD、数据流转索引和结构索引，明确 Task-scoped C2、mutation 边界、replan 和回滚审计口径。
- 2026-08-05：实现 Task-scoped fact-first prompt/action 路由、旧 ready 投影重开、TaskGroup claim priority、频道 follow/confirmation Dispatcher 分支、明确 false evidence 的递增 replan 和 task restart 重建；新增 7 个 C2 回归测试。
- 2026-08-05：发现第二条线上根因：已在目标群的学生会账号对应 membership Action 已被 `already_joined` 跳过，fact-first 预关注只挂在新入群路径，因此 `configured_channel_follow` facts 为 0；修复为 fact-first 正文前复核配置频道，并复用已有账号-目标关注事实。
- 2026-08-05：更新存量 reconciliation 脚本为仅清理缺失 Action 绑定，不再将终态/unknown Action 的 binding 批量释放；legacy confirmation 同样禁止未知结果盲建新 Action。
- 2026-08-05：定向回归当前通过：64 tests passed；`git diff --check` 与 Python compileall 通过。全量 `pytest -m no_postgres` 在 60 秒闸门内运行到约 47% 后超时，PostgreSQL 全量 reset 仍受测试库连接问题阻断。
