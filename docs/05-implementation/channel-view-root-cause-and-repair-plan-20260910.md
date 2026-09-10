# 浏览低履约：根因与修复方案（2026-09-10）

## 范围与状态

- intake_id: channel-view-root-cause-20260910
- 用户输入：为什么会出现这些问题，怎么解决问题。
- 承接：channel-view-execution-diagnosis-20260910；本次继续只读生产诊断与方案分析，不声明已修复。
- 分类：L3 / P1；状态：已定位具体排期缺陷、计划可行性缺口和历史调用证据阻塞；整个浏览低履约不是一个错误即可解释。
- 证据：[根因只读快照与纯计算回放](evidence/channel-view-diagnosis-20260910/root-cause-evidence.json)。不包含凭据、消息正文、手机号或完整任务payload。
- 本轮生产经历独立发布：起始24704522，结束047625b4。涉及source_owner_cursor/source_pacing/engagement_planning_admission/engagement_view_allocation/engagement_shared_usage/engagement_legacy_occupancy/channel_view_pacing的代码无变化。
- 10:52:05的首次只读查询已取得owner_history，其后进程在容器替换期间以137结束。准入查询已改为每任务最新一行的有界查询并单独完成；10:54:07纯函数回放在新容器完成。各快照不混为同一时刻。
- 结束前current=/data/tgyunying/releases/20260910025140_047625b4；backend、Planner、Dispatcher健康，backend oom=false/restarts=0，health API=ok。本任务没有执行发布、重启或生产写入。

## R1：把最晚的计划释放时间当成新批的起点，造成到期工作被推至日末

### 已确认代码路径

1. channel_view_pacing.create_current_view_actions先调用_attach_view_owner_history。
2. source_owner_cursor._owner_history读取同source/period中其他义务的max(release_not_before_at)。查询不区分该时间是未来计划还是实际远端调用事实，也不要求对应Action已执行。
3. source_pacing._points_after_historical_cursor把本轮点放到该游标之后；已冻结的早期义务，如果释放时间早于该游标，也会进入追加gap的分支。
4. 达到deadline后直接结束本轮点分配，形成pacing_capacity_shortfall。剩余义务继续open，下次再走相同路径。

对应代码：source_owner_cursor.py:143/161；source_pacing.py:235/249/268/271；executors/channel_view_pacing.py:94。

### 线上反例与回放

10:52快照显示，同来源的已绑定义务释放时间已经到22点以后。10:54从每任务读取一个真实、未绑定且有冻结due的义务，调用线上无副作用schedule_source_pacing_points，只输入**已绑定其他义务的最大释放时间作为历史游标下界**，不改数据库，不调用实际Planner。

| 任务 | 原冻结到期时间 | 已绑定历史游标下界 | 纯函数得到的释放时间 |
| --- | --- | --- | --- |
| 太郎日记 | 00:11:28 | 22:55:07 | 22:56:30 |
| 成都阿楠 | 00:06:04 | 22:12:50 | 22:15:55 |
| 西安焦点 | 00:06:43 | 22:33:42 | 22:35:43 |
| 郑州精品 | 00:44:40 | 22:14:43 | 22:16:13 |
| 阿哥日记 | 00:18:34 | 23:51:56 | 23:53:21 |

回放是排期函数反例，不是声称上述新时间已经落库或一定通过后续账号准入。使用下界意味着加入其他未选义务的更晚history不会消除该问题。完整批次的顺序可能进一步推迟。

10:50任务统计最近一轮：太郎requested615/scheduled46，成都887/65，阿哥584/42，西安488/107，郑州395/140。统计中的长期累计shortfall/defers包含重复尝试，不能当作独立未完成浏览数。

### 修复方向

- 浏览义务的原due、账号、消息、任务日和allocation身份保持不变。
- 既有future预约只占其实际时间槽，不把此前所有时间都判作不可用；在真实调用间隔与既有预约前、中、后扫描合法空隙。
- 对已有冻结due/release的未绑定义务恢复同一业务身份，不因其他义务更晚而重新追加到整批尾部。
- 以真实Gateway事实、未决调用、有效预约共同计算安全空隙；不能简单删除全部history约束，也不能把未知调用视为未执行。
- 原截止前仍不可行的义务保留可解释缺口；不能改due、延期到次日或把过期义务记成功。

## R2：计划“数量可分配”与“时间上可完成”没有闭合

### 已确认事实

- 五个active allocation标为achievable，且对应冻结边有portfolio_reserved_units。
- 新鲜PlanningAdmissionSnapshot中的timeline_and_behavior_session统一为status=runtime_gate、blocking=false、revision=null。
- 代码engagement_planning_admission._account_path/_runtime_dependency确实将账号时间线与活动窗口延后到运行时检查；engagement_portfolio_capacity主要核对日行为预算和已占数量，不证明这些操作在账号活动窗与来源间隔交集内存在时间位置。
- 到创建Action时reserve_account_pacing才实际求活动窗口与账号时间线交集；无可行位置抛AccountPacingDeadlineExceeded，被浏览路径记为account_capacity_after_ledger_deadline。
- 10:50的规划准入分别为太郎1077/1124、成都1026/1076、西安983/1068、郑州1041/1123、阿哥1059/1110；均partially_serviceable。这些准入数量也没有额外证明窗口可行，不能替代全天可完成量。

