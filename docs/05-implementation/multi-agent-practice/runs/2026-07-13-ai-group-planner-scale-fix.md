# 2026-07-13 AI 活群 Planner 规模治理

## Intake Card

- `intake_id`: `intake-2026-07-13-ai-group-planner-scale`
- `bug_id`: `bug-2026-07-13-ai-group-planner-stall`
- `level/lane`: `L3 / ai-group-quality/planner-runtime`
- 用户目标：监督修复生产 AI 活群，确保每个目标群中的全部账号按北京时间每日真实发言一次，并检查评论任务运行状态。
- 当前状态：release `fecdcfae` 已成功发布，上一轮 Planner 运营摘要拆分已使旧 `operation_issue_accounts` 查询降为 0，证明该子根因修复有效；但稳定周期 2026-07-13 21:48:58 CST 覆盖停在 `394/2320`。Planner `172.19.0.11` 出现 110 秒 Action 全行 SELECT；21:50:51 同容器事务已达 220.6 秒，现场 SQL 是将 `task_account_daily_coverage` 条件更新为 `reserved` 并写 `reserved_action_id`，证明单个 `all_accounts_daily` 任务的一次 build-plan/预约仍可无界处理 580 条义务。Recovery `172.19.0.8` 反复执行约 28 秒的 Action + Task executing membership 查询；dispatcher-1 `172.19.0.23` 出现 107 秒 task stats `GROUP BY`，dispatcher-4 `172.19.0.29` 因更新 `operation_targets/tasks` 形成大量 transaction/tuple lock，现场共 10 个 lock waiters。整体生产 E4 失败，fresh L3 batch `design_status=complete`、`resync=true`、Release Gate 重新 `blocked`，禁止写 `production_fixed`。

## 生产诊断

- 生产存在 4 个 `all_accounts_daily` 任务，每个分母 580，共 2320 条日履约义务。
- Planner 心跳和主 drain 发生长时间停滞；任务欠账存在，但没有 open coverage Action 推进。
- 根因是 Planner 事务叠加多项规模放大：每任务重复在线来源 reconcile、逐账号 readiness / capacity 查询、backlog 全量 ORM 加载，以及无 open Action 时仍执行 preparation。
- 第三次发布后的生产 strace 继续定位到 `_membership_actions_by_account`：每轮按 task/channel 读取全部历史 membership Action 和巨大 payload/result，再在 Python 每账号取最新；Planner 持续接收历史大行，单 drain 超过本地 120 秒心跳阈值，并形成长事务与锁等待。
- 评论任务生产取证确认两条都在北京时间当天 `0 Action / 0 remote success`：`太郎日记回复` 已达到解析后的生命周期总预算 86，属于配置终止但长期显示 `running + last_error`；`阿哥日记` 尚余 49 条预算，但 MiniMax-M3 返回 `unprocessable_entity_error: input new_sensitive (1026)`，旧分类器未触发重描述。

## 评论任务补充 Product Handoff

- 生命周期总预算使用两阶段收口：有 open Action 时保持 `running/draining` 并清空 `last_error`；open 清零且 `success + unknown_after_send` 达上限后幂等转 `completed/next_run_at=null`。
- `unknown_after_send` 参与防超发预算但不得冒充真实远端成功；完成 stats 分别记录 remote success 与 unknown 数量。
- MiniMax `input new_sensitive (1026)` 只在同时出现对应 `unprocessable_entity_error` 时进入首次调用后的最多 3 次安全重描述；其他 HTTP 422 不泛化重试。
- 重试 Prompt 不得再次拼入原始敏感文本。连续 4 次拒绝保留最终 422、创建 0 个 Action，不使用随机表情伪造成功。
- 生命周期预算与收口已拆入 `channel_comment_budget.py`；恢复已满任务先验 cap，已满直接 completed。

### 评论生命周期完成态 recovery Product Handoff（resync）

- fresh 生产证据：太郎任务 `313e…` 的 stats 已记录 `completion_reason=lifetime_cap_reached`、`completed_at`、`remote_success_count=51`、`unknown_after_send_count=35`，解析后生命周期总量 86 已满，但任务仍为 `status=running` 且 `next_run_at` 非空。
- 根因：`service._recover_continuous_task_states` 对 `CHANNEL_DYNAMIC_TASK_TYPES` 中 `completed + scheduled_end=null + 非 specific` 的任务无条件恢复 `running`，没有排除 lifetime-cap completed；因此部署或 recovery drain 会撤销评论生命周期收口。
- Product Design Complete：`design_status=complete`、`resync=true`、L3；实现、独立 QA 和产品复验前 `done_status=blocked`、`release_gate=blocked`。该变更只收窄 lifetime-cap 完成态的 recovery，不改变普通连续动态任务原有恢复能力。
- 状态契约：频道评论 / 回复任务一旦写入 `completion_reason=lifetime_cap_reached`，即成为 recovery 吸收终态；通用 recovery、部署恢复或 Planner 状态修复不得改回 `running`，不得重建 `next_run_at`，不得清空 `completed_at`、remote success、unknown 或解析上限统计。普通动态任务因其他原因 completed 时仍按现有语义恢复。
- start / resume 契约：所有显式 start / resume 入口仍必须先验解析后 lifetime cap；已满时幂等保持 `completed/next_run_at=null`。提高上限也不得由后台 recovery 静默复活，仍需既有显式重置或重新规划动作。
- E2 红测：构造带 `lifetime_cap_reached + completed_at + 51 remote + 35 unknown` 的 completed 动态评论任务，执行真实 `drain_task_recovery` / `_recover_continuous_task_states` 后断言状态、`next_run_at` 和完成统计完全不变；同组必须证明普通非 lifetime-cap 动态 completed 任务仍恢复 running，并保留 start / resume 已满先验回归。
- E4 验收：部署实际 commit 后触发并跨越至少一次 recovery drain / 容器重启，太郎任务保持 `completed/next_run_at=null`，完成统计仍为 `51 remote + 35 unknown = 86`，且不再创建新 Action；评论任务仍须按真实远端结果单列 `pass / blocked / unproven`，状态正确不能替代阿哥或其他评论任务真实发送恢复。

## Product Handoff

- 不改变全账号日覆盖 PRD、分母、北京时间自然日、Telegram 远端成功确认、冷却、小时 / 日上限、hard-hourly、质量和未知结果规则。
- account-online worker 统一维护 desired sources；Planner 只批量读取 readiness。
- 无 open Action 时跳过 preparation；有 open Action 时 preparation 后重新读取 open 状态。
- backlog 使用数据库 `count/min`，hard-hourly 例外只读窄字段。
- 容量缓存必须与原逐账号判定在 Action / MessageTask 状态、时间、冷却、上限、排除项和 reservation 上等价。
- 仅低频来源的在线账号恢复 active 时必须立即进入 `warming` 并探测，成功前 fail-closed；已有 global / active 来源不得被误阻断。

### 群准入最新 Action quick-fix

- 行为不变：current/legacy membership、频道、可选 task、account 非空过滤保持原样；每账号按 `created_at DESC, id DESC` 选最新一条。
- 数据库窗口子查询只投影 id/account/created_at/rank，外层 rank=1 后才加载完整 Action；failed/unknown/open/joined 与 daily recheck 继续复用原判定。
- 发布 Planner smoke 改用轻量 `app.worker_health` 的真实数据库 heartbeat；不降低超时、不绕过 worker unhealthy。
- 无 migration；若 PostgreSQL 大历史或生产仍超过 5 秒，升级为索引/迁移标准流程。

## Dev 与 QA 证据

