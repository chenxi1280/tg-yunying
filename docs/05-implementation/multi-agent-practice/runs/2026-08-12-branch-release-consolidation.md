# 2026-08-12 分支与 release 收敛

## Intake Card

- `intake_id`: `intake-2026-08-12-branch-release-consolidation`
- `level`: `L3`
- 原始需求：审计所有本地/远端分支，识别未合并、未部署功能，并按产品、开发、QA、验收、发布、生产验证流程处理。
- 成功口径：旧分支逐项分类；只将符合 current contract 的缺口送入发布；`master -> release -> Deploy Production` 后分别记录 SHA、运行状态和业务 E4。
- 安全边界：主 checkout 的既有 dirty work 不改写、不 reset、不 clean、不 broad stash；历史分支不因“未合并”而默认有资格上线。

## Product Handoff

- OCR 本地补丁依赖已废弃的模型投票；当前搜索合同固定 AI/VLM 调用数为 0，处置为 `historical_do_not_implement`。
- 搜索 assignment handoff 补丁依赖已废弃的 Window、handoff grace 和 scope capacity；当前 assignment 可直接执行到 obligation deadline，处置为 `historical_do_not_implement`。
- 旧 per-group 补丁依赖 legacy `GroupBotAdmission/PendingVisibilityCredit`，禁止移植。current C2 唯一开发范围是 `TaskGroupBotAdmission` confirmation click 前的实时 source 恢复。
- 旧独立诊断脚本与当前监控重复或包含过期 mutation 路径，不随本发布捆绑。

## Current C2 Contract

1. 绑定账号先精确读取旧 source，再读取目标群当前控制窗口。
2. 只有 trusted bot、精确频道集合、callback 和账号归属同时匹配才可继续。
3. 原 source 有效时才点击；发现同账号新 source 时重新解析并物化新 fingerprint/action，旧 Action 写 `group_bot_confirmation_superseded`。
4. 无匹配来源时重启同账号连续 30 秒观察；读取异常写 `group_bot_confirmation_live_fetch_failed` 并保持 pending。
5. 不新增迁移、跨表锁或 legacy 模型写入；callback unknown 不重点击。

## Dev Evidence

- 隔离分支：`codex/branch-release-consolidation-20260812`，基于 `origin/master@e74406f6`。
- 红测：Task-scoped 新 source 与 fetch error 两例均证明旧实现绕过 refresh 并进入 click。
- 绿测：最终相关准入/confirmation no-postgres 组合 `54 passed`；独立 QA 扩大到 `75 passed`。
- `py_compile` 与 `git diff --check` 通过。
- PostgreSQL 定向测试本地 `blocked`：没有指向 PostgreSQL 测试库的 `TEST_DATABASE_URL/DATABASE_URL`，未伪造通过，交 CI Release Gate。

## Release Gate

- Product Design Complete：`complete`，真相源为 current C2 PRD §6.1/§6.2A、DF-330-C2。
- QA：`qa_pass`；首次 QA 的同名歧义、CAS 原子性和新频道集合三个 blocker 已返工并由 re-QA 关闭。
- Product acceptance：`product_accepted=true`（E2）；exact-source 同名歧义的产品 blocker 已补测并关闭。
- CI / master / release / deploy：`pending`。
- Production E4：`unproven`；部署健康不得替代 `Task -> TaskGroupBotAdmission -> Action -> Attempt -> typed fact -> remote_message_id`。

## Locked Paths

- `backend/app/services/task_center/dispatcher.py`
- `backend/app/services/task_center/group_bot_confirmation_refresh.py`
- `backend/app/services/task_center/task_group_bot_admission_recovery.py`
- `backend/tests/test_fact_first_group_bot_requirements.py`
- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`
- 本运行记录与状态板对应行