### 修复方向

- 在声明可执行计划前，联立冻结账号—来源边、各账号既有活动窗口、跨任务预约、来源间隔、跨日浏览冷却和source/ledger deadline，生成实际可行的时间分配依据。
- 冻结参与集合、需求量、时间可服务量和实际完成量分别记录。临时不可服务不能偷偷缩小原目标来制造达标。
- 执行时若临时占用改变，在同日仍有效的窗口内为原身份重新寻找合法时间；健康账号独立推进，受阻账号保留明确原因与缺口。
- 保持现有活动窗口和安全间隔合同，不能用取消窗口、任意缩短间隔或放大并发代替可行性闭合。

R1会进一步把原本可用的账号窗口浪费掉，R2又使问题直到创建/领取时才暴露，两者叠加产生反复规划、少量执行。

## R3：历史其他任务的未知调用持续占用账号，而原任务日证据缺失

10:54使用正式只读read_legacy_attempt_occupancy检查三个当前被account_shared_usage_unproven阻塞的账号：

| 账号 | 原操作类别 | 原调用日期 | 原结果 | 原任务日 | 当前投影 |
| --- | --- | --- | --- | --- | --- |
| 30 | authored_message | 2026-08-05 | result_unknown | 无法证明 | remote_inflight=true |
| 365 | authored_message | 2026-07-31 | result_unknown | 无法证明 | remote_inflight=true |
| 411 | authored_message | 2026-08-13 | result_unknown | 无法证明 | remote_inflight=true |

这些是其他旧任务的发消息调用，不是今天浏览刚发生的错误。共享账号占用投影缺少原TaskDayLedger引用和可用pacing_due身份时产生original_task_day_unproven；未证明传输已结束的旧unknown又被全历史扫描保留，assert_shared_evidence因此拒绝同账号的新工作。

这里的remote_inflight=true是当前证据投影，不能解释成已证明旧网络连接至今真实存活。

### 修复方向

- 精确区分旧传输是否结束、旧业务是否成功和原任务日身份，不能把三个问题合并成一个永久占用状态。
- 对可取得权威原进程退出/原Gateway结果的精确Attempt，使用既有受保护preview→apply→readback对账入口；按新鲜hash核验，不伪造日期、回执或原进程退出事实。
- 对账只确认其能证明的状态。旧发送业务结果仍未知时继续保留unknown与防重，不能重新发送。
- 原证据缺失的账号应在规划资格中显式反映同一已知阻塞，保留原需求和缺口，避免重复分配再拒绝；其他健康账号继续推进。

三账号是精确样本，不能外推全部499次检查都来自这三条历史调用。

## 独立未决项：当天10个浏览unknown

上一主快照西安4、郑州6个浏览已进入Gateway但结果未知。不能把它们归为R1/R2，也不能认为修好调度就自动解决。必须核对原调用证据；权威成功补同一事实、权威未执行按原合同释放、无法证明继续unknown。本次没有调用修复或对账流程。

## 实施顺序与验收标准

本节为方案，未宣称Product Design Complete或实现完成；进入开发前要将上述具体反例补齐到浏览专项PRD和统一引擎设计。

1. **先修排期缺陷R1，同时闭合R2的时间可行性合同。** 定向回归至少覆盖“已有23:50未来预约，另一账号较早due仍能在合法空隙创建Action”、同owner重入保持原due、不重复预约、原deadline和source/account间隔不被突破。
2. **对R3做独立精确对账设计。** 新代码必须防止继续生成缺少原任务日/请求身份的业务调用；历史数据只有权威证据可用时才受控处理。验收同时读回transport状态与业务unknown，不混淆。
3. **经代码审查、定向QA和Release Gate上线。** 按master→release→本地`deploy/local_release.py prepare/deploy`→SSH安装→生产真实业务验收执行，遵循[本地直接发布合同](../03-feature-designs/local-direct-production-release-prd.md)，独立核对生产SHA。不能以更高吞吐替代业务身份、间隔和防重测试。
4. **只对符合原合同且可证明未调用的残留受控恢复。** 新鲜精确目标、preview/hash、原截止时间和身份核验、审计及readback；原成功/已调用unknown不得重放。
5. **线上验收看原分母下的差额与事实。** 每任务核对到期未物化量、到期确认缺口、调用前阻塞次数、真实view_observed增量、账号覆盖和日结算。发布后短窗口应证明健康范围持续缩小缺口；完整适用任务日及必要的三日覆盖通过后才可声明对应业务恢复。

不能承诺修完R1即刻完成全部13927次；真实时窗/来源截止已经损失的容量必须保留为短缺，不通过改目标或补假成功结算。