- `4 tasks × 580 accounts` account-online 第二轮 reconcile：查询有界、`0 UPDATE`、小于 5 秒。
- 新账号链路：eligibility event → membership / daily ledger → warming blocker → probe online → blocker release → Planner pending Action。
- 容量缓存 15 项 cached / uncached 等价；Planner 580 账号相邻 slot 总查询不超过 3。
- PostgreSQL backlog 覆盖 JSON 布尔、legacy payload、aware / naive bucket 和 partial membership。
- 全量 no-PostgreSQL：`1246 passed, 805 deselected, 5 warnings in 41.77s`。
- PostgreSQL：`15 passed in 3.31s`；Python 编译与 `git diff --check` 通过。
- 独立 QA：无 Critical / Important / Minor；Product Acceptance：通过。
- 评论补充回归：`test_ai_task_limits.py`、评论配置总预算 guard 和 Planner open-action 隔离定向共 `50 passed`；新增 pending 失败释放预算、完成时间幂等、`new_sensitive` 第 1/2/3 次后成功、普通评论/引用回复、连续 4 次拒绝、其他 422 不重试均通过；全量 no-PostgreSQL 更新为 `1252 passed, 810 deselected, 5 warnings in 40.60s`。
- 评论独立 QA：`81 passed`，Critical / Important / Minor 均为 0；最终 Product Acceptance：`product_accepted=true`（仅 E2），同意进入 Release Gate，不等于生产恢复。
- 群准入 latest-action 回归：SQLite 语义/行数下推与轻量 smoke 共 `3 passed`，membership/worker 定向共 `64 passed`；PostgreSQL membership `14 passed`、原 Planner `15 passed`、全量 no-PostgreSQL `1252 passed`。580 账号 × 4 轮大 JSON 历史单查询返回 580 行、实测 `0.0491s`；`EXPLAIN ANALYZE` 显示过滤在 WindowAgg 前、`row_number <= 1`、执行 `18.054ms`。
- 群准入 quick-fix 独立 QA：相关集 `77 passed`，最小语义/smoke `7 passed`，Critical / Important / Minor 均为 0；最终 Product Acceptance：`product_accepted=true`（仅 E2），Release Gate 就绪，不等于生产恢复。
- Release run `29225396989` 因 PostgreSQL fixture 复用 tenant 1 失败；run `29225675866` 因 open-action 测试误拦截遗留任务失败；run `29227840790` 的 checks/镜像成功，但 deploy 三次均在 Planner smoke 或 planner unhealthy 超时，生产实际镜像为 `fd9cf0c9`，不能写发布成功。
- Release run `29230128879` 在 checks 失败、未构建镜像：新增 580×4 PostgreSQL 规模测试未清理自己提交的 2320 条 Action，后续 backlog 测试读到 2328 条而非自己的 8 条。修复为同租户显式前后清理并跳过与本测试无关的规则自动绑定；按 CI 失败顺序 `2 passed`，membership 相关 PostgreSQL 组 `15 passed`。独立 rework QA 通过，Critical / Important / Minor 均为 0，测试后 Tenant / Task / TgAccount / Action 均为 0，Release Gate 恢复就绪。
- 第五次 Deploy Production run `29230895485` 成功：checks、镜像与 deploy 全通过，生产 release `/data/tgyunying/releases/20260713071213_7f7af0c`，实际镜像 commit `7f7af0cb`。发布后北京时间 2026-07-13 的 4 个任务共 2320 项覆盖义务，连续样本只从远端确认 318 增长到 321；业务仍未达到完整自然日矩阵。太郎日记回复已达到解析后生命周期总预算但状态未被 Planner 提交收口；阿哥日记无当天新 Action / 远端结果，评论 E4 未恢复。
- 新生产根因：多个 Planner / Dispatcher PostgreSQL 事务持续 100-400 秒以上并形成锁链，根事务执行租户级 `ai_group_message_memory` 时间窗查询。生产表约 40741 行、总大小约 62MB；查询加载包含 result/画像诊断在内的完整 ORM 大行，而相似度判定只需要 id/normalized_text/raw_text，容器持续接收大结果集，7天聚合在20秒内未完成。Product Handoff 保持租户级跨群去重语义，采用轻投影 + `(tenant_id, status, planned_at DESC)` 并发索引；规格见 `docs/superpowers/specs/2026-07-13-ai-message-memory-dedup-performance-design.md`，实现须等书面规格复核。

### 消息记忆性能 Dev 实现与验证

- 实现提交链为 `532ca921`（三字段轻投影）、`ca831b8c`（模型索引与 0091）、`0e3b0ee6`（迁移失败语义硬化）、`e8043859`（真 PostgreSQL 规模门禁）。`_window_memories` 保持租户级跨群、状态集合、时间窗、排除 id 和 `planned_at DESC` 顺序不变，只返回 `id/normalized_text/raw_text`，不再物化大 `result` / 画像诊断字段。
- `0091_ai_message_memory_dedup_index.py` 新增 `(tenant_id, status, planned_at DESC)` 索引；PostgreSQL 使用 Alembic `autocommit_block` 执行 `CREATE/DROP INDEX CONCURRENTLY`，catalog 只把 `indisvalid=true` 的同名索引视为已完成，DDL 错误不降级、不吞掉。Alembic 当前唯一 head 为 `0091_ai_memory_index`。
- 两个 Important 复核项已在 Dev 阶段修正：其一，upgrade 不再因目标表缺失而静默跳过，缺表、并发 DDL 失败会显式失败；同时补齐 PostgreSQL autocommit 顺序与有效/无效索引 catalog 契约。其二，补充真实 PostgreSQL 的生产规模性能门禁，避免只凭 SQLite 查询形状或源码检查推断生产性能。
- 真 PostgreSQL 规模样本为 40,741 行，每行 `result` 原始逻辑大字段 1,408 bytes，合计约 54.71 MiB；前序性能验收 `_window_memories=0.235042s`、最坏无命中 `_first_similar_memory=0.270144s`，分别低于 2 秒 / 5 秒门禁。本次独立复测同样 40,741 行得到查询 `0.109495s`、扫描 `0.268278s`、无重复命中，首次复测表总 relation 为 `71.23 MiB`；后续 delete/reinsert 观察到的表膨胀不替代 54.71 MiB 原始载荷口径。
- 专用测试库真 PostgreSQL 定向整组（消息记忆、归一化、跨群、查询形状、规模、dispatcher、任务限制、评论配置）为 `81 passed in 7.80s`，墙钟 `8.89s`；query-shape + merge-integrity + database 迁移证据为 `17 passed in 2.59s`，墙钟 `3.37s`。
- 全量 `pytest -m no_postgres -q` 在单次 60 秒硬门禁内为 `1262 passed, 814 deselected, 5 warnings in 53.78s`，墙钟 `58.23s`、退出码 0；5 条 warning 均为 SQLAlchemy 使用 Python 3.12 默认 sqlite datetime adapter 的弃用提示。相关 app、0091 和两个新增测试 `py_compile` 通过，仓库根 `git diff --check` 通过。
- 截至本节 Dev handoff 时点，只证明 Dev E2 与性能测试门禁，独立 QA、Product Acceptance、发布和生产 E4 当时尚未完成；该阶段事实由下方验收记录继续流转，不追改为事后通过。北京时间完整 2320 项远端确认矩阵与评论任务当天真实远端成功仍未恢复。

### 消息记忆独立 QA 与 Product Acceptance

- 初次独立 QA 判定 `qa_pass=false`，发现 2 个 Important：0091 downgrade 在目标表缺失时静默返回，违反失败显式暴露契约；`_window_memories` 暴露 session 之外 4 个位置参数，超过项目最多 3 个位置参数的硬限制。该失败事实保留，不以随后通过覆盖。
- 修复提交：`d180fd96` 让 downgrade 缺表显式失败；`a928500b` 将 tenant/group/cutoff/exclude 窗口过滤改为 keyword-only，同时保持跨群去重语义；`a03d40bc` 让 upgrade/downgrade 都先执行 `_require_table`，以稳定 `RuntimeError` 暴露缺表并补 PostgreSQL 契约测试。
- re-QA：业务与性能定向 `85 passed`，query-shape + merge-integrity + database 迁移证据 `21 passed`；40,741 行真 PostgreSQL 复测 `_window_memories=0.146s`、最坏无命中 `_first_similar_memory=0.271s`，低于 2 秒 / 5 秒门禁；Critical / Important / Minor 均为 0，`qa_pass=true`。
- Product Acceptance：`product_accepted=true`（仅 E2）。轻投影、租户级跨群去重语义、显式失败和 0091 并发索引满足 Product Handoff，允许进入 Release Gate；这不代表已发布或生产恢复，2320 项完整自然日矩阵和评论任务最终 E4 仍待真实生产证据。

### ai-memory 历史 Action 回填 `action_id` 查询 Product Handoff（resync）

