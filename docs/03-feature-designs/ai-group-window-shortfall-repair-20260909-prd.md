# AI活群活动窗口与发送供给修复合同（2026-09-09）

## Intake / Bug Batch Plan

- intake_id: AI-GROUP-WINDOW-SHORTFALL-20260909；L3/P1。
- 用户原话：“你来把问题修复好后统一部署测试”。范围为本任务已经只读定位的AI活群低履约；代码修复、定向测试、统一候选和正式发布已获授权。
- 当前阶段：prod-diagnosis；W1/W5 design_status=complete，实现/158定向/完整CI及正式部署通过，生产SHA839c7b19。全量10任务仍未达标；W2保留现行来源间隔/独占预约合同，W3本次反查没有证实新分配回归，W4外部准入及unknown不得伪造闭合。最终逐Task证据见统一Release Gate末节。
- 原生产锚点5088d001：21:33:14全部10个未删除running活群，当前日投影应完成17979、确认720；最近30分typed消息65、来源节奏推迟277次。21:34:54独立快照到时ready1087，1059不在既有必需活动窗，release未来0。两快照不混算。
- 主工作树存在其他任务变更；本切片在独立worktree `codex/ai-group-window-shortfall-20260909` 基于fa26ddbd实现。Clone当前候选部署/受控验收期间不移动master/release；后续统一候选另行审查。

## 根因分组与未闭合项

1. W1 账号窗口与时间线：反查 `reserve_account_pacing` 的窗口投影后，账号时间线是否把最终effective推离该窗口；claim重验是否同样只有一次窗口投影。必须用真实入口反例，不以“窗口外队列多”直接证明算法错误。
2. W2 来源预约与账号窗口：核对正常source admission推迟后的账号窗口、原due/release、source tail和重复Attempt。定位已冻结预约的具体失配，不将所有`pacing_source_not_before`当异常，也不把未来预约全部清空。
3. W3 组合额度：已发布R3过滤保留；新观察到郑州师范/成都6次执行额度拒绝，必须区分旧超配与新分配后再决定改动。
4. W4 准入、不可见和未知结果：不得把历史`unknown`当未调用、不得将`not_visible`计完成。普通代码可以修复错误恢复/资源投影，但缺可信群管证据或外部权限不能伪造成功。
5. W5 验收可信度：独立E4报告切片88046287/d77a853d经审查、真实PG与整合QA后纳入后续统一候选；第11条反例属于频道浏览自动发现，不改变当前10活群范围。

## 本次不变的产品约束

- 原Task、tenant、目标群、账号范围、已冻结数量分母/原任务日/原due/真实deadline保持；未完成量不通过缩目标或跨日冲抵消除。
- 账号真实活动窗、跨任务间隔、来源间隔、额度、权限及内容审核继续执行；不直接改成全天可发、不提高预算、不关闭审核、不放宽15秒调用合同。
- 只有可证明尚未进入Gateway的工作可以沿正式未调用收口重排；Provider已调用身份/成本/unknown保留。不能用进程重启或超时作为远端未执行证据。
- 生产数据修复如确有必要，须独立精确preview/hash/apply/readback合同；本轮授权不是对所有历史数据的无条件清理授权。

## Product Design Complete待验证清单

- [x] 当前生产精确样本及其原预约/窗口/Attempt链。
- [x] W1/W2真实入口可复现反例及当前产品合同兼容性。
- [x] 窗口与时间线交集算法、终止条件、deadline半开边界及无解显式状态。
- [x] 原准备/已调用/unknown及跨日身份不变；锁序与幂等无额外副作用。
- [x] QA反例、并发PG、旧legacy与其他adapter回归。
- [x] 发布Gate、独立SHA/runtime、逐任务typed事实与日目标缺口读回；已完成读回不等于业务通过，10任务仍未达标，新建预约线上样本未出现。

每个切片设计闭合前不实施其生产代码。上线后有部分新增消息不等于全日达标；无法在原窗口弥补的历史缺口仍如实记录。

## W1 Product Handoff（design_status=complete / resync=true）

21:40:14生产只读样本：Action `9a64e481`、账号59，原账号活动窗11:38–12:08，reservation.release=11:38，最终effective=12:08:40，零Attempt。另有账号1141窗口09:25–09:59而预约effective=10:00:15。当前`_resolve_reservation_timing`只在计算账号时间线之前调用一次窗口投影；`revalidate_action_pacing_before_claim`也先窗口、后账号/群间隔，最终effective未再次与活动窗求交。这里是实际排期约束未闭合，不是授权放宽窗口。

