# AI 活群供给与故障兜底 Release Gate

- intake_id: AI-GROUP-SUPPLY-FALLBACK-20260910
- level: L3 / P1
- release_mode: github_actions
- release_owner / rollback_owner: 本任务
- status: pending（本地定向 QA 已通过；完整 Prepare 与生产 E4 待核验）

## 上线范围

按专项 PRD 修复已准入账号供给分页与 portfolio 分配、同真实目标 membership canonical 投影、无上下文独立主题、同原义务模型候选切换及应急签到/表情、来源排期空隙与既有账号窗口交集。配置/API/页面显示应急开关与独立质量分账。保留历史未知调用、数量义务、失败证据及真实权限校验。

## 必须满足

- 产品设计与反向检查：`ai-group-executable-supply-and-fallback-20260910-prd.md` design_status=complete，全部相关专项与索引已 resync。
- 代码审查：分别复核供给/投影、来源排期/Provider 切换、主题/应急/Gateway；发现的当前 payload 身份、原维护误释放、冻结候选与缺主题入口问题已修复并加反例。
- backend_tests：根 backend/.venv、每轮硬超时60秒。第一轮广覆盖28文件共297 passed；应急/主题/维护/未知53 passed；供给9文件127 passed；模型切换15项新测试及55项既有回归通过。真实 PostgreSQL 来源争用、准入追加、应急行锁/旧 token CAS 已通过。最终补充 QA 结果在发布证据中记录，不将重复运行相加声称独立用例数。
- frontend_build：TypeScript + Vite production build 通过；既有 chunk size 提示，无编译失败。
- migration_impact：0230 仅新增不可重复的应急内容选择事实表，不修改业务存量。旧版本增量升级链及 merge/head 检查通过；模型列/约束在 PostgreSQL 验证。已产生事实时禁止删除表降级。
- worker_impact：正式 all-worker cutover，保留已 Gateway-started unknown；不触发附加维护、任务重跑或扩大账号窗口。
- external_platform_impact：正常与应急共用原 Action/quantity/账号/群/真实引用，正式 Gateway 前校验成员、发言与内容权限；已 Telegram unknown 不替代不重发。
- rollback_plan：未发生新选择事实时可经正式发布回滚应用；有新选择事实后保留表及历史审计，先暂停候选发布并前向修复，禁止删事实制造可重发。源排期历史 release 不作批量回拨。
- observe_window：以 Deploy Production 完成时间为起点，对原10 running Task 读取当前日 ledger、入群供给、正文路由、来源排期与 typed 消息。深夜无原窗口的任务标明待窗口/未证实，不能以健康检查写 production_fixed。

## 发布后复核

- Prepare 全量检查和镜像必须通过后，master → release → Deploy Production。
- 独立核对 current SHA、backend/worker health、API 和 Alembic head。
- 逐 Task 输出 post-release typed 消息、独立主题/应急选择与已送达事实、未准入与 unknown 缺口。历史错绑成员记录不自动视为当前事实。

最终补充：模型切换71项非PG与4项PG通过；canonical membership74项及helper调整后91项通过；主题Gateway正/负例6项通过；typed独立质量计数11项通过，含缺fact/unknown/未call/wrong account/tenant/obligation/旧fact/错日/错mutation反例。

Prepare 34381205676（6a68f685）首轮：两个PostgreSQL分片、前端及镜像全部通过；4个no-postgres分片发现旧自然机会合同断言、UTC fixture及不完整测试替身。已修正测试真实合同，不削弱生产门槛；UTC下5个相关文件49 passed（6.93s），自然机会真实落库回归27 passed。候选需重新完整Prepare，首轮失败不豁免。