- fresh 生产证据：ai-memory worker 每 60 秒从历史 Action 中选取最近最多 100 条候选，并对每条调用 `_memory_exists_for_action` 查询 `ai_group_message_memory.action_id`。生产表约 41,255 行、64 MB，该列无索引；已观察到 100 秒级事务、`DataFileRead` 等待，以及阻塞 heartbeat 的锁链。该根因独立于上方 `_window_memories` 轻投影问题，所以上方 0091 的 E2 通过事实保留，但不能继续据此判定本次 Release Gate 就绪。
- Product Design Complete：`design_status=complete`、`resync=true`、L3；实现/QA/产品复验前 `done_status=blocked`、`release_gate=blocked`。本次只补充行为保持型查询性能契约，不改变历史回填业务语义。
- 历史回填语义必须保留：每轮仍核对最近最多 100 条符合条件 Action；只有 `action_id` 对应记忆精确存在时才跳过该条，缺失记录必须继续回填。不得为消除长事务而缩小批次、停用 worker、跳过历史 Action、缓存假命中、返回 mock success 或吞掉数据库错误。
- 查询性能约束：`action_id` existing/missing 点查都必须由可验证的数据库索引支撑；迁移、查询超时或数据库错误必须显式失败。实现不得删除现有历史记忆，也不得改变 30 天回填范围、最近 100 条上限、归一化或租户级跨群去重规则。
- E2 验收：在真 PostgreSQL `ai_group_message_memory >= 40,000` 行的数据集上，分别验证已存在和缺失 `action_id`；两类点查均 `<100ms`，`EXPLAIN` 证明使用目标索引且不做全表顺序扫描，连续核对 100 条的单轮事务 `<10s`；同时断言已存在记录不重复插入、缺失记录确实回填、查询/迁移失败显式暴露。
- E4 验收：按实际 release commit 和迁移状态核对生产索引有效；planner、dispatcher、ai-memory 等并发运行后，连续至少 3 个 60 秒 maintenance 周期无 `>60s` 事务、无该查询引发的 `DataFileRead`/heartbeat 锁链；抽样同时证明 existing 正确跳过、missing 持续回填，2320 项任务 × 群 × 账号远端确认分子在连续样本中继续增长。worker healthy、事务消失或覆盖单点增长不能单独替代完整自然日 E4。
- 回滚口径：新增查询索引应为兼容旧应用的加法变更，应用回滚默认保留索引；若必须删除索引，须在维护窗口使用非阻塞方式并核对数据库状态。回滚到无索引旧路径会重新暴露长事务风险，不能记作恢复。

### action memory 与 comment recovery cross re-QA / Product Acceptance

- ai-memory 最终 cross re-QA：Critical / Important / Minor 均为 0。0092 为 `action_id` 非唯一索引，PostgreSQL 使用并发 DDL；缺表、invalid 同名索引和 DDL 失败均显式暴露。历史 Action 查询在 batch limit 前通过 `NOT EXISTS` 排除已有记忆，仍保留近 30 天缺失回填和单条存在性复检，不以跳过回填换性能。
- comment recovery 最终 re-QA：未发现 Critical / Important；旧测试名称把 start / resume 混写的问题作为 Minor 修正为真实 `resume_task`，主代理评论定向复测 `12 passed`。实现只排除 `channel_comment + completion_reason=lifetime_cap_reached`，普通动态 completed 任务继续恢复，并覆盖 recovery 后不复活与 start / resume 已满幂等。
- 主代理最终验证：真 PostgreSQL action-index / query-shape / scale `23 passed in 5.47s`；全量 no-PostgreSQL `1278 passed, 814 deselected in 41.27s`；相关 `compileall` / `py_compile`、仓库 `git diff --check` 通过，Alembic 唯一 head 为 `0092_ai_memory_action_idx`。
- Product Acceptance：`qa_pass=true`、`product_accepted=true`（仅 E2）。两项实现满足 `c8567324` 与 `a5ec1de7` Product Handoff，Release Gate ready；这不证明代码已发布、生产长事务已消失、太郎状态已收口或 2320 自然日覆盖完成。
- 发布前 blocked baseline（必须保留）：2026-07-13 17:55/18:00 CST，AI 活群远端覆盖 `347/2320`；阿哥评论 `remote=263`，判定 PASS；太郎仍为 `running`，判定 blocked；生产长事务仍存在。只有发布实际 commit 后完成 0092 有效性、recovery 吸收终态、无 `>60s` 事务/锁链和完整自然日覆盖 E4，才可更新生产结论，当前禁止写 `production_fixed`。

### Planner 全账号运营摘要长事务 Mini Bug / L3 Product Handoff（resync）

- Mini Bug Card：症状为 release `59d5c3e`、0092 有效且 missing lookup `0.485ms` 后，active planner `172.19.0.28` 仍连续出现 `>60s` 事务，现场 SQL 为 `UPDATE operation_issue_accounts`；业务影响为 4×580 覆盖只推进到 `359/2320`，太郎 lifetime-cap 仍未收口。等级维持 L3，影响核心 Planner、运营读模型和评论恢复 E4。
- 根因链：`_drain_task_planner -> refresh_task_stats -> refresh_task_summary -> for _task_account_ids -> refresh_account_summary`。单个 580-account group_ai_chat 在 Planner 主事务内逐账号读取 24 小时 Action、容量、风控、安全与重试信号，并更新 `account_runtime_summary` / `operation_issue_accounts`，把运营观测投影的 O(accounts) 成本和锁持有时间叠加到规划事务。
- Product Design Complete：`design_status=complete`、`resync=true`。职责边界确定为 Planner 保留规划所需的 task stats、task/target 轻量汇总、latest failure 和单个代表异常；全账号健康 / 容量 / 风险 / retry / 趋势和异常影响账号分页由 metrics worker 独立刷新。不得仅延长 Planner 超时、吞掉摘要错误、跳过账号或返回陈旧数据并伪装实时。
- metrics 数据流：以显式 `account_summary_batch_size`（生产/QA 首版每事务最多 100 账号）和可恢复游标分批覆盖全部账号，每批独立提交；下一轮从游标继续，最终覆盖 580/580。metrics 失败必须保留最后已提交快照并暴露 heartbeat/error、`updated_at` 和 stale，不得回退到 Planner 内同步全刷。
- 保留语义：task summary 的 planned/success/failed/pending、oldest pending、latest failure、runtime stage 和 target 关联不变；account summary 的健康分、风险、身份/授权/设备、各能力可用性、容量解释、pending/executing/unknown 占用、不可用原因、next retry、24 小时趋势不变；operation issue/source/account 的异常类型、严重度、代表 task/action、failure type/reason、建议动作、handling mode、来源、影响账号、impact type、latest seen 和人工状态不变。
- E2 QA：真 PostgreSQL 构造 580-account group_ai_chat 及现存 issue/account 投影，证明 Planner drain 不调用逐账号 `refresh_account_summary`、不产生全量 `UPDATE operation_issue_accounts`，查询/写入数量不随账号数线性增长且单轮 `<5s`；metrics 分批覆盖 580/580、每批最多 100、单事务 `<10s`，中途失败后从游标继续且不重复/漏账号。拆分前后对比上述 task/target/account summary 与 issue/source/account 字段和 resolve/upsert 语义等价。
- E4：发布实际 commit 后核对 planner、metrics、recovery heartbeat 与数据库 active transaction；并发连续至少 3 个完整 cycles 无 `>60s` 事务、无 Planner 发起的全量 `operation_issue_accounts` 更新；`359/2320` 覆盖分子在连续样本继续增长；太郎必须成为 `completed/next_run_at=null` 且跨 recovery 保持吸收终态。任一项缺失均保持 blocked，不能以 0092 点查成功或容器 healthy 代替。

### Planner 运营摘要职责拆分 Dev E2

- Planner 调用 `refresh_task_stats(..., include_configured_accounts=False)`，因此 task/target、latest failure、代表异常和 latest-failure 单账号摘要继续刷新，但不再遍历 `account_config.account_ids` 中的 580 个配置账号。
- `upsert_operation_issue` 将历史累计账号与本次观测账号分开：累计集合继续保留在 issue 和计数中，只有本次 failure 账号更新 `operation_issue_accounts.latest_seen_at`；旧账号不会因别人的新失败被伪更新。
- issue action source 的未解决检查和代表 Action 选择改为 `operation_issue_sources -> actions -> tasks` 直接 JOIN，不再先物化数百 action ID 再生成超长 `IN (...)`。
- 新增 `runtime_summary_batches.py`：metrics 主指标提交后开启独立事务，生产默认每批 20、硬上限 100；缺失摘要优先，全部建立后按最旧 `updated_at` 轮转，失败显式传播且已提交快照不回退到 Planner 重算。
- TDD / QA 前验证：SQLite 边界增至 `10 passed`，覆盖失败批次重试、oldest 轮转、跨租户 JOIN 隔离和唯一索引契约；真 PostgreSQL 使用真实 `refresh_account_summary` 完成 6 批 `[100,100,100,100,100,80]`、`580/580` 且每批 `<10s`，同 issue 580 action sources `<5s`。最新两文件 `13 passed in 5.06s`，关联五文件 `43 passed in 6.20s`；全量 no-PostgreSQL `1288 passed, 817 deselected, 5 warnings in 54.67s`；新增模块 Ruff、compileall、`git diff --check` 通过。
- 硬指标处理：本次 metrics 责任拆为 `metrics_runtime.py`（100 行），issue JOIN / get-or-create 拆为 `runtime_issue_queries.py`（96 行），账号分页为 `runtime_summary_batches.py`（60 行）；新增/修改函数均 `<=50` 行、位置参数 `<=3`。历史大文件没有做无关大重构，且 `runtime_summary.py` 从 HEAD 1581 净降至 1570、`service.py` 从 HEAD 2896 净降至 2851。
- 独立 re-QA：关联 SQLite + 真 PostgreSQL `43 passed`；完整 no-PostgreSQL 按 60 秒硬门禁分两段 `664 + 624 = 1288 passed`，均 exit 0；新模块 Ruff / C901、compile 和 diff-check 通过。Critical / Important / Minor 均为 0，`qa_pass=true`；当前转 Product Acceptance，仍未发布、未取得 E4。
- 当前只到 Dev E2，`qa_pass` / `product_accepted` / 发布 / E4 均未声明；生产基线仍为第二轮 release `59d5c3e`、覆盖 `359 -> 361/2320`，长事务与锁链仍存在，太郎仍 `running`，Release Gate 保持 blocked。

