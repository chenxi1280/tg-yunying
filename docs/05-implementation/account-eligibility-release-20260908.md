# 账号资格与履约证据修复 Release Gate（2026-09-08）

- intake_id: account-eligibility-release-20260908；level=L3；release_mode=github_actions。
- 用户授权：按流程提交当前已审查的未提交代码并部署线上。
- release_owner / rollback_owner / merge_owner：本次Codex；当前checkout为主仓master，无其他worktree占用release。
- 基线：本地master=origin/master=`069db23a4ff55393be8c1159dce419656e7a5acf`；origin/release及生产代码=`d26a562bfadc09a3c2af960b9c0c438111aaad8d`。前者仅比已部署代码多冻结验收文档；无分叉。
- 候选身份：包含本Gate文件的不可变提交SHA；推送前要求所有发布路径均与冻结清单/hash一致。Git提交和Actions的headSha作为精确候选来源，避免在文件内自引用SHA。

## 发布范围与产品接受

1. 统一引擎账号资格：失效/冻结/无效当前授权在计划、新分配、生成、Provider和Gateway入口排除；保留旧计划及远端unknown。分组/用途与健康状态分离，待重试动作的本轮Attempt身份准确区分。
2. 已知Session失效、需重新登录、冻结账号的自动复查每24小时；封禁/禁用保持退出保活，正常保活与人工恢复入口不变。
3. 运行明细清理保护72小时成功统计依赖、业务引用、活跃owner、待投影及unknown；清理前锁内复核。纯内容配置变更和同值保存保留任务生命周期与既有排期。
4. 当前已审查PRD、专项设计和索引同步；统一§19.60/§19.61中尚未实现的新应急/完整异步能力不宣称随本次交付。

详细合同及分批QA见`legacy-prd-p1-repair-20260908.md`、`account-assignment-eligibility-20260908.md`及统一§19.64。Code review中两处回归已修复并复验；本地产品接受限定上述实际实现范围。

## 本地验证

- 前序清理/内容生命周期批次220项、资格批次423项、每日复查批次131项记录见各自开发文档；批次有重叠，不累加成唯一测试数。
- 审查修复：18项正式反例，修复前13失败/5通过；与相关9个文件回归合计176 passed / 25.34s。
- 发布前真实PostgreSQL复验：资格并发、引用保留/并发、原retention、退役门禁共16 passed / 22.49s；独立本机tg_yunying_test，advisory lock；从空库迁移到0228通过。
- 发布前Provider HTTP、内容更新生命周期、业务引用保留、最近成功统计复验84 passed / 23.26s。
- 新增或改动62个Python文件编译通过；git diff --check通过。本次没有前端、部署脚本或迁移代码变更；完整前后端/迁移CI仍由候选Deploy Production执行。
- 所有本地后端pytest使用backend/.venv及每轮60秒进程组硬超时。

## 线上发布前只读检查

- current=`/data/tgyunying/releases/20260908034913_d26a562b`；backend及本项目全部业务worker健康；Alembic=`0228_account_freeze`。
- 授权runtime=off；ABC批次无running；已有一个stopped批次保留，不恢复或改写。
- 当前运行四类统一Task共23：活群10、评论2、点赞6、浏览5。后验必须按实际Task身份和新部署锚点读回，不能套用旧22任务数字。
- 当前账号冻结322；这个数字是发布前快照，不是发布后最终事实或本次覆盖证明。

## 发布与验证计划

- 路径：提交master → 非强制推送master → fast-forward更新release → workflow_dispatch Deploy Production（全部可选生产修改/重试参数保持false）。
- CI必须终态成功且headSha为候选；部署后独立核对current、backend和worker RELEASE_SHA、Alembic、共享调度active合同、local/public health。
- 业务只读取证：候选SHA及精确时间窗口下，失效账号新增Action/GenerationJob/Attempt/Gateway，健康账号typed远端事实；异常账号last_probe_at/next_probe_at的24小时调度；新旧Attempt处理报错是否出现；存量unknown保持。配置保存和物理清理不以生产试写验证。
- 无新增Schema；保留既有0228冻结事实和0226退役约束，不执行数据库downgrade。不自动退回会重新派发失效账号或删除业务证据的旧应用；若发现回归，依当前证据走前向修复并重跑闸门。
- 本文件记录提交时的Release Gate：local_gate=passed；ci/deployment/business=pending。最终结论以本候选Actions终态、线上版本独立读回及发布后业务证据为准，禁止仅由本地QA写production_fixed。

## 首次CI失败后的修复与复验

- 首次候选`fe15fd731b9a8e97c36d9cb523b3fe3db0085e13`，Actions run `34193137652`：前端/候选校验通过，后端共27项失败，镜像构建及部署均skipped，生产未切换。
- 实际回归：只读切换容量预览误用账号FOR UPDATE；按统一§19.64.9复用资格谓词但显式不加锁、不发布摘要，真实分配仍默认锁。其余失败来自旧fixture缺Session/错误离线枚举、旧四查询预算、模拟当前Attempt却在派发前创建、纯异常转发测试未替换新增DB读。
- 固定查询预算覆盖资格锁与读取，1/32/128候选不出现N+1；真实PG并发竞争返回account_eligibility_busy，原事务提交后重试看到已占用容量且不超分配。没有跳过失败测试或放宽生产资格。
- 修复复验：原失败相关7文件69 passed / 13.48s；共用fixture及资格/分配消费者14文件162 passed / 23.82s；历史调用/恢复8文件115 passed / 20.85s；真实PG只读切换、组合容量与资格并发9项通过。普通测试与PG分开执行，避免迁移fileConfig影响caplog的进程内日志配置；混跑时12项仅日志断言失败，隔离复跑115项全通过。
- 2个生产模块为最小修复，PRD与两项索引同步；Python AST和diff-check通过。随后将补充提交快进master/release并重新运行完整候选CI；最终上线证据仍待新流水线终态。

## 第二次发布线上反查与共享资格锁修复

- 候选`49214fdca46a9e8d63608945719e593bd1940a54`的Actions run `34194192661`于2026-09-08 14:35:17北京时间成功。CI合计7037 passed、14 skipped、2 xfailed；生产current、backend及18个业务worker SHA一致，服务/内外网health通过；共享合同active，Alembic0228，授权runtime off、ABC无running。
- 业务验收未通过：23个实际Task逐项读回无完整新远端成功链；多个新Planner错误为account_eligibility_busy，另有一次PostgreSQL deadlock。前述服务健康、异常账号零调用不能替代业务通过。
- 根因：资格读取对候选TgAccount使用FOR UPDATE，与正常读取者及Action/Attempt账号FK KEY SHARE冲突。统一§19.64.10修订为FOR SHARE NOWAIT；current授权/online事实仍使用共享锁，冻结UPDATE仍互斥，预算仍由原policy锁防超分配。生产修复仅此一处锁模式变更，无数据清理或unknown重试。
- 新增2项真实PG反例：修复前2 failed / 6 passed（12.01s），修复后连同资格失效先后顺序、观测写锁、预算policy竞争及冻结专项12 passed（19.51s）。普通资格/审查回归/组合容量/批量读取/共享使用/每日检查/冻结合同/membership等10文件161 passed（22.07s）。AST与diff-check通过。
- 需将本修复重新提交、快进release并通过完整CI，使用新部署锚点验证正常读取不再持续互斥、失效账号仍排除及健康账号真实执行。完整24小时复查周期及未实现的§19.60/61仍不得声称已验收。
