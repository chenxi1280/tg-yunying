# Telegram 冻结账号执行资格修复

## Intake / 产品交接

- intake_id: frozen-account-eligibility-20260908；级别 L3；production_related=true。
- 用户要求：修复已冻结账号仍被分配并执行任务的问题。
- 只读生产证据：2026-09-08 09:57，89d837b8 版本；账号1263此前已有冻结错误，今天05:32/09:56再次进入Gateway收到明确冻结拒绝，账号仍在线；最近24小时205个membership Attempt返回冻结错误。
- 根因：membership 群权限分支早于冻结分类且提前返回；unknown收尾没有记录独立账号冻结事实；健康检查只检查授权/get_me，成功直接覆盖在线投影。
- 本次 Product Design Complete：下列合同覆盖状态、并发、迁移、恢复和QA；先完成文档，再进入开发。发布仍需Release Gate，不能凭测试声明production_fixed。

## 合同

1. Telegram `FROZEN_METHOD_INVALID`、`FROZEN_PARTICIPANT_MISSING`及对应SDK类/明确英文错误统一识别为账号冻结，优先于群权限、FloodWait和内容错误；普通群禁言/权限错误不冻结账号。
2. `TgAccount.telegram_frozen`和`telegram_freeze_observed_at`保存独立账号事实。冻结后status投影为疑似封禁，健康分不超过20；操作任务候选和执行前均检查冻结事实，其他流程将status写回在线也不能放行。历史分母、成员归属、任务配置与unknown证据不删除或改写。
3. 健康检查授权/get_me成功后必须调用`help.getAppConfig(hash=0)`，读取完整配置中的`freeze_since_date`。非零为冻结；完整响应缺失/零为非冻结。NotModified/畸形配置/RPC失败不能当作已解冻或健康成功。读取只发生在正式健康检查，不用发送消息、加群测试可用性。
4. 只有带权威冻结检查及其开始时间的健康结果可清除冻结事实。按观测时间单调CAS：晚返回的旧健康检查不能覆盖更新的冻结拒绝，晚返回的旧冻结观察不能覆盖更新的完整检查。健康应用同时核对账号授权与连接代次，旧身份结果不得覆盖当前账号。
5. 冻结错误必须在membership群权限处理之前记录并终止本次业务路径，禁止针对冻结错误启动群权限救援。已经进入Gateway但缺少精确无变更证明的动作仍保留unknown；冻结资格和动作结果分别处理，不因账号冻结而重放、改写远端事实或批量终结存量动作。
6. 恢复业务资格须新的完整Telegram配置明确无冻结，且结果未过时；普通连接成功、mock健康对象或缺少冻结观测的结果不能解除已知冻结。已封禁/删除/禁用及授权代次变化不因此修复恢复。
7. 迁移只增加非空默认false标记与nullable时间，不猜测历史错误账号的当前状态，不自动改写历史Action。发布后正常在线worker通过新的只读探测逐账号收敛；之前历史错误范围不能直接视为当前冻结全集。
8. 冻结阻断属于用户要求的业务资格规则，明确报账号不可用/冻结；保留既有恢复入口，不新增重试、静默fallback或吞掉异常。

## 数据与索引

Telegram异常/完整AppConfig → 账号冻结观测CAS → TgAccount冻结事实/状态投影 → 普通任务候选与调用前资格 → Action/Attempt原结果；AppConfig非冻结新观测 → CAS解除冻结 → 原在线检查流程。账号主键更新，无全表错误JSON扫描；不保存AppConfig原文、申诉URL或用户身份敏感信息。

## QA / Release Gate

- SDK错误两类、英文错误、正常群权限分类；冻结membership不得进入权限救援。
- get_me可读但AppConfig冻结；健康无冻结；配置畸形/NotModified/失败；健康client正常断连。
- 新冻结拒绝与旧健康结果顺序颠倒；新非冻结配置与旧冻结结果顺序颠倒；授权/连接代次变化；标记冻结但status在线时候选与调用前仍拒绝。
- unknown动作身份与结果不被账号冻结修复重写，已在途动作保留原证据。
- 后端测试用backend/.venv，每次硬超时60秒；定向no_postgres、必要的PostgreSQL CAS验证，禁止生产测试库重置。
- master→release→Deploy Production；独立核对SHA、迁移、worker。发布后读回冻结观测数量、候选排除、新Attempt时间与当前冻结事实；账号被冻结后新Gateway调用必须为0（此前已开始的调用单列）。确认存量pending不再进入Gateway；通过正式只读AppConfig观察证明真实冻结隔离，不以CI/容器健康代替业务验证。
- 回滚须保留冻结事实及兼容读取；旧版不检查新事实，不能作为恢复业务的默认回滚。

## 官方依据

- https://core.telegram.org/api/auth#frozen-accounts
- https://core.telegram.org/api/config#freeze-since-date

## 验证记录

设计自检：冻结为账号级资格，unknown为动作级结果，两者独立；完整只读配置为解冻依据；CAS防迟到覆盖，运行期再检查防缓存资格。开发/QA/发布/业务验收待本次执行补充。

开发反向审查补正：账号状态在线不能覆盖独立冻结事实；持久化账号范围的membership候选与日覆盖readiness也检查冻结。Telegram get_permissions异常不得把账号冻结强制转换为群权限错误。Gateway只对未发出的调用做冻结拦截，已发出结果单独结算。PostgreSQL使用账号行共享锁与观测更新互斥，忙则沿既有未调用等待路径，不丢弃冻结事实。

定向QA：119项membership/退役/online/冻结测试通过；86项online时序、usage、client生命周期与冻结协议测试通过；2项真实PostgreSQL迁移/并发验证通过（独立schema、正式测试库advisory lock，无生产写入）。新增补充用例及最终代码审查后更新最终计数。

## Release Gate（2026-09-08）

- owner/merge_owner: 本次Codex；隔离工作区codex/frozen-account-eligibility-20260908；主工作区原始clean、未覆盖并发工作。
- design_status=complete；code_review=passed；qa_pass=189项定向no_postgres + 2项真实PostgreSQL并发/迁移；compileall、diff-check通过。
- 代码审查：冻结先于权限救援、独立于unknown；普通连接健康不能解冻；行锁串行化检查与freeze写入；健康代次/时间双校验。仅已有未调用等待语义用于锁忙。
- 发布包含已在master的b72c4d9f诊断脚本提交（仅.github诊断文件，不改变运行业务），以及本次修复。迁移0228为添加账号事实字段；无前端变更。
- 生产发布前只读检查：current=89d837b8；Alembic=0227；authorization runtime=off；ABC无running批次。
- release_status=pending；production_fixed=unproven；发布后复核SHA、迁移和冻结账号新调用，不把上线当作业务验收。

发布前迁移反查补正：0001通过当前模型生成legacy bootstrap，必须排除本次新增两列，避免空库升级0228重复加列。7处迁移head断言同步0228。已补跑13项真实PostgreSQL空库/旧版本升级及round-trip、12项迁移图/合并完整性；最终定向累计216项通过。0228仅在不存在任何冻结观测时允许schema downgrade，有观测则拒绝删除事实；生产继续只采用兼容前向修复。第一轮发布34181003934在部署前主动取消，未触及生产。
