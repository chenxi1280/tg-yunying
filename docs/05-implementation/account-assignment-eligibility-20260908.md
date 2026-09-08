# 失效账号不分配任务：开发批次

## Intake / Product Handoff

- 用户授权：按统一引擎§19.64修复Session失效、冻结等账号仍被分配的问题；L3。
- 当前工作区前序PRD及P1清理/内容生命周期修复均保留，不混淆实现归属。
- PDC：共同资格事实、过滤先于人数/覆盖、已有计划续派、前置任务、Provider/Telegram边界、历史保留、恢复和真实PG竞态均按§19.64。
- 本地反例：20个四类型/五种失效组合与4个旧计划续派用例；初始均失败（其中活群fixture缺目标已修正），过滤修复后24项通过。

## Bug Batch Plan

1. 当前账号资格共用查询与锁定：配置成员 → 有效候选 → 新参与计划；当前授权优先，legacy无current指针沿正式account RuntimeTransport身份，不把两种Session做OR。
2. 已有selected的后续来源、coverage、membership、物化及生成候选重新过滤；通用Action创建前和Gateway调用前复核。
3. Provider晚到结果保留证据并拒绝给失效账号发布ready；Telegram unknown不改写、不换号重放。
4. 定向回归及真实PG观测/分配/执行竞争验证；每条后端测试60秒硬超时，backend/.venv，独立tg_yunying_test与advisory lock。

## 当前状态

- design_status=complete；dev=complete；qa=qa_pass（本地）；release=not_released；production_status=unproven。
- 反查resync：旧临时故障用例使用session_ciphertext=None；按新需求它属于失效，已改用代理故障检验临时等待仍不重抽历史计划，并另保留Session缺失排除用例。


## 实现与代码审查

- 公共入口：`account_assignment_eligibility.py`负责当前身份SQL、锁及准确原因；`account_assignment_snapshot.py`将授权/连接/事实代次与观测/原因/hash写入原PlanningAdmissionSnapshot，不新增Schema。
- 规划：账号先过滤再算参与者；成员归属快照与当前资格分离。已有selected保留历史；coverage与retry在LIMIT之前过滤，部分无效账号不挤占健康候选。新portfolio和浏览/点赞来源使用当前资格，原业务数量与旧远端事实不被下调或撤销。
- 物化/调用：通用Action工厂、讨论组/救援/epoch恢复直接构造路径、生成claim、Provider HTTP、Attempt创建与Gateway边界复核；Provider要求Action.payload.generation_job_id精确对应原Job，不能借同义务的新Action授权旧生成请求。
- 结果：群消息/评论晚到内容保留candidate hash与Token/质量证据，失效账号不写ready；不改写Provider或Telegram unknown，不生成假成功。
- 可用性：零有效账号显示no_eligible_accounts且不冻结0人完成计划；合法current授权不依赖旧Session副本，旧standby不能覆盖无效current。账号、current授权及在线记录均使用真实PG锁复验。
- 本次触达超过50行的函数已拆出对应职责helper；新模块均低于500行，新/增长函数满足50行限制。保留其余既有超大文件的边界，未为该修复改写无关模块。
- `RuntimeResourceBlocked`作为Exception必须允许Python写入traceback；旧frozen dataclass在事务上下文传播时会将原typed错误覆盖成FrozenInstanceError，已修复该真实错误路径。

## QA 证据

24个测试文件共423个唯一用例已按批次通过；以下运行有重叠，不能直接相加：

| 批次 | 结果 | 主要证据 |
| --- | --- | --- |
| 参与/分配、历史计划、成员、portfolio、冻结、恢复和候选查询 | 199 passed / 27.55s | 后续新增2项另由末轮复验覆盖 |
| 生成时序、真实本地HTTP、Phase C、覆盖及派发 | 145 passed / 37.03s | Provider额外身份用例另由末轮复验覆盖 |
| Provider PostgreSQL + 资格 PostgreSQL | 14 passed / 12.20s | 原5项资格锁加9项Provider持久事实；新增授权锁由下行复验 |
| 失效资格42项 + 讨论组/救援/恢复 | 103 passed / 18.63s | 直接Action创建、两类晚到结果、retry前置过滤 |
| membership策略/候选 + 最新资格测试 | 104 passed / 15.38s | helper拆分后复验，0有效前置状态与健康候选不饥饿 |
| Provider真实HTTP + PostgreSQL + 资格锁 | 42 passed / 29.47s | 失效后零HTTP，同义务新Action不能替代原Job |
| 资格真实PG最终复验 | 6 passed / 9.12s | stale ORM、冻结/Session先提交、账号/授权/在线锁竞争、分配先提交 |

- 使用`backend/.venv`，每次pytest进程组硬超时60秒；PostgreSQL仅独立本地`tg_yunying_test`，有advisory lock。资格竞态使用独立schema及真实FK/事务；Provider套件执行到现有0228迁移head。
- 423为测试清单去重结果（collect-only），运行结果由上述分批QA覆盖；并非将重复运行次数计入用例数。
- Python编译与`git diff --check`通过；未调用生产Provider或Telegram，也未修改生产账号/任务/状态。

