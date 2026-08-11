# AI 活群与浏览到期履约整体修复计划

## Goal

修复生产环境 AI 活群与浏览任务的到期量核算、物化和执行节奏，安全处置既有错误积压，并以发布后 Task -> ledger -> Action -> Attempt -> typed remote fact 的 E4 证据确认恢复。

## Current Phase

Phase 4

## Phases

### Phase 1: 生产诊断与产品设计
- [x] 刷新生产 Task/ledger/Action/Attempt/remote fact 证据
- [x] 定位首个业务断点和代码路径
- [x] 补齐 PRD、专项设计、数据流转、QA 与发布/回滚口径
- [x] 完成 Product Design Complete 自检
- **Status:** complete

### Phase 2: 回归测试与实现
- [x] 为 fact_first_v3 当前准入/open obligations 写失败回归
- [x] 修复 AI 到期量的开放义务核算，阻止重复物化
- [x] 移除已到期 AI/浏览义务的二次任务级摊速并守住 deadline
- [x] 更新项目结构索引和必要运维入口
- **Status:** complete

### Phase 3: QA 与产品验收
- [x] 运行聚焦单测、相关 no_postgres 测试和静态检查
- [x] 验证 all/group/manual、幂等、未知远端结果及账号级安全边界
- [x] 完成本地 QA Gate 与 Product Acceptance；PostgreSQL/完整套件交给 CI Release Gate
- **Status:** complete

### Phase 4: 发布与运行时验证
- [ ] 推进 master -> release -> Deploy Production
- [ ] 校验 immutable SHA、容器/worker、应用与公网健康
- **Status:** pending

### Phase 5: 生产积压安全恢复与 E4
- [ ] 只读 preview 精确识别无 Attempt/Gateway 的错误未来/超额 Action
- [ ] 以哈希/版本/数量守卫执行审计化 apply 和独立 readback
- [ ] 触发正常 replan，不重试 unknown remote outcome
- [ ] 连续观察 AI 活群与浏览的发布后 typed remote facts
- **Status:** pending

## Success Criteria

1. 同一 ledger 的 confirmed + valid open obligations 不超过自然到期量；重复 Planner 轮转不再增长。
2. fact_first_v3 使用 TaskGroupBotAdmission/数量槽位等当前合同事实，不依赖旧 GroupBotAdmission 才能计算开放量。
3. 已到期义务不再被任务级 minimum spacing 二次推迟，且任何 Action 不越过 ledger.deadline；账号级 FloodWait、冷却、session/代理边界不变。
4. 浏览任务按自然到期缺口和真实账号容量物化；目标超过可用账号时明确暴露容量缺口，不伪造完成。
5. 历史积压处置保留 success、claiming/executing、Gateway-started、unknown；只终结精确匹配的 pre-Gateway 错误义务，并完整释放关联预留。
6. 发布后生产 E4 出现新的 AI remote message facts 与浏览 ViewRemoteFact，积压/到期差距按合同收敛。

## Decisions Made

| Decision | Rationale |
|---|---|
| 从本地 origin/master 跟踪点建立独立 worktree | 主 checkout 有大量用户未提交修改，必须保持不动 |
| 先修核算与调度合同，再处理历史积压 | 否则 cleanup 后 Planner 会再次制造相同错误 |
| 自然 due curve 是唯一任务级节奏闸门 | 已到期义务应由真实账号/资源容量与安全约束执行，不再二次按模板摊速 |
| 生产修复只接受 typed remote fact | CI、部署、health、Action success 均不能替代 Telegram 远端事实 |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `git fetch origin master release --prune` 在 GitHub TLS 握手时报 `SSL_ERROR_SYSCALL` | 1 | 记录为访问路径故障；先基于 2026-08-11 02:52 已知 origin/master 跟踪点设计/开发，推送前必须重新 fetch 并校验祖先关系 |
| macOS 缺少 GNU `timeout`，首次红测命令未启动 | 1 | 改用 Python `subprocess.run(..., timeout=60)` 包裹主 checkout 的 `backend/.venv/bin/pytest`，不重复原命令 |
| 完整 `-m no_postgres` 在 60 秒硬门禁内只运行到 45% | 1 | 保留 hard timeout；聚焦 276 项通过，完整套件和 PostgreSQL 分区由 Deploy Production CI Gate 执行 |

## Notes

- 用户已授权完整修复、发布和线上验证，但生产数据 apply 仍必须先 preview、精确目标、漂移守卫、审计和 readback。
- 浏览 1000/消息高于当前约 797 个可用账号属于容量缺口，代码不得静默降低目标或伪造成功。
