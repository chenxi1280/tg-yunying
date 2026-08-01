# 共享调度与 AI 履约修复 PRD 计划

## Goal
基于 2026-08-01 生产事故事实和已完成专项 PRD，完成共享调度、Gateway原子终结、AI存量接管与远端核验的代码修复、自动化 QA和产品复核；生产发布和E4仍需独立证据。

## Current Phase
Phase 20: post-deploy Scope identity 刷新修复

## Requirements Checklist
- [x] Intake Card 与 L3 分级、证据边界
- [x] 公共 Dispatcher 唯一分片拓扑与容量合同
- [x] 跨 epoch Reservation 守恒与重建规则
- [x] 统一锁序和事务边界
- [x] AI 历史 scope contract 安全接管
- [x] 郑州大学、郑州师范、郑州楼凤专项恢复规则
- [x] 搜索点击 assignment、验证码与安全容量边界
- [x] 评论、点赞、浏览的非故障边界
- [x] 数据模型、API、worker、配置和前端状态
- [x] preview/apply、幂等、审计、回滚
- [x] QA、Release Gate、生产 E4 验收
- [x] Product Design Complete 自检与 Product Handoff

## Phases

### Phase 1: Intake 与真相源核对
- [x] 核对主 PRD、专项 PRD、数据流和生产运行文档
- [x] 把生产根因转成产品/系统约束
- **Status:** completed

### Phase 2: 专项 PRD 编写
- [x] 新建专项 PRD
- [x] 覆盖目标、非目标、流程、状态、数据、边界和验收
- **Status:** completed

### Phase 3: 真相源同步
- [x] 更新 feature design README
- [x] 更新主 PRD 的专项引用/事故 resync
- [x] 更新数据流索引
- **Status:** completed

### Phase 4: 设计审查与交付
- [x] Product Design Complete 自检
- [x] 文档链接与内容一致性检查
- [x] 输出 Product Handoff
- **Status:** completed

### Phase 5: 逆向设计审查
- [x] 核对搜索首次 outcome 所有权与跨 epoch 回收是否冲突
- [x] 核对 Gateway 前后事务边界是否留下重复发送或丢结果窗口
- [x] 核对 Stage A/Stage B 迁移期间的并发、幂等与断点续跑
- [x] 核对混合任务公平性、严格任务饥饿和容量可行性
- [x] 核对拓扑指纹、动态配置与故障恢复语义
- [x] 对照代码和父级真相源输出分级问题清单
- **Status:** completed

### Phase 6: 审查问题修订
- [x] 修正搜索首次 outcome 所有权与普通 Reservation release 合同
- [x] 修正 Stage A/B fence 顺序并定义迁移期 claim gate
- [x] 增加持久 takeover batch/item 与断点续跑合同
- [x] 冻结 post-Gateway 原子事务和 remote reconcile 状态机
- [x] 冻结计数字段、topology fingerprint 与 shard liveness 合同
- [x] 补齐并发、崩溃、混合公平和远端核验 QA
- [x] 同步父级 PRD与数据流索引并执行一致性检查
- **Status:** completed

### Phase 7: 实现基线与红测映射
- [x] 核对分支、dirty worktree、现有迁移和受影响代码入口
- [x] 把 PRD §5-§9 映射为代码/迁移/测试清单
- [x] 运行现有定向基线测试并记录已实现/缺失/冲突
- **Status:** completed

### Phase 8: 数据模型、配置与合同基础
- [x] 增加 scope/window/shard state、takeover batch/item、remote case模型和幂等迁移
- [x] 增加 canonical fingerprint、activation state与配置校验
- [x] 写迁移、fingerprint和状态机红测并通过
- **Status:** completed

### Phase 9: 调度、容量与搜索守恒
- [x] 实现唯一runtime topology、live shard预算和stale/recovery
- [x] 实现 ordinary current/stale/no-longer-due三分支
- [x] 保护search首次outcome并限制exclusion carrier
- [x] 通过跨epoch和混合高债务公平定向测试
- [ ] 通过PostgreSQL并发测试（测试数据库不可连接）
- **Status:** local_complete_postgresql_blocked