### Planner 运营摘要职责拆分 Product Acceptance

- Product 只读复核提交 `8352fef0`：Planner 所有规划分支都以 `include_configured_accounts=False` 刷新统计，继续保留 task/target planned/success/failed/pending、oldest pending、runtime stage、latest failure、代表异常和 latest-failure 单账号摘要，不再遍历 580 个配置账号。
- observed / known 语义满足 Handoff：`issue.affected_account_ids` 保留历史累计 known accounts，`affected_account_count` 和既有分页关系不丢；只有本次 failure 的 observed accounts 才更新 `operation_issue_accounts.latest_seen_at`，不会因其他账号的新失败伪刷新历史账号。
- metrics 边界满足 Handoff：全局指标/heartbeat 先独立提交，账号摘要在新 session 中按缺失优先、最旧 `updated_at` 轮转并单批提交；单批失败显式抛出并整体回滚，下一轮重试同批，已提交快照不回退 Planner 重算。默认批量 20、硬上限 100，不会以 limit 永久漏掉账号。
- issue source 查询改为 `operation_issue_sources -> actions -> tasks` 直接 JOIN；同时约束 issue tenant、source tenant 和 Action tenant，跨租户 source id 不能把其他租户失败保留为当前租户 open issue。580 action sources 不再物化长 ID 列表。
- QA 证据：独立 QA `Critical / Important / Minor = 0 / 0 / 0`、`qa_pass=true`；真实 PostgreSQL 580 账号六批 `[100,100,100,100,100,80]` 均 `<10s`，580 action sources `<5s`，关联套件 `43 passed`；no-PostgreSQL `1288 passed`；新模块分别 96 / 60 / 100 行，相关 compile / diff-check 通过，旧大文件相对 HEAD 净减少。
- Product Acceptance：`product_accepted=true`（仅 E2）。实现满足 `de23f38e` Product Handoff，Release Gate ready；不代表已发布或生产恢复。E4 仍必须证明 planner/metrics/recovery 并发连续 3 cycles 无 `>60s` 事务、覆盖分子持续增长、太郎为 `completed/next_run_at=null` 且跨 recovery 保持，当前禁止写 `production_fixed`。

### Planner / Dispatcher 运行时统计与锁链 L3 Bug Batch Plan（resync）

