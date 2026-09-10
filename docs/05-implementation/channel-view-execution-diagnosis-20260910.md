# 2026-09-10 线上统一引擎浏览任务执行诊断

## Intake 与范围

- intake_id: channel-view-execution-diagnosis-20260910
- source / raw_input: 用户“你来看看线上引擎的浏览任务的执行情况”。
- owner / from_agent: 当前任务，prod-diagnosis；to_stage: product。
- suspected_type: online_issue；production_related: true。
- suggested_level: L3；suggested_severity: P1（持续日目标大幅不足）。
- initial_evidence_level: E0；本次 evidence_level: E3 + 已持久化 E4 浏览事实。
- status: reproduced / partial_progress / due_unmet，不能标记 production_fixed。
- authorization: 只读诊断；未修改生产配置或数据、未触发任务、未调用 Telegram、未重试 unknown、未重启或发布。
- affected_scope: tenant 1 的 5 个 running、未删除 channel_view Task；全部 fact_first_v3 + unified_engagement_v1、当前 epoch=2。
- 排除范围：5 个 stopped 旧浏览任务、2 个 completed 历史 canary、11 个已删除任务。无暂停浏览任务。
- observation: 2026-09-10 10:37–10:43 Asia/Shanghai。主表来自 10:39:42 单一快照；专项边界来自 10:41:59 另一快照，不能混称同一时刻。
- release: 24704522d900b672773415ab5dfec78d60f28630；current=/data/tgyunying/releases/20260910022307_24704522。
- runtime: backend 2026-09-10 10:23:44 +08 启动；API health=ok；backend、Planner、两个 Dispatcher、Listener 均 running/healthy，结束前重复读回相同 current。
- 本地代码锚点与生产完整 SHA 相同；代码只用于解释只读生产证据。
- evidence: [只读快照 JSON](evidence/channel-view-diagnosis-20260910/readonly-snapshots.json)。无凭据、手机号、消息正文或完整业务 payload。

## 主要结论

五个任务均有真实浏览事实，当前引擎在推进，但完成量显著低于已到期目标。10:39:42，今日冻结全天目标 13,927，target 已持久化到期量 6,151，确认浏览 571，到期缺口 5,580；确认量相当于到期量的 9.28%。最近 30 分钟新增 66 次、最近 60 分钟新增 132 次。

今日尚未结束，不能宣布全天最终失败；但昨日已关闭任务日同样只有 1,593 次真实浏览，对照冻结全天目标 13,793，比例 11.55%，可确认低完成量不只是本次短时观测。

## 逐任务主表

主表为 10:39:42 的 REPEATABLE READ READ ONLY 快照。到期量直接读取各 target 的 persisted due_count，多数 target 最近更新在10:35–10:39，郑州另有两个早前冻结的小目标；不是用已建 Action 或 obligation 数当分母。全天目标来自 effective_target_snapshot，与当前 active allocation 的 edge_count 一致。实际浏览来自 ViewRemoteFact，并以通用 view_observed 交叉核对。

| 任务 | 当前来源数 | 冻结参与账号 | 冻结全天操作目标 | 已持久化到期量 | 今日确认浏览 | 到期缺口 | 确认/到期 | 最近30分钟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 太郎日记 | 2 | 1,124 | 2,248 | 994 | 6 | 988 | 0.60% | 2 |
| 成都阿楠 | 6 | 1,076 | 3,228 | 1,428 | 88 | 1,340 | 6.16% | 16 |
| 西安焦点 | 4 | 1,068 | 3,223 | 1,420 | 232 | 1,188 | 16.34% | 24 |
| 郑州精品 | 5 | 1,123 | 3,008 | 1,331 | 236 | 1,095 | 17.73% | 20 |
| 阿哥日记 | 2 | 1,110 | 2,220 | 978 | 9 | 969 | 0.92% | 4 |
| 合计 | 19 | 不跨任务去重求和 | 13,927 | 6,151 | 571 | 5,580 | 9.28% | 66 |

每个任务今天确认浏览涉及的不同账号数恰好等于上述确认次数；不能把 571 当成跨任务去重后的账号数。计划 selected cohort 比例为 88.74%–93.46%，各 allocation 当前标记 achievable、全部 edge 有 portfolio_reserved_units；这些计划标签不证明真实履约。

今日数量覆盖午夜至快照，跨越本次部署。10:41 专项读回另外为每个任务取到一次当前部署启动后的 Action→成功 Attempt→Gateway→view_observed 完整链路，证明当前版本继续有真实操作。

## 等待与首个阻塞边界

### 1. 存在 Action 之前的未物化义务

主快照共 4,441 个 open 浏览 obligation 未绑定 Action，其中 1,185 个已有 pacing_due_at 且已到时；另有 3,173 个未冻结 pacing_due_at，其余在未来。各任务已有来源和 active allocation，不能解释为整批无来源。

