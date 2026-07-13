# 2026-07-13 AI 活群 Planner 规模治理

## Intake Card

- `intake_id`: `intake-2026-07-13-ai-group-planner-scale`
- `bug_id`: `bug-2026-07-13-ai-group-planner-stall`
- `level/lane`: `L3 / ai-group-quality/planner-runtime`
- 用户目标：监督修复生产 AI 活群，确保每个目标群中的全部账号按北京时间每日真实发言一次，并检查评论任务运行状态。
- 当前状态：第二轮 release `59d5c3e` 已上线，0092 有效，生产 `action_id` missing 点查 `0.485ms`；但 active planner client `172.19.0.28` 仍出现 `>60s` 事务，当前语句为 `UPDATE operation_issue_accounts`。fresh L3 根因定位到 Planner 在同一事务为 580 个账号逐个刷新运营摘要，覆盖仅从 `347` 增长到 `359/2320`，太郎 lifetime-cap 任务仍 `running/next_run_at` 早于当前时间。新 Product Handoff `design_status=complete`、`resync=true`，实现/QA/发布前 Release Gate `blocked`，禁止写 `production_fixed`。

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