- 生产 E4 结论：`fecdcfae` 发布成功，上一轮 `operation_issue_accounts=0` 单项记为 pass，不回退该职责拆分；整体仍为 blocked。21:48:58 稳定周期覆盖 `394/2320` 且无增长，太郎仍未取得 `completed/next_run_at=null` E4。
- Root Cause Group R1 — Planner / Recovery 历史 Action 全行读取：active planner `172.19.0.11` 的 `SELECT actions` 持续约 110 秒，recovery `172.19.0.8` 的 Action + Task executing membership 查询约 28 秒且反复重扫。Planner 热路径不得为统计、去重、准入或恢复加载任务全部 Action ORM 大行；Recovery 也不得在每轮物化全部 executing Action + Task。两者只允许按明确时间窗 / 状态 / action type 使用 id、task_id、status、account_id、lease/attempt、scheduled/executed 时间及确实需要的 payload 子字段窄投影，聚合使用数据库 count/min/max/window；恢复明细必须按不可变 keyset 稳定排序、有界 limit 和独立短事务继续，不能从头重复扫描同一集合。
- Root Cause Group R2 — Dispatcher 同步全历史 task stats：dispatcher-1 `172.19.0.23` 的 `SELECT actions.status, count(actions.id) ... task_id ... action_type NOT IN (...) GROUP BY` 持续约 107 秒。每条 Action 执行完成后不得在同一 Dispatcher 事务重扫该任务全部历史 Action；Dispatcher 只按本次显式 `old_status -> new_status` 做 O(1) 幂等增量，或写入待汇总事实，完整 task stats / membership exclusion / archived skipped / hard-hourly 纠偏由 metrics/reconcile 在独立事务完成。
- Root Cause Group R3 — Dispatcher 共享行写锁：dispatcher-4 `172.19.0.29` 在更新 `operation_targets/tasks` 时形成大量 transaction / tuple lock，现场 10 个 waiters。claim、Telegram/Gateway 调用、Action 结果落库、task/target 读模型刷新必须拆开事务：claim 短事务提交后再调用外部网络；结果事务只校验 action lease / attempt 并落 Action 与必要幂等状态；共享 task/target/issue 汇总由 metrics 批处理，不得让多个 dispatcher 为每条 Action 竞争同一行。所有多表写保持统一锁顺序，失败显式暴露，不得吞锁超时或伪成功。
- Root Cause Group R4 — 单任务每日覆盖规划 / 预约批次无界：21:50:51 planner `172.19.0.11` 的事务 age 220.6 秒，当前 SQL 为条件 `UPDATE task_account_daily_coverage SET state='reserved', reserved_action_id=...`，说明一次 `all_accounts_daily` build-plan 可在同一事务遍历并预约 580 条义务。实现必须把 `daily_coverage_plan_batch_limit` 定义为受代码常量约束的显式生产配置，首版单任务每批最多 20 条到期 `ready` 义务；查询按 `(coverage_date, targeted_at, account_id, id)` 不可变 keyset 排序。任务日继续游标使用独立于 `Task.stats` 的控制记录，以 tenant/task/date、last key、cycle/version 和 updated_at 标识进度；每批选择、Action 创建、条件预约和游标 compare-and-swap 推进在同一短事务原子提交，任一失败整批回滚且游标不前进。崩溃时从最后已提交游标继续，并在一轮到达末尾后回卷检查此前因 `next_eligible_at` / readiness 未到期而跳过的行；多 Planner 使用条件预约或 `FOR UPDATE SKIP LOCKED`，同一账本义务只能对应一个有效预约。批量上限只限制单事务工作量，不得缩小 580 分母、降低每账号每日目标、覆盖任务自己的 `messages_per_round` 业务配置、把未处理行改为 blocked/confirmed，或用截断结果宣称当日完成。
- 事实源 / 汇总表责任：Action / ExecutionAttempt 保持发送事实源，`TaskAccountDailyCoverage` 保持北京时间当日任务 × 群 × 账号义务及 580 分母事实源；任务日游标只保存调度控制进度，不能参与分母、完成率或成功判定。`unknown_after_send`、membership Action 排除、archived skipped、remote success 与失败原因语义不变；Task.stats、TaskRuntimeSummary、TargetRuntimeSummary 和运营 issue 是派生读模型。在线增量只接受带 action id / attempt / 状态转换幂等键的原子更新；metrics 必须周期性从 Action / ExecutionAttempt / coverage 事实源做 index-backed reconcile，发现漂移显式修正并记录时间/数量，不能让缓存、游标或增量计数成为唯一事实。
- 索引约束：E2 必须以真 PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` 证明 Planner / Recovery 窄查询、task 状态聚合、daily coverage keyset 批次和 metrics reconcile 使用与过滤顺序匹配的索引。Action 路径可选择 `(tenant_id, task_id, status, action_type, scheduled_at/id)` 的复合或等价 partial/index 组合；coverage 路径必须覆盖 `task_id + coverage_date + state + targeted_at + account_id + id`（或语义等价的 partial 组合），让 20 条批次不读取/锁定 580 条。新增索引必须走并发迁移、invalid/DDL 失败显式暴露；禁止只靠加索引保留 Dispatcher 每 Action 全历史重算或 Planner 单事务 580 条预约。
- 事务边界：Planner 普通单任务规划或 `all_accounts_daily` 单个 coverage 批次、Recovery 单个 keyset 批次、Dispatcher claim、Gateway 外部调用、Action finalize、metrics reconcile 必须是可独立观测的阶段；外部调用期间数据库事务必须关闭。coverage 批次提交前不得继续下一批，失败只回滚当前批且不推进游标；finalize 不同步刷新全量 task/target 统计；metrics 以显式 batch size、稳定游标和短事务提交，失败批次回滚并可重试，不能阻塞 Planner/Dispatcher 主业务事实落库。
- Product Design Complete：覆盖原始生产证据、四组根因、Planner/Recovery/Dispatcher/metrics 数据流、索引与迁移、每日覆盖稳定游标及分批预约、幂等/并发、unknown 结果、异常显式暴露、回滚和 QA/E4；`design_status=complete`、`resync=true`。该批次统一进入 dev，避免四处各自加临时 timeout 或局部 fallback 后继续制造锁链。
- E2 QA：在真 PostgreSQL 建立单任务 580 条当日到期覆盖债务、生产等量历史 Action、membership/unknown/archived-skipped 混合状态、2 个并发 Planner 及 4 个并发 dispatcher。要求 580 条债务在多个不超过 20 条的批次中完整进入有效预约 / 后续事实链，每个 planner task transaction `<5s`，中途崩溃重启、游标回卷和并发 Planner 均无漏预约、重复有效预约或分母变化；Planner 不出现无界 `select(Action)`，Recovery executing membership 以窄投影 keyset 分批、单事务 `<5s` 且不从头反复扫描。单 Action finalize 不执行全历史 `GROUP BY` 且事务 `<2s`；metrics 全量 reconcile `<10s` 并与事实源计数逐字段等价；并发执行无 deadlock、无持续 `>5s` lock waiter。测试须覆盖增量重复投递幂等、进程在 coverage select/Action create/conditional reserve/游标提交及 claim/Gateway/finalize 间崩溃、`unknown_after_send` 不重发、跨租户 task/action/coverage 隔离、迁移 upgrade/downgrade 与失败暴露。
- E4：发布实际 commit 后连续至少 3 个完整 planner/dispatcher/metrics cycles，数据库无 `>60s` transaction、无持续 `>5s` transaction/tuple lock waiter，旧 `operation_issue_accounts` 长查询继续保持 0；覆盖必须从 `394/2320` 在连续样本增长；太郎必须为 `completed/next_run_at=null` 并跨 recovery 保持。任一项缺失均保持 blocked，worker/container healthy 或单条查询变快不能替代整体 E4。
- 回滚：应用回滚不得撤销已验证有效的 0092 和上一轮运营摘要职责拆分；新增索引默认保留以避免放大锁风险。若新增增量汇总逻辑回滚，必须先停用对应写入者并由 metrics 从 Action 事实源完成一致性 reconcile；回滚本身不等于恢复。

### Planner / Dispatcher 运行时锁边界 Dev Handoff

- R1：Recovery 先以 `scheduled_at,id` 稳定顺序窄投影最多 20 个 executing Action id，再按主键加载有界明细；membership 补偿探测前保存必需标量并提交，外部 Telegram probe 不持有数据库事务。Planner 主路径不再物化无界历史 Action。
- R2：Planner 与 Dispatcher finalize 不再逐 Action 调用 `refresh_task_stats`。`metrics_runtime` 成为全历史任务统计 owner，每轮最多选择 20 个缺失或最旧 task summary，每任务独立事务 reconcile；失败保持显式，不回退到 Planner/Dispatcher。
- R3：延期 AI 生成前提交 claim/read 事务，生成后短事务写 payload / message memory；Telegram Gateway 仍处于独立无事务区间，随后短事务 finalize。AI provider 配额轮换在每次外部调用之间提交健康状态，不把下一次调用包在数据库事务内。
- R4：新增 `TaskDailyCoveragePlanCursor` 和 `daily_coverage_planning.py`。单任务日按 `targeted_at,account_id,id` keyset 选择最多 20 条 ready 义务；Action 创建、条件 coverage reservation 和最后实际成功 reservation 对应游标推进同事务提交，任一失败 rollback 不前进。到尾部回卷复检先前未到期行；任务自己的 `messages_per_round` 精确保留，10 为单批 10、30 为 20+10、60 为 20+20+20，未缩小 580 分母或每日目标。
- 0093：新增 cursor 表和 `ix_task_daily_coverage_plan_ready`、`ix_actions_task_stats_reconcile`、`ix_actions_executing_recovery`。真实 PostgreSQL 从空库完整 `alembic upgrade head`、downgrade 到 0092、再 upgrade 到 0093 均成功；并发索引通过 autocommit block 创建，invalid 同名索引/缺表/DDL 错误不吞掉。
- 真 PostgreSQL E2：2 个 Planner 并发将 580 条到期 coverage 在多个 `<=20` 批次中完整预约；预提交崩溃后 Action、reservation、cursor 全回滚，重试无漏/重复；整测 `1 passed in 3.32s`，且每个测得的 task transaction `<5s`。生产等量 40,741 条 Action 历史下，task stats reconcile `<10s`，Recovery 稳定返回 20 个 id 且 `<5s`，整测 `1 passed in 8.99s`。
- 当前阶段仅为 Dev E2 handoff：独立 QA、Product Acceptance、发布和生产 E4 均未完成，Release Gate 保持 blocked；不得据此声称 2320 自然日覆盖或评论任务已经生产恢复。

### Dev QA Rework（I1 / I2 / I3 / Minor）

- I1：Grok fallback 在读取 `TenantAiSetting.ai_enabled` 标量后提交 ORM 事务，再进入 `GrokCliBridge.generate`；桥接测试显式断言外部调用期间 `session.in_transaction() is False`，stage/provider metadata 保持不变。
- I2：新增 `recovery_claims.py`。stale executing 与 due unknown membership 都先按稳定顺序、最多 20 条执行 `FOR UPDATE SKIP LOCKED`，把 claim owner/token/expiry 持久化并提交；probe 后只有 claim token 仍归当前 worker 才能 finalize。真 PostgreSQL 双 recovery worker 同时领取 40 条时各得 20 条且集合不重叠，首批进入 cooldown 后下一批不会再次卡在相同头部。
- I3 Planner：真实 `group_ai_chat.build_plan` 验证 10 / 30 / 60 `messages_per_round`；60 场景在 active window 23:59 完整生成 `60×9 + 40 = 580`，23:30 的 568 是累计 pacing 应到量而非漏规划。coverage keyset `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 命中 `ix_task_daily_coverage_plan_ready` 且无 Seq Scan；跨租户 coverage 保持 ready 未被误占。
- I3 Runtime：40,741 Action 下 metrics 与 SQL status 分组逐字段等价且 `8.58s`，Recovery EXPLAIN 命中 `ix_actions_executing_recovery` 且无 Seq Scan；4 个并发 dispatcher finalize 全部 `<2s`、未执行 Action 历史 `GROUP BY`、结束后无 Lock waiter。
- I3 Migration：`test_runtime_stats_migration.py` 6 passed，覆盖 SQLite 实际 upgrade/downgrade、缺表、缺 cursor、invalid 同名索引和 DDL 原错透传；独立 PostgreSQL 迁移库再次通过 `空库 -> head -> 0092 -> head`。
- Minor：把本次新增的 coverage cursor 记录/推进职责提取为两个 helper，`group_ai_chat.build_plan` C901 维持基线 `70`，未由 QA 前报告的 73 继续上升。当前仍是 Dev E2 rework，必须重新进入独立 QA，之后再回 Product Acceptance；未发布且生产 E4 仍 blocked。

### Independent QA I1 -> Planner 外部 AI 事务 Product Resync