| 任务 | open未绑定Action | 其中pacing_due已到时 |
| --- | ---: | ---: |
| 太郎日记 | 925 | 222 |
| 成都阿楠 | 1,550 | 289 |
| 西安焦点 | 609 | 265 |
| 郑州精品 | 475 | 200 |
| 阿哥日记 | 882 | 209 |

这证明规划目标尚未全部转成可执行工作。每条未绑定义务具体属于当前账号资格、频道成员准入、账号可用时间或其他容量不足，本轮未逐条归因，保持 unproven；不能仅凭 open 数认定 Planner 软件错误。

### 2. 已有 Action 多数排在未来，当前活动窗口进一步限制领取

主快照 2,708 个 pending 浏览 Action 中，2,569 个 scheduled_at 在未来，139 个已到时。所有这些 Action 关联的账号预约均为 bound；没有发现本范围的 cancelled 账号预约残留。

10:41:59 专项快照以线上正式 current_session_priority 的纯 SELECT 表达式重算：已到 scheduled_at 的 144 个 pending Action 中，139 个 session_rank=1，5 个 session_rank=0。前者表示账号当前活动窗或 release_not_before 尚不满足；后者只表示这一层可通过，还需来源节奏、账号占用等检查。因此不能把 pending 总量或 scheduled_due 总量称作即时可执行量。

### 3. 调用前最集中的原因是来源节奏和旧调用身份不完整

10:39:42 主快照从今日零点累计的浏览 Attempt：

| 结果/原因 | Attempt次数 | 是否进入Gateway |
| --- | ---: | --- |
| success | 571 | 是，均有对应typed浏览事实 |
| pacing_source_not_before | 1,944 | 否 |
| account_shared_usage_unproven | 499 | 否 |
| account_legacy_remote_inflight | 120 | 否 |
| 账号不可用 | 27 | 否 |
| execution_circuit_open | 11 | 否 |
| execution_circuit_probe_pending | 5 | 否 |
| unknown_after_send | 10 | 是，结果未知，不能记成功 |

这些是调用尝试次数，包含同一 Action 的多次检查，不能当成不同账号数或不同浏览义务数。

10:41:59 专项证据：

- 来源 pacing domain=view，当前最小间隔为太郎38秒、成都26秒、西安19秒、郑州28秒、阿哥38秒。source admission 还保留历史26秒等冻结值。不存在本轮所有浏览任务被错误冻结成一天一条的证据。
- 西安 source 的最后真实调用时间10:40:58、next_call_not_before10:50:10；郑州对应10:40:59与10:51:24。该时间线已有预约，不能把十分钟游标差直接解释为没有处理或直接断定是软件缺陷。
- account_shared_usage_unproven 的专项错误均命中 original_task_day_unproven，指旧调用缺少可核准的原任务日身份。该快照分别涉及太郎4、成都8、西安15、郑州18、阿哥3个账号（每任务去重）。它是具体旧调用证据问题，不是证明全部账号不可用。
- 代码映射：engagement_shared_usage.assert_shared_evidence 读到占用证据 issues 后明确拒绝；engagement_legacy_occupancy 在无法确定 original_task_day 时产生该 issue。source_pacing_admission._defer_until 在来源时间未到时记录 pacing_source_not_before 并推迟原释放时间。未执行任何会改状态的 guard/claim/admission 函数。

### 4. 未知浏览结果独立保留

主快照西安4、郑州6个 obligation=unknown，共10个；相应 Action 中7个 closed_unknown、3个 unknown_after_send。它们都有已进入 Gateway 的未知 Attempt，不能由“关闭”标签推导成功或未发生，也不能重放。本轮未执行对账。

## E4 身份与结果核验矩阵

主快照今日全部571条 view_observed，逐条关联其 Action、ExecutionAttempt 和浏览 obligation：missing_link=0、invalid_attempt=0、identity_mismatch=0。核对了Task归属、ledger、当前epoch，以及Action/Attempt/obligation账号一致，Attempt=success且存在gateway_call_started_at。对应 ViewRemoteFact 数量逐任务一致。

| unit | service | schedule/ledger | Action | Attempt/Gateway | remote fact | first blocker | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 太郎日记 | pass | 当前日目标2248/到期994 | open925未绑定；pending393 | success6；主要调用前等待 | view6 | 未物化、活动窗口、来源节奏；旧调用日期证据 | 部分推进；到期未达标 |
| 成都阿楠 | pass | 3228/1428 | open1550未绑定；pending477 | success88；主要调用前等待 | view88 | 未物化、活动窗口、来源节奏；旧调用日期证据 | 部分推进；到期未达标 |
| 西安焦点 | pass | 3223/1420 | open609未绑定；pending719 | success232；unknown4 | view232 | 未物化、窗口/节奏、旧占用和部分账号失败 | 部分推进；到期未达标；unknown |
| 郑州精品 | pass | 3008/1331 | open475未绑定；pending718 | success236；unknown6 | view236 | 未物化、窗口/节奏、旧占用和部分账号失败 | 部分推进；到期未达标；unknown |
| 阿哥日记 | pass | 2220/978 | open882未绑定；pending401 | success9；主要调用前等待 | view9 | 未物化、活动窗口、来源节奏；旧调用日期证据 | 部分推进；到期未达标 |

