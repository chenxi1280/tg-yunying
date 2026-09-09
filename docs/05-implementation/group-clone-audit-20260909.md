# 独立群克隆任务检查记录（2026-09-09）

- intake_id：intake-20260909-group-clone-audit
- 用户范围：群克隆是独立任务；检查是否通过测试及是否存在问题。此次为诊断，不改业务实现、不发布、不执行 Telegram 写操作。
- suggested_level：L3；severity：P1；status：reproduced；production_related：true。
- 本地 HEAD：43bb3cbb；线上 RELEASE_SHA：8fc0197537c6e57b0901e52bc11ca7396f620b7c。
- affected_scope：group_clone / v2_group_clone 的媒体与相册准入，不涉及四类统一引擎是否支持克隆。
- observed_at：2026-09-09 16:41:49 +08:00（生产数据库时间）。

## 测试结论

现有 `backend/tests/test_group_clone_*.py` 全部执行，62 passed / 15.36 秒。包括 API、生命周期、collector/ingress、账号绑定、顺序控制、模型/迁移专项及双媒体 dispatch。测试运行使用 backend/.venv，每个进程 subprocess timeout=60 秒。

补充诊断反例共 5 项：3 failed、2 passed。三个失败合并为以下两个根因。使用项目既有隔离 SQLite fixture、真实 create/start、事件物化、Action 与 Dispatcher，只有 Telegram 外呼边界使用 SendResult 测试替身。失败断言要求发送边界调用次数为 0，实际均为 1。此证据证明本地实现允许动作越过发送前检查，不能外推为真实 Telegram 已发送成功。

- 失败：黑名单命中的单张 photo 仍到达发送边界。
- 失败：相册第二张 photo 命中黑名单仍到达发送边界。
- 失败：相册第二张 photo 的 protected_content=true 仍到达发送边界。
- 通过：相同黑名单对 text 生效，不生成发送动作。
- 通过：相册第二张包含 entities 且输出规则要求变换时，进入现有人工审核路径，无发送动作。

诊断文件（本机临时证据）：`/tmp/tgyunying-clone-audit-20260909/test_clone_audit.py`；最终输出：`/tmp/tgyunying-clone-audit-20260909/diagnostic-results.txt`。最初的 entities 反例没有配置触发输出变换的 output_checks，不能用于判定生产缺陷；补齐真实触发条件后通过，已排除该假设。

## F1 / P1：媒体过滤拒绝被转换为空文字继续发送

- root_cause：`group_clone_materializer.py::_new_event_content` 在 sanitize_clone_content 返回 None（规则拒绝）时，只要包含媒体便返回空字符串；`group_clone_media_materializer.py::_media_contents` 对相册非首项使用 `sanitize_clone_content(...) or ""`，同样丢失拒绝语义。
- observed_evidence：单图与相册第二图两个黑名单反例均创建发送 Action，通过真实 dispatch 后发送边界被调用一次。
- inference：配置的消息过滤无法阻止媒体复制；被拒绝的 Caption 消失，媒体继续外发。
- code_path：`backend/app/services/task_center/group_clone_materializer.py:214`；`backend/app/services/task_center/group_clone_media_materializer.py:74`。
- contract：专项 PRD 的共享内容过滤、仅复制允许内容及 filtered 不算成功的要求。
- 修复验收要求：明确区分允许的空 Caption 与规则拒绝；单媒体和相册每项都保留拒绝结果，并按既有相册整体/降级合同处理，不把拒绝静默改为空文字。

## F2 / P1：相册只检查首项内容保护，后续受保护项进入发送

- root_cause：`_new_content_events` 只检查当前首事件的 protected_content，随后 prepare_album_events 纳入同 grouped_id 全部事件；admit_media_events 只检查数量与媒体类型，不逐项检查内容保护。
- observed_evidence：第一项正常、第二项 protected_content=true；第二项仍进入第一项相册 Action 的 media_items，真实 dispatch 后到达发送边界一次。后续单独处理第二事件不能撤回已经构造的相册发送内容。
- inference：相册的内容保护准入存在逐项校验缺口。Telegram 是否另行拒绝未实测；平台自身禁止进入 Gateway 的合同已经违反。
- code_path：`backend/app/services/task_center/group_clone_materializer.py:223`；`backend/app/services/task_center/group_clone_media_materializer.py:25`。
- contract：专项 PRD §2.3 / 第58行，保护事件不得下载媒体、复制正文或进入 Gateway。
- 修复验收要求：相册冻结/发送前逐项检查保护状态；任何受保护项不得出现在 Gateway 发送载荷中；明确对应义务和相册状态。

## 线上只读证据

SSH 访问成功；current 指向 `/data/tgyunying/releases/20260909061103_8fc01975`，backend 环境 RELEASE_SHA 为上述完整 SHA。连接生产 DB 后执行 SET TRANSACTION READ ONLY 与 statement_timeout=10s，只读取类型、状态、ID及聚合计数，不读取消息正文或账号凭据。

| unit | service | schedule/ledger | Action | Attempt/Gateway | remote fact | first blocker | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| group_clone | backend/DB 可访问，SHA 已读回；未做全 worker 健康验收 | Task=0，SourceEvent=0，Obligation=0 | 0 | 无 Task 链可核验；未发送真实测试消息 | CloneMessagePart=0，无任务链证据 | 无真实任务验收样本 | unproven |

补查旧 `group_relay` 与 `group_clone` 的 Task/type/status 聚合均无行，避免将旧监听转发当作未查询到的 Clone。此次未直接统计全部 FulfillmentRemoteFact 或 Attempt 表，不把域映射零行写成独立事实表的查询结果。

涉及两个缺陷的本地代码与已读回线上 SHA 的 Git 对象无差异；这是候选源码对照，不是容器源码逐文件哈希证明，更不表示线上已发生同样副作用。

## 既有交付边界与判定

当前专项 PRD §18.2 仍列媒体缓存/fingerprint、完整 cutover exclusion 与有输入/远端 mutation 后的 rollback、PostgreSQL/真实 Telegram E4 等未闭合项。这是当前文档声明，本轮没有逐项重做其全部实现审计。

- local_existing_tests：pass，62 项。
- local_new_counterexamples：failed，3 项，对应两个 P1 根因。
- safe_online_repro：仅当前状态只读盘点；未创建/启动任务，未调用 Telegram 写接口。
- blocked：没有真实 Clone 任务样本可用于线上全链验证。
- unproven：真实 collector → SourceEvent → Obligation → Action → Attempt/Gateway → typed remote fact 的生产 E4。
- overall：不能写 qa_pass、production_accepted 或 production_fixed。
- next_route：产品合同核对 → 定向开发修复 → QA；本次检查没有实施上述修复。