- Independent QA 新 I1 否定上一节的 AI 事务验收：真实 `build_plan` 在锁定 Task、`TaskDailyCoveragePlanCursor` 和 `TaskAccountDailyCoverage` 后，仍可沿 quality -> `generate_group` / `generate_reply` -> Grok 同步外呼。只在 Grok bridge 前 commit 不能覆盖 MiniMax、显式模型、reply 和 provider-backed 质量/改写；在 AI 返回后直接 commit 又会破坏 Action 创建、coverage reservation 和 cursor 推进的 Phase A 原子性。当前 Dev E2 / Product Acceptance 因此失效，Release Gate 继续 blocked。
- Phase A Planner：每批最多 20 条，只在短数据库事务固定 Cycle/slot/account/reply target/profile/topic/teacher/质量规则，原子创建 `ai_generation_status=pending` Action、条件预约 coverage 并 CAS 推进任务日游标；Planner 禁止 AI、Grok、Telegram Gateway、远端上下文采集等所有网络外呼。30/60 `messages_per_round` 仍映射 20+10 / 20+20+20，同 Cycle slot 不因事务切批改变。
- Phase B Dispatcher：短事务 claim 并写 generation lease/attempt 后提交；reply 与 normal 分批，在无数据库事务区间完成 reply 目标复核、normal 最新上下文刷新及全部 provider-backed 生成、fallback、改写和质量轮次。输出必须按 `slot_id` 与 Phase A 的 account/coverage 一一对应；目标消息消失、过期或不可引用时不转 normal、不调用 Telegram。
- Phase C Dispatcher：以 lease/attempt 条件短事务完成 slot 映射校验、内容、消息记忆、指纹/语义重复、内容政策和质量结果落库；质量失败或 reply 失效必须在同事务终结对应 Action 并释放自己的 coverage。通过后才进入现有无事务 Telegram Gateway 和短事务 finalize；每个 Dispatcher 数据库阶段 `<5s`。
- 幂等 / unknown：Action、dedupe key、cycle/slot 和 coverage reservation 跨生成重试保持不变。AI 成功但 Phase C commit 失败时群内尚无可见副作用，旧 generation attempt 记 `ai_result_persist_unknown`，恢复只重试同一 Action/slot且不得创建第二个有效预约；该状态不得混同 Telegram `unknown_after_send`。Phase C 已 ready 的 Action 重复消费不得再次调用 AI。
- E2：断言 Planner 对所有外部入口调用数为 0；验证 Phase A 任一失败整批 Action/coverage/cursor 回滚，580 债务、10/30/60 Turn 映射、reply 目标消失、normal 最新上下文、同批 slot 缺失/额外/重复、质量失败释放、AI 成功后 DB commit 失败恢复、双 Dispatcher claim 和跨租户隔离。Phase A、Phase B claim、Phase C、发送前检查和 finalize 每个事务均 `<5s`，外部 AI / Telegram 调用期间 `session.in_transaction() is False`。
- Product Design Complete：专项设计 `docs/03-feature-designs/ai-group-dispatcher-ai-generation-transaction-design.md` 与 DF-167/168、BG-004/A/005、PERF-007 覆盖说明已定义原始需求、前后端状态、Worker/API/数据流、权限安全、reply/normal 边界、并发幂等、质量失败、生成/发送 unknown 分层、迁移回滚和 E2/E4。`design_status=complete`、`resync=true`；进入 dev 后必须再次独立 QA 和 Product Acceptance，禁止沿用上一轮 `qa_pass`。

### Independent QA I2 / I3 Deterministic Rework

- I2 根因是 Recovery 的裸 `FOR UPDATE SKIP LOCKED` 同时锁住 JOIN 的 Task 行，同一 Task 两个 worker 因而退化为 20/0。改为 `FOR UPDATE OF actions SKIP LOCKED` 并给 Action/Task JOIN 增加 tenant 相等约束后，同 Task 与跨 Task 都稳定并行领取 20/20；跨租户 Action 引用不会被领取。
- I3 将 stats 和 runtime summary 的 Action 状态计数统一为 tenant + task、`count(*)` 的共享 SQL，并把 `ix_actions_task_stats_reconcile` 调整为 tenant-leading。40,741 Action 经 `VACUUM ANALYZE` 后，对实际 production statement 的 EXPLAIN 命中目标索引且无 Seq Scan；跨租户伪造 Action 不进入任一汇总。
- 定向真 PostgreSQL 证据：runtime stats 全文件连续两轮 `5 passed`（10.76s / 10.38s），tenant isolation `3 passed`，Recovery lock scope `1 passed`。本节仅恢复 I2/I3 的 Dev E2，I1 仍按上一节专项三阶段合同待实现；独立 re-QA、Product Acceptance、发布和生产 E4 均未完成。

### AI 活群与频道评论 Phase A/B/C 当前流转

- `prod-diagnosis`：2026-07-14 17:30-17:41 CST 复查时，生产仍运行 `fecdcfae`，不是本轮 `9e26d08c..6bf6dfce` 实现。Planner、dispatcher-2/3/4 不健康，仅 dispatcher-1 健康；数据库有 20 个超过 60 秒的事务、21 个阻塞会话，累计 deadlock 351。当天最近可读的 15:34 覆盖快照仅为 4 个任务各 `13/580、8/580、9/580、18/580`；刷新本身又被锁链阻塞，因此结论保持 `production_blocked`，不能写 `production_fixed`。
- `product`：提交 `9e26d08c` 已完成 Product Design Complete 和数据流 resync。统一契约是 Planner 只使用持久事实创建稳定蓝图；Dispatcher 在无活动数据库事务时调用 AI，再以 generation lease/attempt CAS 完成 Phase C 质量与结果落库；`ai_result_persist_unknown` 与 Telegram `unknown_after_send` 分层。频道引用回复固定已选目标与规则版本快照，目标失效不降级为普通评论。
- `dev / group_ai_chat`：`fe7aa6c5..23611480` 将 AI 活群 Planner 收敛为 Phase A：每批最多 20 条，固定 Cycle/slot/account/coverage/reply/画像/话题，原子创建 `pending` Action、预约 coverage 并推进游标；`ai_generation_*` 承担 Phase B 无事务 Provider/Grok/远端 reply 复核和 Phase C slot 映射、内容政策、消息记忆、质量、CAS、恢复。Action/slot/coverage 在重试中保持不变。
- `dev / channel_comment`：`9e6dd0a2..6bf6dfce` 将评论 Planner 收敛为只读取已采集频道消息并创建空文本 `pending` 蓝图；任务级事务 advisory lock 在准备提交后重新取得，双 Planner 串行 Action 创建且不重复蓝图。failed/skipped 释放预算与 slot，pending/claiming/executing/success/unknown 占用。`comment_generation_dispatch.py` 在 claim 提交后生成 direct/reply 评论，Phase B/C 都复核源消息仍可评论；`comment_generation_quality.py` 使用 Planner 固定的规则版本快照、完整非空 Action/远端历史和统一 outbound policy。`0094_channel_comment_history.py` 为 modern/legacy 历史查询提供 partial index；失效源消息/reply 不调用 Provider/Gateway、不转 direct，stale CAS 释放账号运行时预约，重试按 attempt 清理旧 Provider 标记并终结审计。
- `dev / worker`：`b5374a09..466585b3` 让独立周期线程同时刷新数据库 heartbeat 与本地 Docker health 文件；本地文件写入串行并原子替换。长 drain 不再只靠主循环返回后刷新健康事实，写失败继续显式记录。
- `qa / E2 当前证据`：群聊边界测试覆盖 Planner 外部入口零调用、10/30/60 Turn 有界切批、slot/account/coverage 映射、质量失败释放、stale CAS、生成落库未知恢复和真 PostgreSQL 双领取；评论边界测试覆盖 Planner 零 AI/远端采集、预算状态补位、direct/reply 生成、固定 reply/规则快照、源消息关闭、超过 50 条空蓝图不遮挡历史成功、stale runtime reservation 释放、attempt 审计/Provider 标记重置、persist-unknown、真 PostgreSQL 双 Planner/Dispatcher 与 reply/unknown 二次领取；0094 覆盖 SQLite 升降级、缺表/invalid/DDL 失败暴露。worker role 测试覆盖长 drain 期间 DB/local 双心跳和并发文件写。以上 Dev 定向证据均已纳入最终 E2 QA。
- `qa / Task 3`：频道评论最终独立复核 `39 passed`，上轮 4 个 Important 全部关闭；真 PostgreSQL direct 双 Dispatcher、reply、unknown cache 二次 claim 通过。0094 在真 PostgreSQL 重复 upgrade/downgrade 成功，modern/legacy 查询分别命中两个目标索引；相关 compile、diff-check、文件/函数硬限制通过，并已纳入最终整体 QA。
- `qa / Task 2 concurrency`：`6bf6dfce` 的真 PostgreSQL 双 Planner 确定性竞态得到创建数 `[0,2]`、最终仅 2 个 Action 且 `<5s`；并发用例连续 10 次和提交后复验均通过。Task 2/3 相关回归、ruff、编译、diff 与代码指标通过；调试输出未进入提交。
- `qa / final E2`：HEAD `8359662a` 最终分组执行共 `793 passed / 0 failed`，覆盖群聊 Phase A/B/C、频道评论 Planner/Dispatcher/0094、coverage/recovery/worker、dispatcher/capacity/task limits、operations/runtime summary 与真实 PostgreSQL 并发边界。相关 Ruff、compileall、merge integrity、`git diff --check` 及生产/测试代码 metrics gate 均通过；`qa_pass=true`，Critical / Important / Minor 为 `0 / 0 / 0`。
- `product acceptance`：`product_accepted=true`（仅 E2）。实现覆盖 Planner 蓝图、无事务 AI、Phase C 质量/CAS、生成/发送 unknown 分层、评论预算并发和 worker 双心跳，允许进入 `master -> release` 发布流程；这不等于代码已上线或生产恢复。
- `配置边界`：郑州任务当前仍绑定 `account_group_id=9 / selection_mode=natural`，不是全账号口径；青岛任务仍为 paused，且存在失效 `@qdsfxy` 与账号可发送能力不足事实。这些是业务配置 / 授权资产 blocker，不属于本次事务边界代码修复；未获产品确认前不擅自改为 all、不恢复青岛任务。
- `评论生产事实`：15:34 快照中阿哥日记为当天 12 条 success、11 个账号，最新成功约 15:12，仍有 9 条 overdue；17:41 刷新没有取得新的 success + ExecutionAttempt remote message id 组合。评论链路因此仍为 `unproven/blocked`，worker healthy 或本地测试通过都不能替代真实远端成功。
- 当前阶段：`prod-diagnosis -> product -> dev -> qa -> product` 的 E2 流转已完成，Release Gate 已具备发布条件；代码尚未合入 `master -> release`，本轮生产发布和发布后 `prod-diagnosis` E4 均未执行。生产状态继续为 `unproven/blocked`，禁止写 `production_fixed`。