### 最小实现合同

1. 预约与claim共用“窗口→账号/群时间线→窗口复核”的单调求交逻辑。若时间线把候选推到当前窗口之外，转到下一个原有窗口并重新核对时间线，直到找到同时合法的点或原deadline前无解。
2. 不增加窗口、唤醒次数、容量、隐藏最大迭代次数；每次回环必须进入严格更晚的窗口，使用原有限窗口和deadline自然终止。合法首点保持旧值，不改变原due、随机样本/计划hash。
3. 预约只改变合法release/effective；重建未绑定预约与新预约消费同一逻辑，原已绑定预约不在reserve阶段偷偷改写。claim按原账号→Task锁序核对，尚未调用的旧错位工作在正常claim时收敛到合法窗口；窗口终点和deadline均为排他边界。
4. 明确点名且已取得原wake预算的micro-session继续原wake语义；此次不扩大wake准入，不多扣预算。legacy非Session策略维持既有账号时间线行为。
5. 找不到合法窗口使用既有`account_behavior_session_unavailable`/`pacing_claim_deadline_exceeded`路径；时间线本身越过真实deadline使用既有timeline/deadline失败语义。不得返回一个窗口外的“成功”预约。
6. 代码边界：将账号窗口求交与claim状态结算按责任抽出，以满足现有account_pacing_guard超过500行问题；公开入口和异常类型引用保持，其他adapter只获得同一既有窗口合同的一致执行。无API/schema/前端配置变化。

### QA与生产验收

- 真实reserve：当前窗口剩余时间不足账号间隔→下窗口；下窗口另有已预约工作→再次按间隔求交；半开end、deadline无解；原due不变；跨Task共享同账号原额度/间隔不变。
- 真实claim：账号或群间隔把候选推出窗口后，不持久化窗口外effective；下一合法窗再次claim通过；无窗口按原安全结算，不伪造Gateway调用。
- 既有wake/legacy/UTC转换/账号与Task锁忙/重复reserve回归通过；后端测试每进程60秒硬超时。
- 生产分别核对新建预约是否落在原窗口、正常claim后的effective是否仍合法、来源后续推迟是否造成新的交集缺口，以及实际typed消息；W1通过不等于W2/W3/W4或全日目标完成。

本节仅W1允许进入dev；W2/W3仍反查，W4外部控制事实缺口保持unproven。后续如改变本节合同必须再次resync。

## 统一候选反查结论与范围冻结（21:58）

- W2：统一引擎§19.18明确采用共享尾游标分配失效预约的独占时刻。当前source_pacing_cursor实现与该条一致；不得为了降低等待数字删除预约、跳过last-call间隔、开启未配置capacity v2或改成空隙插队。Source推迟后再次领取必须满足原release及合法账号窗口，本切片W1入口回归覆盖这一交接；仍不保证来源和账号窗口在原deadline前必有交集。无解保留原shortfall，不计完成。
- W3：21:57:03在fa26ddbd只读查询10个running活群最近2小时的task_account_portfolio_capacity_exhausted，22个Action全部创建于11:04–15:56，早于已发布R3。新创建分配同类失败未在该范围检出；继续现有额度与正式未调用收口，不以存量超配失败推断需要提高预算。
- W4：pending_admission、原Provider unknown、结果不可见继续原类型化事实合同。不清除unknown、不重放原发送、不绕过群管要求。本候选不宣称这部分已经修复。
- W5：独立提交88046287/d77a853d逐文件审查后整合为4e3628b0/2dc85657；索引追加冲突保留双方内容。验收脚本只读事务先于首个查询，真实消息必须同tenant/task/ledger/account及remote id，且Gateway与fact均在新发布锚点之后；unified abandoned不移出必达分母。
- 迁移/API/前端/生产配置：无。正常Planner/claim生效；无历史数据批量apply，无清理Task/Action。原due/day/数量/间隔/未知身份保持。
- 发布准入仅覆盖W1/W5已闭合切片；发布后逐Task区分窗口一致性、真实新增消息、日目标与准入/unknown阻断，任一必达缺口存在不得写production_fixed。
