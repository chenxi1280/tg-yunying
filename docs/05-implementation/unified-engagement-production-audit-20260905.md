# 统一互动引擎生产审计与修补（2026-09-05）

- intake_id: intake-20260905-engagement-production-audit
- level: L3
- source: 用户要求按统一互动 PRD 检查线上并修补
- route: prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis
- baseline: clean master f869375f9a751d06d84c5ffa755337b813c9a706
- production: /data/tgyunying/releases/20260905010257_f869375f
- migration: 0223_burst_negative_outcome
- observation: 2026-09-05 09:40–09:44 Asia/Shanghai，SSH、数据库只读事务
- evidence: 本机 /tmp/tgyunying-engine-audit-20260905/ 的脱敏计数与边界快照，不含凭据/消息正文
- production_status: unproven

## Root Cause Grouping

| 编号 | 观察事实 | 判断及修补范围 |
|---|---|---|
| R1 | 23个运行中任务的 unified 配置为空、binding表0行；21个all，2个单组 | 程序已上线，存量任务未接管。须按PRD §17核对等价账户范围、重叠目标、在途身份，不能直接批量开关 |
| R2 | 发布后浏览有66个result_unknown；异常为目标username解析不存在，发生在view RPC之前 | Gateway混淆解析错误与mutation unknown。修补实际RPC边界；不追溯改写旧unknown |
| R3 | 评论Action af5465f7-913b-4562-a22f-c1c8d039949b executing/generating、lease 08:18到期，ExecutionAttempt=0 | Job被误判action_missing后cancelled，Action的Provider started已持久化。修正身份解析；历史行仅可精确纠正为unknown，禁止重调Provider |
| R4 | 浏览发布后约4000个pacing_source_not_before尝试；郑州精品大量目标无效 | 排期重领与目标不可解析分别诊断；不能把跳过算成功，不能凭名称猜新目标 |

## 当前业务证据

09:40快照存在活群、评论、浏览的发布后类型化成功事实；点赞快照暂未观察到发布后reaction fact。活群的membership_observed不计发送；浏览view_observed不等于Telegram计数器增长。全天目标尚未到截止点，不能以当前缺量直接判日目标失败。以上均为legacy Task的事实，统一引擎E4尚无样本。

## Product Handoff / Release Gate

R2以PRD §19.17为开发合同；变更限定Gateway浏览边界和针对性回归，无schema/前端修改，不恢复或重试旧任务，不改变目标/数量。真实RPC进入前失败须带false；RPC未知须保留None；成功仍一次RPC。

- local_gate: passed；三组61项no_postgres定向回归（10+32+19），语法与diff check通过；本机5432无服务，PostgreSQL验证由候选CI独立执行
- code_review: passed；浏览实际RPC边界保留unknown，评论精确身份不放宽活群/跨作用域匹配；恢复模块拆出身份职责后488行
- release_path: master -> release -> Deploy Production
- migration_impact: none
- rollback: 本切片无新数据结构，旧版本会恢复错误分类；不通过rollback消除unknown
- production_probe: 发布SHA与worker，随后自然view调用的Attempt、mutation标记及typed fact；旧unknown保持不重放
- release_status: pending