### Release checks rework 与最终 Product Acceptance

- 首次 release merge `b28bd72b` 的 Deploy Production run `29359103999` 在 checks 阶段失败，`20 failed / 2191 passed / 14 skipped`；镜像构建和 deploy 均未执行，生产继续运行 `fecdcfae`，没有半发布。
- 第二次 release merge `7aef6a41` 的 run `29362258741` 收敛为 `2 failed / 2209 passed / 14 skipped`，仍在 checks 阶段停止。剩余两项同属 UTC/北京时间跨日测试数据错位；全库 `coverage_date=date.today()` 的 9 处测试账本已统一为 `beijing_now().date()`，不改变生产业务代码。
- 失败根因分为三组：旧 workflow 测试仍把异步 metrics 当成 Dispatcher 热路径同步统计；UTC runner 在北京时间跨日窗口用 `date.today()` 建覆盖账本；前序 generation recovery 测试留下 running task 的 due pending Action，令后续全局双 Dispatcher 测试分别领取两个不同 Action。
- 严格 workflow 回归进一步暴露真实缺口：生成重复/质量失败已把 Action 置为 `failed`，但 `_handle_ai_generation_failure` 提前返回且未释放账号 runtime reservation，下一周期持续命中 `account_inflight_conflict`。实现只在该终态分支释放预约，不恢复每 Action 全历史 stats 写入，不接受 pending 作为成功。
- 测试修复保持生产边界：统计断言先显式运行 metrics worker；动态频道评论先持久化 listener 输入；Deferred AI 输出按稳定 slot 映射后由目标 task dispatcher 实际发送。覆盖日期统一使用北京时间；STARTED_SCOPE 通过 finalizer 清理 Action、Task 和 SchedulingSetting。
- re-QA：原 12 个失败 workflow `12 passed`；`test_workflow.py` 全文件 `104 passed / 14 skipped`；北京时间 `TZ=UTC` 的 5 个 coverage/dispatch/material 文件 `60 passed`；generation recovery + 评论并发 + generation phase + coverage 组合 `38 passed`；前序 recovery 与双 Dispatcher 原失败顺序 `2 passed`。compileall、diff-check、变更函数 `GROWN_OVER50=0 / NEW_OVER50=0` 通过，Critical / Important / Minor=`0 / 0 / 0`。
- Product Acceptance：`qa_pass=true`、`product_accepted=true`（仅 E2），Release Gate 再次 ready。生产仍为旧 `fecdcfae`，真实 worker、长事务、覆盖矩阵和评论远端结果必须在新 release 上重新取得 E4，当前仍禁止写 `production_fixed`。

### 生产发布与 Runtime Retention 三次 rework