### Phase 10: Gateway B0/B1与远端核验
- [x] 冻结Gateway前B0并原子化Gateway后B1
- [x] 实现remote case及confirmed/absence-proven/inconclusive CAS
- [x] 通过SQLite故障注入、unknown防重和账本一致性定向测试
- [ ] 执行PostgreSQL独立journal/B1 rollback测试（测试数据库不可连接）
- **Status:** local_complete_postgresql_blocked

### Phase 11: AI存量接管与发布fence
- [x] 实现takeover batch/item/cursor、crash resume和claim gate
- [x] 实现preparing/active激活合同及Stage A/B/C脚本顺序
- [x] 通过drift、batch chain、遗留claim和发布顺序定向测试
- **Status:** completed_local

### Phase 12: 索引、运行文档与全量QA
- [x] 同步项目结构索引、数据流索引和PRODUCTION_RUNTIME
- [x] 运行定向no_postgres、迁移结构、静态、frontend build和格式检查
- [ ] 运行PostgreSQL分区（测试数据库不可连接）
- [x] 对未发现问题做反向审查；新设计问题先回写PRD再修复
- **Status:** local_complete_postgresql_blocked

### Phase 13: Product验收与交付
- [x] 核对PRD每项本地验收证据和未证明边界
- [x] 更新implementation/qa/product状态且不伪造生产E4
- [x] 输出变更、测试、风险与生产状态
- **Status:** completed_local_handoff

### Phase 14: 完成性反向审计
- [x] 核对preparing期间全部业务writer是否真正零写
- [x] 核对B0/journal/B1/recovery所有崩溃点是否可达终态
- [x] 核对remote confirmed/absence/inconclusive对五类业务义务的收口
- [x] 核对跨epoch/search公平、shard恢复和activation chain边界
- [x] 核对迁移、生产配置、发布脚本和测试盲区
- [x] 输出仍会导致任务不完成的问题及证据，不在本轮擅自修复
- **Status:** completed_findings_block_release

### Phase 15: 审计缺陷修补
- [x] 先补专项PRD、父级口径与DF-324的heartbeat retirement、metadata merge和membership唯一核验协议
- [x] 为两个群管Gateway动作补B0/journal/B1红测并实现
- [x] 为worker优雅退役、合同metadata保持和正常发布生命周期补红测并实现
- [x] 将membership权威reprobe纳入唯一RemoteReconcileCase CAS闭环
- [x] 生产环境fail closed拒绝embedded worker并补发布配置测试
- [x] 运行定向回归、静态检查和完成性复审
- [ ] 运行PostgreSQL并发/独立journal分区（测试数据库不可连接）
- [ ] 执行Actions、生产发布、runtime验证和Telegram E4（本轮未授权/未执行）
- **Status:** local_complete_postgresql_and_release_blocked

### Phase 16: 生产发布与 E4 恢复验收
- [x] 核准本次共享调度修复的精确发布范围，排除并行计划的无关改动
- [x] 在 GitHub Actions PostgreSQL/全量测试通过后完成 production deploy
- [x] 核验线上 migration、runtime contract、worker heartbeat、claim/reconcile 运行态
- [ ] 观察目标任务的真实 Telegram 业务事实与任务账本，未完成则继续定位和修复
- [ ] 只有部署、runtime 与业务 E4 均通过后，才更新 PRD 为 production_fixed
- **Status:** runtime_pass_business_blocked_by_expired_search_release

### Phase 17: 过期搜索 Window 释放阻断修复与再发布
- [x] 用生产日志与账本定位 `dispatch_release_window_unclaimed_negative`
- [x] 先回写专项 PRD、主 PRD 与 DF-324 的过期 Window 计数语义
- [x] 补低层和 assignment 集成红测，修复 effective 二次扣减
- [x] 通过搜索释放、跨 epoch、Dispatcher claim 与 Gateway 原子性定向回归
- [x] 提交并推送 master/release，等待完整 Actions 和生产部署
- [x] 证明 Dispatcher 不再因过期搜索释放整轮退出
- [ ] 发布后产生真实 Attempt/远端事实（被 Phase 18 的 shard 心跳时区问题继续阻断）
- **Status:** deployed_release_blocker_fixed_next_blocker_found

