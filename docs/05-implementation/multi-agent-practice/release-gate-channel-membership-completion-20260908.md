# 六频道关注补齐阻塞修复 Release Gate

- Intake：用户要求把六频道账号关注补齐完成；L3，发布负责人/验收负责人为当前Codex任务。
- 范围：tenant1频道6/19/2765/2806/5911/5981；全量账号资格合同和原冻结排程不变。
- 根因：历史成员unknown被当永久物理在途，补偿复检覆盖原Attempt终止证据。生产只读候选复算24750条旧判定占用，24683条具备返回证明，67条继续保持阻塞，不能把理论释放当真实关注。
- 产品合同：channel-membership-precondition-design.md §14.8，design_status=complete。
- 实现：原Gateway结果身份/归属/双hash校验，明确ACK或原同步返回回执解除物理占用；同账号同目标unknown及无证明行继续阻塞。复检合并保留原Attempt snapshot。无数据库迁移、无前端/API变化。
- 本地QA：95项关注排程/并发/返回证据/未知复检及完成审计回归通过，10.48秒，每轮60秒硬超时。
- PostgreSQL QA：独立仅Unix socket的tg_yunying_test实例，pytest advisory lock保护，3项真实启动/生命周期/行锁回归通过（5.43秒）；实例已停止。
- 审查：缺失目标、epoch/账号/请求/hash/观察时间错误、显式未确认取消、同目标unknown、冷却和未结束并发均保持原阻塞；原记录不改写。独立工作树保留主工作区并发提交。
- 发布：待完成master→release→Deploy Production及独立SHA读回；不通过热补丁绕过流水线。
- 生产验收：每频道有效候选数、真实成员缺口、到期/未来Action、Attempt和成员远端事实；权限异常分列。关注全量补齐前production_status=unproven。
- 回滚：无迁移，本补丁可用逆向提交恢复原判定；已经产生的真实关注和历史unknown证据不得回滚或伪造。