- 第三次 release merge `a535ccc8` 的 Deploy Production run `29363008024` 已完整通过 checks、frontend build、backend/frontend image build、SSH deploy 与 Grok bridge；生产 `current` 已切到 `20260714195333_a535ccc`，backend、planner、4 个 dispatcher、listener、recovery、metrics、account 与 ai-memory worker 曾全部转为 healthy。
- 部署启动被旧容器地址 `172.19.0.8` 的 1 小时事务阻塞：该会话在全量删除 actions，另一个旧 heartbeat 会话及 `0093` 并发索引迁移均被它阻塞。确认旧容器已移除后终止两个陈旧会话，迁移、后端和全部 worker 随后正常启动。
- 新 recovery 启动后同一问题再次真实复现：`cleanup_runtime_details` 每轮无上限加载所有 5 天前 Action，并在单事务中全量汇总、删除 Attempts/Reviews/Actions；新会话再次连续删除超过 6 分钟，数据库查询延迟升至分钟级，评论动作长期停留 claiming/pending。这证明根因属于当前 retention 实现，不是旧会话偶发残留。
- Product Handoff：保留 5 天 retention、逐维汇总、审计和显式失败合同；每个 recovery 周期只处理本轮 `limit` 个最老 Action，子表与 Action 同事务删除，每批独立审计；DailyRuntimeStat 对后续批次做累加，不能覆盖前一批汇总。覆盖账本和入群长期记录保留业务快照并清空过期 Action 引用，动作专属搜索降权预约随 Action 删除。禁止跳过清理、吞错或扩大 retention 作为 fallback。
- Dev：`cleanup_runtime_details` 增加确定性最早创建优先批次并用 `FOR UPDATE SKIP LOCKED` 防止双 worker 重领；recovery 将自身 `limit` 作为批量（生产 100）；统计改为 PostgreSQL/SQLite 原子 upsert 累加。生产 EXPLAIN 从 effective-time 排序的 `Seq Scan + Sort cost 39608` 收敛为复用 `ix_actions_created_at` 的 `Index Scan + Incremental Sort first-100 cost 533`。生产 recovery 在修复发布前显式停止，并终止其已失去客户端的长 DELETE 会话，其他业务 worker 保持 healthy。
- re-QA：新增红绿回归先稳定暴露不支持 `batch_size`，修复后验证 3 条历史 Action 按 2+1 两轮删除且全局 total 累加为 3；真 PostgreSQL 外键回归覆盖 Coverage 2 字段、Admission 4 字段与 SearchRank reservation；双 session 并发稳定取得 1+1 不重复批次且原子汇总为 2。合并 retention/role/recovery/runtime stats/tenant isolation/generation recovery 共 `40 passed`，workflow `104 passed / 14 skipped`。本轮仍需完整 Release Checks 和新版本生产 E4，当前不得写 `production_fixed`。
- 第四次 release `2ca937bf` 的 run `29367174749` 完成 checks、镜像和部署，生产 11 个 worker 全部 healthy；但 09:00 后 E4 显示每个 100 Action 批次仍耗时约 1 至 3 分钟。生产 `pg_constraint + pg_indexes` 证明 9 个 Action 外键中 7 个引用列无 leading index，导致 Action DELETE 的外键校验反复扫描 Coverage、Admission、Review 表；recovery 已再次停止，群与评论 worker 保持运行。
- 第二层 rework：0095 对 Review action、Coverage reserved/last-success、Admission membership/test/delete/rescue 共 7 列使用 `CREATE INDEX CONCURRENTLY`，模型元数据同步；不改变 retention、任务窗口、消息条数、账号范围或完成判定。迁移幂等/可逆和 PostgreSQL concurrent DDL 加入自动化，相关组合 `128 passed`；待再次发布后用每批时长、无锁等待、评论远端 ID 与每日覆盖增长做 E4。
- 0095 首次 release merge `496f00a3` 的 run `29381873407` 在 checks 阶段以 `2 failed / 2214 passed / 14 skipped` 停止，未 build/deploy。失败一是 merge integrity 仍硬编码 0094 head；失败二是旧 AI unavailable 测试使用 `messages_per_round_mode=auto`，在 09:35 业务时间生成 3 个到期 slot 却断言 1。更新 head 断言并把该单轮合同固定为 manual 1 后，原失败顺序与相关组合 `130 passed`；生产逻辑和任务配置未改。
- 稳定版 release `f085716c` 的 run `29382245541` 已完整通过 checks、镜像构建和 SSH deploy，生产切到 `20260715014914_f085716`，Alembic 为 0095，11 个 worker healthy。发布后持续采样确认 retention 的分钟级 DELETE 已消失，但暴露两个后续根因：metrics 对单任务历史 `send_message` 的 `payload.ai_generation_status` / `result.generation_outcome` 做全量 JSON `GROUP BY` 达 1 分 51 秒；planner 的 hard-hourly 近 24 小时 Action 查询达 43 秒，且缺少 tenant 过滤与 executed_at 对应索引。
- 0096 行为保持型 rework：为两条 JSON 聚合表达式增加 partial concurrent index，为 `(tenant_id, task_id, action_type, executed_at)` 增加并发索引，使 executed/scheduled 的 OR 两侧都有可用访问路径；聚合由等价的 `count(id)` 改为 `count(*)`，hard-hourly 查询补 `tenant_id` 条件。跨租户红测在修复前得到 1、修复后为 0；迁移 SQLite 幂等/可逆、PostgreSQL concurrent DDL、唯一 migration head 和相关业务组合共 `67 passed in 20.07s`。当前仍仅 E2，必须发布后用生产 EXPLAIN、连续慢事务采样及真实群/评论远端结果完成 E4。
- 0096 release `5ebb133b` 的 run `29384560377` 完整成功，生产三条索引均 `indisvalid=true / indisready=true`，全部 worker healthy；当天远端覆盖增长到天津 `10/580`、郑州师范 `8/580`，阿哥评论取得 Telegram remote id `325`。但 fresh E4 同时否决了初版查询性能：SQLAlchemy 将 JSON key 编译为 `$1` 参数，PostgreSQL 无法把参数化表达式匹配到固定表达式索引，现场相同聚合仍运行 146 秒。rework 只对 PostgreSQL 的两个内部固定键生成 literal expression，SQLite 保持方言 JSON 表达式；不接受外部 key、不拼接用户输入。新增 PostgreSQL query-shape 红测锁定字段名不得绑定，相关组合更新为 `68 passed in 18.17s`；待二次发布后重新验收索引命中和无长事务。
- 二次 release `9a92c85a` 的 run `29385689365` 成功，实际 metrics SQL 已固定 JSON key，生产 EXPLAIN 使用 `ix_actions_ai_generation_status_counts`。持续采样又定位到独立的 `_next_cycle_index`：只投影 `payload.cycle_id`，按 task/type/action 过滤后 `created_at DESC LIMIT 200`，仍缺 tenant 条件和对应排序索引，现场运行 47 秒并使 task stats UPDATE 等待。0097 新增 `(tenant_id, task_id, action_type, created_at DESC)` concurrent index，并为 cycle index、recent sent/planned message、account memory等同任务读取补 tenant 隔离。跨租户 `cycle:99` 红测、迁移幂等/可逆/并发 DDL及相关组合 `70 passed in 19.85s`；待第三次发布做最终慢事务采样。
- 第三次 release `90445cb6` 的 run `29388507586` 成功，0097 valid/ready 且 cycle 慢查询消失；但全历史 JSON `GROUP BY` 在当前生产磁盘负载下仍运行 93 秒。相同表达式的精确状态等值查询命中 0096，实际执行 `110ms`。第四层 rework 因而只聚合 UI 合同需要的 4 个闭合生成状态和 10 个质量/引用 outcome；任意生成失败码仍完整统计，但只在 `Action.status=failed` 的窄集合内分组，避免扫描全部成功历史。原相关 70 项及 generation observability/dataflow 17 项通过，失败统计口径未降级；待第四次发布复验。
- 第四次 release `cc6b5489` 的 run `29389579502` 完整成功，11 个 worker 全部 healthy；fresh E4 仍发现未知生成失败码的窄集合 `GROUP BY` 运行 36 秒。`generation_failed_count` 的产品合同只消费精确总数，不消费失败码分组，故第五层 rework 改为对全部非闭合生成状态直接 `COUNT(*)`，语义与原实现一致；0098 只索引 `(tenant_id, task_id)` 且 predicate 锁定 `send_message + 非闭合状态`，避免读取成功历史 JSON 堆页。迁移 idempotent/reversible/concurrent 红测转绿，相关 `15 passed in 16.39s`；第四次 E4 已否决，待第五次发布复验。
- 第五次 release `6a4aa86e` 的 run `29390817312` 完整成功，0098 `indisvalid/indisready=true`、大小 `184 kB`，11 个 worker 持续 healthy。生产 `EXPLAIN ANALYZE` 对天津失败总数使用 `Index Only Scan`，总执行 `125.856ms`；间隔采样得到 active 超 20 秒查询 `0`、旧 generation `GROUP BY` `0`、旧 cycle scan `0`。评论任务阿哥日记当天 10:31 的 Action 与 ExecutionAttempt 均 success、remote id 均 `325`，后续 15:00 至 20:00 动作为未来排期。四保证群当天确认覆盖为天津 `13/580`、石家庄 `31/580`、郑州师范 `11/580`、郑州楼凤 `0/580`，合计 `55/2320`；运行时事故得到 E4 修复证据，但大量账号在线探测 blocked、楼凤无远端成功且北京时间自然日未结束，整体 done_status 保持 `not_done`，禁止写 `production_fixed`。
- 第五次发布后的业务失败分组进一步定位到恢复缺口：生成阶段 `ConnectionTimeout` 会由 dispatcher 延后 10 秒重试，但 payload 仍是 `generating`；下次 claim 不会覆盖 generation token，随后以“send_message action 缺少可发送文案”终结。修复复用既有 `recover_stale_pre_gateway_generation`：provider 前失败重置为 `pending`，provider 后未知写成 `ai_result_persist_unknown`，再叠加 `dispatcher_db_error` 重试审计。回归红测确认旧行为保留 generating，修复后 pending/token 清空；相关 DB error、生成恢复组合 `8 passed`，待第六次发布验证失败链消失和楼凤首次远端成功。
- 第六次 release `cf4ecd40` 的 run `29392387289` 成功，11 个 worker healthy；遗留楼凤生成动作在 lease 到期后由 recovery 转为 `generation_recovery/retry_pending`，随后按旧 13 点硬窗口过期合同跳过。手工执行标准 planner drain 时再次捕获固定 4 状态 JSON `GROUP BY` 运行 22 秒，说明此前仅消除失败码分组仍不足。第七层 rework 对固定 4 个生成状态与 10 个 outcome 分别执行等值 `COUNT(*)`，复用 0096 表达式索引，彻底删除 JSON 分组；SQLite statement 红测锁定 payload/result JSON 不得 GROUP BY，相关组合 `43 passed in 17.36s`，待第七次发布后重跑 planner drain 并核对楼凤远端成功。

## Release Gate 与 E4

1. 按 `master -> release -> GitHub Actions Deploy Production` 发布并核对实际镜像 commit。
2. 发布后确认 backend、planner、account-online、dispatcher、recovery 和评论相关 worker heartbeat / Docker health。
3. 确认 Planner drain 不再长事务停滞；ai-memory 连续至少 3 个 60 秒 maintenance 周期无 `>60s` 事务、无 `action_id` 点查引发的 `DataFileRead` / heartbeat 锁链；account eligibility event 无异常积压，online missing / stale / blocker 可见且持续下降。
4. 对 4 个 AI 活群任务核对 due debt、Action 创建、远端成功和任务 × 群 × 账号矩阵；只有完整北京时间自然日全部 2320 项由 Telegram 远端成功证据覆盖，才能写 `production_fixed`。
5. 评论任务必须另列 `pass / blocked / unproven`，核对任务状态、最近规划、执行、远端结果与错误；worker healthy 不能替代评论成功证据。
6. 对 lifetime-cap 已完成评论任务主动触发 recovery drain / 容器重启后复核吸收终态；任何 `completed -> running`、`next_run_at` 重建或新增 Action 都阻断 Release Gate。

## 回滚

- 应用回滚走正常 release 提交，默认保留与旧应用兼容的 0091 复合索引，避免回滚应用时额外扩大数据库锁风险。
- 若索引本身必须回滚，应在维护窗口执行 0091 downgrade 的 `DROP INDEX CONCURRENTLY`，并核对 Alembic current 与目标 revision；不得在业务高峰直接删除索引。
- 旧应用会恢复消息记忆完整 ORM 查询；应用进程存活、迁移回退或索引删除都不等于本次长事务事故与 AI 活群业务恢复。
