# Findings & Decisions

## Requirements
- 用户授权完成代码修复、正式发布和线上业务验证。
- 需求/行为变化先更新 PRD/专项设计/索引，再实现。
- 发布必须走 `master -> release -> GitHub Actions Deploy Production`。
- L3 只有修复后真实 Telegram E4 才能写 `production_fixed`。

## Production Findings
- 生产 SHA 为 `9d99319f`，核心 worker active。
- 5 个 running AI 活群中仅 `郑州大学` 停发；其他 4 个在最近 15 分钟均有真实成功。
- 目标任务当日 `due=3848, confirmed=805`，20:57 后无远端成功，open Action 为 0。
- 20:57 至 22:48 产生 92 个 `account_mask_evidence_missing`，全部 `ExecutionAttempt=0`。
- 806 个 ready admission 中 805 个有 active 面具；账号 949 是唯一缺面具账号。
- 账号 949 在线且探活新鲜，但面具生成因 `voice_profile_provider_timeout` 进入 `manual_required`。
- extra-volume 候选按成功数最少排序，账号 949 因 0 成功稳定排第一；它不在当前 ledger coverage 中。
- Action 无 `coverage_ledger_id`、`primary_quantity_slot_id`、mask id/version/hash，`mask_status=missing`；GenerationJob 仍 ready，Dispatcher 在 Gateway 前正确拦截。

## Contract Findings
- 专项面具 PRD：缺面具账号只能履约自己的 coverage 签到，不得承担 extra-volume；其他账号必须继续。
- 群日目标 PRD：extra-volume 只能从当日已覆盖账号中选择。
- 当前 `_daily_group_extra_accounts` 只过滤在线与 admission，没有要求当前 ledger confirmed coverage 或 active mask。
- `TaskAccountDailyCoverage` 可用 `task_day_ledger_id/state/confirmed_count/target_count` 判定当日覆盖完成；`voice_profile_prompt_details` 只返回 active、质量 active 且摘要非空的面具，可直接作为候选资格真相源。
- 既有测试只证明“缺面具 extra-volume 不能签到”，没有覆盖“候选层必须排除并继续选择其他账号”。
- 主 PRD 的“缺面具签到可计 coverage 与群日总量”表示 coverage 签到这一次远端消息同时计两类统计，不等于允许缺面具账号承担独立 extra-volume；本次需显式消除该歧义。
- 当前专项验收已有“600 个可用账号 + 1 个缺面具账号时其他账号继续”的原则，但没有绑定到当前 ledger `confirmed` 身份与 Planner 候选查询的具体回归。
- 数据流索引已有 extra-volume 数量槽边界，却没有写明 fact-first JIT 候选还必须是本 ledger 已 confirmed 且面具 active。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 在 extra-volume 候选查询前建立当前 ledger confirmed account 集合 | 与群日目标身份一致，避免非冻结/unknown/abandoned 账号补量 |
| 要求存在 active + quality active + non-empty summary 的面具 | 与 Dispatcher 冻结证据门禁一致，避免生成后才失败 |
| 保留 Dispatcher `account_mask_evidence_missing` | 它是必要的发送前 fail-closed 防线 |
| 添加混合候选回归 | 证明一个缺面具账号不会饿死多个合格账号 |
| 本次不自动触发/重试面具生成 | 用户授权是代码修复和发布，不包含生产数据/作业写入；且单账号恢复不应成为任务恢复前提 |
| 全部候选不合格时不创建 Action | 沿用显式账号不足状态，不引入 silent fallback；Dispatcher 的 fail-closed 仍作为最后防线 |

## Release Findings
- `origin/master=489d41eb`，`origin/release=9d99319f`。
- 历史分叉为 release 独有 59、master 独有 7，但 `git diff origin/release..origin/master` 为空，树完全一致。
- 修复在 master 提交后，可从 release 分支 merge master 形成正常发布合并，不需 force push。
- `Deploy Production` 只监听 `release` push；CI 会分别运行 `no_postgres` 与 `not no_postgres`，再构建镜像和部署。
- `production-task-monitor.yml` 可在发布后用明确 release anchor、expected SHA 和目标 task IDs 做只读 E4 复核。
- 本修复不需要迁移、前端构建逻辑变更或生产数据写入；worker 影响为 Planner 候选选择与 AI 生成量恢复。
- 回滚边界：代码改动本身可回到上一不可变 release，但发布流程包含既有 takeover/migration 步骤，正式发布前需以 workflow 实际输出确认无新迁移。

## Resources
- `backend/app/services/task_center/executors/group_ai_chat.py`
- `backend/app/services/task_center/dispatcher.py`
- `backend/tests/test_mask_missing_check_in.py`
- `docs/01-product/tg-ops-platform-prd.md`
- `docs/03-feature-designs/ai-account-mask-initialization-reliability-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`
- `docs/05-implementation/multi-agent-practice/runs/2026-08-08-ai-extra-volume-mask-recovery.md`

## Visual/Browser Findings
- Chronicle 当前画面没有显示任务详情页，因此未用屏幕内容认定任务 ID；生产 DB 对全部 5 个 running AI 任务逐项对照后锁定 `郑州大学`。