### Phase 18: shard 心跳时区修复与再发布
- [x] 用生产数据库同进程重算定位 live shard 可用量被误算为零
- [x] 先回写专项 PRD、主 PRD 与 DF-324 的平台时钟语义
- [x] 补生产同形态红测，把无时区北京时间与 aware heartbeat 统一后修复
- [x] 通过 runtime、activation、claim/release、跨 epoch 和搜索释放定向回归
- [x] 提交并推送 master/release，等待完整 Actions 和生产部署
- [x] 证明 live shard 预算恢复并产生真实 active claim
- [ ] 证明 Attempt增长并产生真实远端事实（被 Phase 19 的 post-deploy 校验误报继续阻断）
- **Status:** deployed_liveness_fixed_next_blocker_found

### Phase 19: post-deploy 跨窗 active claim 验证修复
- [x] 从 run `30696937584` 定位 release内部verify通过、20秒后post-deploy verify报`closed_window_active`
- [x] 区分preparing/fence零active与active合同合法跨窗在途并回写PRD/DF-324
- [x] 补跨窗合法与错绑失败红测，锁Scope后按Action binding验证运行期投影
- [x] 通过定向回归
- [x] 提交并再次走master/release完整发布
- [ ] 在线证明post-deploy verify通过、Attempt与真实Telegram事实增长
- **Status:** deployed_cross_window_fixed_next_blocker_found

### Phase 20: post-deploy Scope identity 刷新修复
- [x] 从 run `30697835240` 定位发布/接管通过、外层校验报 `scope_active_projection`
- [x] 核对同 Session candidate 旧缓存与 Scope 行锁读取未刷新形成混合快照
- [x] 先回写专项 PRD、主 PRD、DF-324 与结构索引
- [x] 红测复现数据库已更新但 identity map 仍旧的 Scope 加锁读取
- [x] 最小修复加锁查询刷新语义并通过定向回归
- [ ] 提交并再次走 master/release 完整发布
- [ ] 在线证明两次 verify、Attempt 与真实 Telegram 事实增长
- **Status:** in_progress_local_verified

## Decisions Made
| Decision | Rationale |
| --- | --- |
| 新建聚焦的 L3 专项 PRD | 现有全任务 PRD 过长，本事故需要可独立交接和验收的修复边界 |
| 不改变既有 fail-closed 安全口径 | 问题在实现偏离、兼容迁移和容量守恒，不需要放宽 Telegram/验证码/准入规则 |
| 先完成设计闸门再进入实现 | 用户随后明确授权按已完成PRD修补，代码变更均须回链专项合同和测试 |
| heartbeat退役使用显式status而非删除历史行 | 保留审计事实并让old-writer检查只读取active writer |
| heartbeat metadata使用合并更新 | 业务drain不得删除worker loop冻结的合同版本 |
| membership只保留RemoteReconcileCase作为unknown终结真相源 | 禁止旧reprobe绕过expected hash与evidence CAS |
| 保留现有全局active plan，不用本专项覆盖 | 工作区还有并行发布计划；本次用显式路径维护共享调度发布证据，避免篡改其他任务状态 |
| 已结束 Window 的 release 不再扣 effective | effective 已退出当前容量预算；仍扣历史 unclaimed并保留唯一 carrier，避免过期搜索 unit 阻塞整个 Dispatcher drain |
| naive runtime clock按北京时间绑定 | 项目 `_now()` 返回无时区北京时间墙钟；直接绑定UTC会把真实心跳误判为8小时前，不能靠扩大stale窗口掩盖 |
| active运行期允许真实跨窗在途 | Window是claim批次边界而非Gateway deadline；仅fence激活要求closed active归零，运行期必须核对Action binding而非无条件拒绝 |

## Errors Encountered
| Error | Attempt | Resolution |
| --- | --- | --- |
| matching project skills are placeholder templates | 1 | use AGENTS.md and repository truth-source documents |
| proposed `DF-200` collides with an existing API flow ID | 2 | select the next unused dataflow ID after inspecting the full index |
| PostgreSQL test database closes connection before collection | 2 | keep PostgreSQL concurrency/journal tests blocked and report separately; do not count as pass |
| membership case红测缺TelegramDeveloperApp fixture | 1 | 在定向单元测试注入credentials resolver；生产代码不增加fallback |
| membership case测试用refresh覆盖未提交B1状态 | 1 | 改为flush后断言；真实Recovery由外层统一commit |
| Actions run 30690191293 no_postgres分区18条失败 | 1 | 部署未执行；按active contract夹具、stale recovery签名、0134 head与终结语义分组复现并修复，不放宽生产fence |