## Release Gate / 产品接受

本地符合§19.64的账号排除、计划保留、精确生成归属及并发验收，可进入后续发布审核；当前工作区尚未提交或发布，本批次不能写production_fixed。既有PRD/P1代码改动均保留，不把其发布状态与本批次混合。

生产验证仍须独立确认master/release、CI、部署SHA与运行版本，再从实际Task→有效候选/新plan→Action/GenerationJob/Attempt链验证：失效观测之后该账号新增工作为0、健康账号仍产生合法工作、历史unknown/confirmed未改写。历史数据修复/retry/reconcile不在本批次执行记录内。§19.60/§19.61尚未实现的其他设计不计入本批次完成声明。


## 追加需求：异常账号每天复查一次（2026-09-08）

- Intake：用户要求Session失效、需重新登录、冻结、封禁、禁用每天检查一次；沿既有L3合同补充§19.64.7，再进入开发。
- 设计反查：旧login_required 30分钟，冻结/失败5分钟；批次调度覆盖单条next_probe_at。已封禁/禁用已有来源排除，保持停用；资格入口仅查询持久事实，无Telegram RPC。
- 实现：异常自动复查24小时，旧短deadline在SQL LIMIT前受last_probe_at限制；异常账号网络/凭据再次失败仍24小时；冻结被其他流程投影为在线也不绕过。封禁/禁用残留desired_online不能触发RPC。circuit账号/route/egress入口排除独立冻结事实。正常5/15分钟保活及网络恢复不变；授权切换原流程清除旧probe时间，仍可立即验证。
- 代码审查：保留旧失败和冻结CAS/身份代次处理；不改变人工健康检查、登录、账号停用意图，不改业务unknown，不新增Schema。circuit无可用账号仍沿原superseded/probe_dependency_changed结算，测试验证无健康RPC及明确原始失败证据，不修改无关circuit重排规则。
- QA：7个文件131项通过，17.36秒；包含20项每日复查、7项circuit、既有online/冻结及42项即时分配资格回归。既有AuthKeyDuplicated用例出现一次SQLAlchemy空主键查询warning，未隐藏或扩大修改范围。7个本次Python文件编译、函数/文件度量及diff-check通过。
- Release Gate：design_status=complete，dev=complete，qa=qa_pass（本地），release=not_released，production_status=unproven。未修改线上排期或调用真实Telegram；上述频率需发布后按运行SHA和last_probe_at/next_probe_at及真实probe记录单独验证。


## 未提交更改审查问题修复（2026-09-08）

- Intake：用户“你来修复问题”，修复本次审查的分组/用途过滤丢失及历史Attempt误判；沿既有L3任务，先补统一§19.64.8。
- Bug Batch Plan：R1将运营范围与健康过滤分离，同步直接/持久scope入口；R2用本轮派发前Attempt身份区分新旧，保持远端历史。当前PDC已闭合，进入定向反例与dev。
- 基线：工作区所有前序代码/文档保留；审查临时3项反例失败，相关96项旧回归通过。开发需将反例提升为正式回归，不能凭旧QA宣布修复。

- 实现：提取`apply_operational_account_scope_filters`保留普通用途与启用组合同，原`apply_operational_account_filters`继续额外排除冻结。配置候选、统一持久membership候选及覆盖范围（含外部传入候选）复用范围条件；legacy持久scope沿原流程。`_dispatch_action`在进入执行分支前捕获已有Attempt ID，资源等待处理与当前latest ID比较，避免使用历史尝试结算本轮等待。
- 正式反例：新增`test_account_assignment_review_regressions.py`共18项，修复前13失败/5通过，修复后全部通过；八项分组/用途与四种scope入口、两项健康失效统计、六项历史Attempt/首次尝试、两项当前Attempt未调用/已调用边界。
- 复审：静态范围过滤不吞掉冻结/Session失效统计，不重复注入legacy范围行为；本轮之前的failed/success/unknown/before_call/skipped历史均不改写。当前新Attempt仍按未调用资源结算，当前已调用继续显式拒绝未调用结算。每次派发新增一次现有按action_id/attempt_no查询以捕获前置身份，不增加远端请求、Schema或重放。
- QA：9个文件176项通过（25.34秒）：审查回归、account_usage_policy、task_account_scope_sync、channel_membership_candidates、engagement_membership_foundation、engagement_assignment_eligibility、account_online_daily_recheck、gateway_evidence_journal、task_retirement。backend/.venv，每轮60秒硬超时；6个本次Python文件编译及改动函数50行度量通过，git diff --check通过。测试仅本地，未修改生产。
- 本轮状态：design_status=complete；dev=complete；code_review=passed；qa=qa_pass（本地）；release=not_released；production_status=unproven。所有前序未提交改动保留，不借用过去发布/E4声明本轮已上线。