本次读取的 ViewRemoteFact.counter_increment_proven 全部为false。合同的浏览成功指该账号对冻结来源完成浏览操作；不证明 Telegram 对外展示的阅读计数按相同数量增长。

## 昨日完整任务日对照

昨日 ledger 已为 closed_closed，但关闭不等于达标。以下目标和事实均为本次生产只读读取；不是复用昨日报告。

| 任务 | 昨日冻结全天目标 | 昨日确认浏览 |
| --- | ---: | ---: |
| 太郎日记 | 2,208 | 177 |
| 成都阿楠 | 3,201 | 235 |
| 西安焦点 | 3,219 | 634 |
| 郑州精品 | 3,041 | 378 |
| 阿哥日记 | 2,124 | 169 |
| 合计 | 13,793 | 1,593 |

## 结论边界与工程路由

- root_cause: 已确认低履约的当前断点跨越义务物化与调用前时间/占用准入；旧调用缺原任务日是具体证据缺口。尚未证明一个解释全量不足的唯一代码根因。
- observed_evidence: 当前任务/ledger/目标/计划/Action/预约/Attempt/两类typed事实及旧日目标事实；主快照各查询同事务。
- inference: 本范围主要损耗在进入Telegram之前。执行结果持久化对已确认571条未见漏记或身份错配；不存在由这些结果支持的全局Dispatcher停摆判断。
- affected_scope: 上述5个当前浏览Task；其他任务类型和历史停止Task不纳入统计。
- safe_online_repro: SSH进入既有backend进程环境，SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY；单SQL20秒、锁等待2秒；只读SELECT/正式只读诊断函数。
- code_path_if_relevant: executors/channel_view.py、engagement_shared_usage.py、engagement_legacy_occupancy.py、source_pacing_admission.py、dispatch_session_priority.py；本地相关代码相对线上SHA无差异。
- blocked: 无生产访问阻塞。最初误查宿主8000端口不通，改用部署文档规定18090后health=ok；该访问细节不构成服务故障。
- unproven: 每条未物化义务的完整资格原因、来源预约与账号窗口对全天目标的可行性、旧调用原任务日的可信恢复依据、10个unknown的远端结果、今日最终结算及三日参与覆盖、Telegram计数器增长。
- next_route: 若进入修复，先由product收敛目标/时间/占用证据合同，再dev→qa→product→prod-diagnosis；本次授权仅诊断，未进入实现/生产变更阶段。
- release_gate_required: 后续影响生产的修复需要；本次无发布。
- production_verification_required: 后续必须按同Task/day/epoch重新读取typed浏览事实和日结算，不能以健康、发布或Action数量代替。


## 10:47:37 只读刷新

用户再次要求查看线上浏览任务。本次仍使用相同只读SQL、REPEATABLE READ READ ONLY事务及查询超时，未改变生产状态。runtime版本仍为24704522，backend/Planner/Dispatcher/Listener均healthy。

[刷新快照](evidence/channel-view-diagnosis-20260910/readonly-refresh-1047.json)显示：

| 任务 | 今日到期 | 今日确认 | 较10:39新增确认 | 最近30分钟 |
| --- | ---: | ---: | ---: | ---: |
| 太郎日记 | 1004 | 8 | 2 | 3 |
| 成都阿楠 | 1440 | 93 | 5 | 17 |
| 西安焦点 | 1443 | 239 | 7 | 24 |
| 郑州精品 | 1346 | 241 | 5 | 18 |
| 阿哥日记 | 994 | 11 | 2 | 5 |
| 合计 | 6227 | 592 | 21 | 67 |

约8分钟内五个任务均新增了真实浏览事实，但到期量新增76，确认仅新增21，到期缺口从5580扩大为5635。确认/到期=9.51%，全天冻结目标仍为13927。

调用前pacing_source_not_before累计2021次（+77），account_shared_usage_unproven累计506次（+7）；旧调用未结束120次、账号不可用27次及unknown10个没有增加。当前pending2690个，其中2542个scheduled_at仍在未来、148个已到排期时间。此轮未重新计算活动窗口，不复用上一快照的session_rank数量冒充当前值。

全部592条当日typed浏览事实的Action/Attempt/obligation关联检查仍为missing_link=0、invalid_attempt=0、identity_mismatch=0。结论保持partial_progress / due_unmet；没有证明业务已恢复达标。
