# Worklog: prod-diagnosis

## 2026-07-08 硅谷 recovery CPU 最终生产 E4 通过

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-prodverify-fixed-f4c66fc5-001
- action: 对多轮返工后的硅谷 recovery CPU 背压修复做最终生产 E4 复核。
- input: Deploy Production run `28935442307` success；release head `f4c66fc5146ff21421494d88eec833f5337f0d62`。
- output: `production_fixed_recovery_cpu_backpressure`
- evidence: 公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`；硅谷 `47.251.126.134` 上 `tgyunying-backend` 与 `tgyunying-worker-recovery` 均运行镜像 `ghcr.io/chenxi1280/tg-yunying-backend:f4c66fc5146ff21421494d88eec833f5337f0d62` 且 healthy；worker healthcheck 已切换为读取本地 heartbeat 文件，`docker inspect` 显示 `WORKER_LOCAL_HEALTHCHECK_FILE` / `/tmp/tgyunying-worker-heartbeat`，不再使用 `python -m app.worker_health`；生产采样未发现 `app.worker_health` 进程。
- evidence_detail: 事故初始 load 多次在 8+，曾见 `tgyunying-worker-recovery` 单次 CPU 148.69%；最终 E4 采样 load 降至 `2.85, 4.94, 6.33`，随后 `3.15, 5.18, 6.44`；即时复查 `tgyunying-backend 0.14%`、`tgyunying-worker-planner 7.03%`、`tgyunying-worker-recovery 0.65%`。追加约 5 分钟持续观察中，load 保持在 `2.35-4.14` 一分钟负载区间，recovery CPU 大部分低于 1%，一次短峰 `16.57%`，同窗 `recovery_errors=0`。生产 DB 只读：`stale_membership_executing=0`；recovery 日志近窗关键错误 `worker drain failed=0`、`Task was destroyed but it is pending=0`、`telegram_probe_timeout=0`、`telegram_probe_connection_error=0`。
- decision: 本次 L3 recovery CPU 背压修复已取得 E4，可写 `production_fixed`。Telegram `Server closed the connection` 仍有残余日志，但当前未形成 recovery CPU tight loop；同机偶发 `python -m tg_v_chat.healthcheck` 高 CPU 属于邻近 `tg-v-chat` 容器，不属于本次 `tg-yunying` 修复范围。
- next_agent: product
- unresolved: 无本次阻断项；若要继续降低 Telegram `Server closed` 噪声或治理 `tg-v-chat` healthcheck，应另起独立事件。

## 2026-07-08 硅谷 recovery CPU 持续升高线上止血

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-prod-diagnosis-001
- action: 排查并临时止血硅谷生产服务器 CPU 持续升高问题。
- input: 用户反馈“线上硅谷的服务器，cpu 占用一直增加”。
- output: `production_incident_reproduced_recovery_stopped_fix_pending_release`
- evidence: 生产 SSH 直连 `root@47.251.126.134`，主机 4 核，load 多次在 8+；`tgyunying-worker-recovery` 单次 `docker stats` 到 148.69%，累计网络 I/O 150GB/2.54GB；recovery 日志 10 分钟内 `worker drain failed` 18 次，20 分钟内 38 次，栈落在 `_recover_unknown_membership_action -> gateway.probe_target_capabilities -> TelethonClientLifecycle.run`，异常为 `TimeoutError` 并伴随 `Server closed the connection` / `Task was destroyed but it is pending`。生产 DB 统计：`unknown_after_send=1043`，running unknown membership=126，stale executing=27。2026-07-08 14:04:52 CST 已仅停止旧 `tgyunying-worker-recovery` 容器，未写数据库；停止后 load 样本 `7.16, 7.83, 8.24`。
- decision: 根因定位为 recovery 对历史 unknown membership 高频 Telegram 补偿复检且 timeout 未显式落库/冷却，Telethon 超时后未取消后台 coroutine；旧 recovery 已临时停止以止血。当前只能写 incident reproduced / stopgap applied，不能写 `production_fixed`。
- next_agent: dev
- unresolved: 新代码尚未发布；需要 Deploy Production 后确认生产镜像版本、recovery 容器重新 healthy、`worker drain failed` 下降/归零、CPU load 降到 4 核可承受范围，并复查 DB `telegram_probe_timeout` 冷却字段。

## 2026-07-08 硅谷 recovery CPU 首次发布后 E4 失败

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-prodverify-failed-001
- action: 对 release candidate `889e94635541bf937f4fc259f06435f7397fbc5e` 做生产 E4 复核。
- input: Deploy Production run `28921986236` success；公网 `/api/health` 返回 `{"status":"ok"}`。
- output: `production_verification_failed_connection_error_branch`
- evidence: 硅谷 `47.251.126.134` 新镜像已落地，backend 与 recovery 均为 `ghcr.io/chenxi1280/tg-yunying-backend:889e94635541bf937f4fc259f06435f7397fbc5e` 且 healthy；但部署后约 4 分钟内 `tgyunying-worker-recovery` CPU 仍约 83%，15 分钟内 `worker drain failed=5`。失败栈仍在 `_recover_unknown_membership_action -> gateway.probe_target_capabilities`，本次异常为 `ConnectionError: Connection to Telegram failed 5 time(s)`，前置日志为 `Could not connect to proxy tgyunying-mihomo-024:7890`。生产 DB 只读统计：stale membership executing=1、unknown membership total=126、timeout_unknown_membership=0。
- decision: 首次发布不是 production_fixed；根因同属 recovery Telegram reprobe 未把网关连接失败转为显式冷却状态，需要 dev 返工处理 `telegram_probe_connection_error`。
- next_agent: dev
- unresolved: 连接失败补丁尚未重新发布；CPU/load 降载、`worker drain failed` 清零和冷却字段 E4 仍 pending。

## 2026-07-08 硅谷 recovery CPU 二次发布后 E4 失败

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-prodverify-failed-lifecycle-001
- action: 对 release candidate `f96dfa4eb306030e8509dac85165a39e1f896153` 做生产 E4 复核。
- input: Deploy Production run `28922993123` success；公网 `/api/health` 返回 `{"status":"ok"}`。
- output: `production_verification_failed_telethon_lifecycle_pending_tasks`
- evidence: 新镜像 `f96dfa4e` 已落地且 recovery healthy；第一轮样本 `worker_drain_failed=0`、`pending_task_destroyed=0`，但 3 分钟后 recovery CPU 约 96.91%，5 分钟内 `Task was destroyed but it is pending=796`、`Server closed=784`、`missing_proxy_024=6`。DB 只读确认 stale executing 样本已变为 `unknown_after_send`、lease 已清空，说明状态机返工有效，但 Telethon connect failure 后台任务仍在残留。
- decision: 二次发布仍不是 production_fixed；根因推进到 `TelethonClientLifecycle.get_or_create_client` connect failure 后未 disconnect 新 client。已停止线上 recovery 容器临时止血，等待 dev 发布 lifecycle cleanup。
- next_agent: dev
- unresolved: lifecycle cleanup 尚未发布；三次发布后需复核 recovery CPU、pending task、server closed 和 worker drain failed。

## 2026-07-07 MiniMax-M2.5 成人语境生产探针

- message_id: 2026-07-07-minimax-m25-adult-context-prodprobe-001
- action: 按用户要求在线上生产容器内使用真实 MiniMax provider key 测试 `MiniMax-M2.5` 对成人/色情语境频道评论的生成支持；只记录分类指标和哈希，不回显样本文本、密钥或候选原文。
- input: 用户要求“测试一下 minimax2.5 对色情情况的支持，如果好，我们就用 minimax2.5”。
- output: provider_already_default_m25_adult_context_partially_supported_not_fully_stable
- evidence: 生产 SSH 直连确认当前 release `/data/tgyunying/releases/20260707075044_a0f1a8e`，backend/worker 容器均为 `a0f1a8e3aea6b59c8865efa52d244ce81b62f4ba` 且 healthy；租户 1 `ai_enabled=true`、`fallback_to_mock=false`、`default_provider_id=4`，provider id=4 `MiniMax`、`model_name=MiniMax-M2.5`、`health_status=健康`；`AiGateway.check` 返回 `provider ready; chat capability ready`。
- evidence_detail: 受控 `count=4` 成人语境探针中，成人夜生活样本返回 `candidate_count=4/clean_count=4/refusal_hits=[]/facilitation_hits=[]/review_tone=false/ai_meta_count=0`，成人服务边界样本返回 `4/4/[]/[]/false/0`；直接标注为色情内容频道的样本返回 `candidate_count=3/clean_count=3`，出现 `refusal_hits=["色情内容"]`、`facilitation_hits=["telegram"]`、`review_tone=true`，未给满 planner 批次上限 4。阿哥日记最近 3 条真实频道消息 `count=4` 探针分别返回 `2/2`、`4/4`、`2/2`，均无拒绝和 AI 过程文本，其中 1 条 `review_tone=true`。
- decision: `MiniMax-M2.5` 已是生产默认模型，不需要再次切换；它对成人暗示和成人服务边界语境可以生成非露骨评论，但对明示“色情内容”的场景仍可能输出审核/分类口吻或候选不足。因此不能把本次测试写成“色情语境完全支持”，只能写“可用但不稳定，仍依赖候选质量过滤和 planner 候选不足显式失败”。
- next_agent: product
- unresolved: 未对生产配置做写入；未创建真实 Telegram 发送动作；明示色情标签场景下的 review-tone 候选清洗仍不是完全覆盖。

## 2026-07-07 Clash egress 安全预检生产复核

- message_id: 2026-07-07-clash-egress-preflight-prodverify-001
- action: 使用 workflow_dispatch 对生产 Clash 订阅节点做 `apply=false` 安全 egress 预检，不写数据库、不批量改账号绑定、不创建郑州 smoke 任务。
- input: 2026-07-06-account-mask-clash-node-batch-bind-001；用户要求继续完成。
- output: clash_egress_verified_db_apply_unproven
- evidence: Deploy Production workflow_dispatch run `28808100947` 在 release head `e23144537af5477c86ed038097b8145ca09626d1` 通过 checks、build-images、deploy；生产发布后公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，公网 `/` 返回 HTTP 200。
- evidence_detail: `Configure Clash proxies and Zhengzhou smoke task` 步骤 success；`CLASH_LIVE_APPLY=false`；`CLASH_CONFIG_PREPARED` 显示 `node_count=8`；真实 egress 探测结果为节点 1 `CLASH_PROXY_EGRESS_FAILED`，节点 2-8 `CLASH_PROXY_EGRESS_OK`，`CLASH_PROXY_HEALTHY_INDEXES=2,3,4,5,6,7,8`；日志明确 `CLASH_DB_APPLY_SKIPPED apply=false`。
- decision: 已证明生产订阅中有 7 个 Clash 节点可通过临时 Mihomo 容器真实出网，且本次没有改动账号绑定或创建任务。账号面具选择 Clash 节点的代码路径已发布；节点可用性具备生产预检证据。
- next_agent: product
- unresolved: `apply=true` 会对生产账号环境做批量绑定并创建郑州 smoke 任务，当前缺少用户指定的账号中心分组、授权槽位、目标 Clash 节点和确认范围；真实线上账号绑定样本、远端授权设备变化和郑州搜索加入 smoke 仍未执行。

## 2026-07-06 账号面具代理修复发布生产复核

- message_id: 2026-07-06-account-mask-proxy-release-prodverify-001
- action: 对账号面具分组批量绑定代理、账号面具选择 Clash 节点、账号代理 / 授权指纹 Tab 拆分和用户确认的 AI 敏感处理撤销完成 release 发布复核。
- input: 2026-07-06-account-mask-pool-proxy-batch-bind-local-qa-001；2026-07-06-account-mask-clash-node-batch-bind-local-qa-001；2026-07-06-account-mask-tabs-split-local-qa-001；2026-07-06-account-mask-proxy-review-fixes-local-qa-001；2026-07-06-ai-sensitive-processing-reverted-local-qa-001。
- output: release_gate_passed_prod_health_ok
- evidence: Commit `92e36126` 已推送 `master` 和 `release`；Deploy Production push run `28802430977` 在 release head `92e361268e95671d9ba9aaee28e0767441915880` 通过 checks、build-images、deploy；checks 中 Backend checks 和 Frontend build 均 success；公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，公网 `/` 返回 HTTP 200。
- evidence_detail: 上一次 run `28799800009` 因测试仍期待敏感上下文清洗而失败；本次按用户最新口径“模型支持暗示，不要再做处理”更新测试契约，CI 后端套件通过。发布内容不包含敏感上下文改写、MiniMax 旧模型自动降级或 mock success；频道评论 / 引用回复 prompt 原文透传给当前配置模型，模型拒绝或候选不足继续显式失败。
- decision: 账号面具相关代码和前端静态包已生产发布，生产健康通过；系统设置 Clash 页仍只维护订阅源，不做账号分配；账号面具页负责账号中心分组批量绑定本地代理或健康 Clash 节点。
- next_agent: product
- unresolved: 本次 push deploy 中 `Configure Clash proxies and Zhengzhou smoke task` 等 workflow_dispatch-only 生产动作是 skipped；真实线上账号批量绑定操作样本、真实 Clash 节点绑定样本、远端授权设备变化和“阿哥日记”任务真实恢复仍需单独生产执行证据。

## 2026-07-06 AI 活群 hard-hourly 分布护栏补齐线上复核

- message_id: 2026-07-06-ai-group-hard-hourly-distribution-guard-prodverify-002
- action: 发布并 SSH 直连复核 hard-hourly 旧队列清理、在线样本和分布门禁补齐。
- input: 2026-07-06-ai-group-hard-hourly-distribution-guard-qa-002
- output: production_deployed_ssh_verified_with_online_health_residual
- evidence: Commit `7f50d75c` 已推送 `release`；Deploy Production push run `28795358424` 的 checks、build-images、deploy 均通过；生产 SSH 直连确认 `/data/tgyunying/current -> /data/tgyunying/releases/20260706133733_7f50d75`，backend / planner / dispatcher / listener / recovery / account-online / ai-memory / metrics 等容器均运行 `ghcr.io/chenxi1280/tg-yunying-backend:7f50d75c08846eefd47adcf2b0a2b24ad7ed1b09` 且 healthy；公网 `/api/health` 返回 `{"status":"ok"}`，`/task-center` 返回 HTTP 200。
- evidence_detail: 生产容器内直接调用 `_hard_hourly_distribution_skew([101,101,101], 3)` 返回 `{"max_consecutive_run":3,"unique_account_count":1}`，`[101,101,102,102,103]` 返回 `{}`；SSH 直连 `prepare_open_actions_for_planning` + `drain_task_planner(limit=20)` 后无新增规划（processed=0）。当前 hard-hourly open action 共 11 条，其中石家庄未来 4 条序列 `[271,271,668,668]`、`max_run=2` 且 ready_account_count_for_replan=1；青岛师范学院未来 5 条序列 `[271,668,668,668,668]` 但 ready_account_count_for_replan=0，未触发分布偏斜清理，归因于账号在线健康不足而非多账号可用下的规划偏斜。运行中任务已暴露在线样本：青岛 `offline_count=65` sample `[2,19,119,130,236,264,387,405,523,528]`，天津 `offline_count=77`，石家庄 `offline_count=45`，郑州大学 `offline_count=22`，郑州楼凤 `offline_count=24`。
- decision: 本次补齐代码已生产部署；分布门禁和在线样本已在生产容器验证可用。当前线上仍有 AI 活群账号在线健康缺口，导致部分旧 pending 无法按多账号重规划清理；该问题不再归类为 hard-hourly 分布门禁代码缺失。
- unresolved: 未使用 workflow_dispatch AI_GROUP_QUALITY_DONE 作为证据；账号在线健康恢复仍需单独处理。

## 2026-06-27

- message_id: 2026-06-27-docs-practice-incident-001
- action: 输出文档级演练的 Incident Report
- input: 本地 `tg-yunying` 缺少可复用的四 Agent 协作材料，线上问题闭环容易把验收通过误当生产恢复
- output: 将问题交给 product Agent 定义修复范围
- evidence: `docs/05-implementation/multi-agent-practice/` 初始目录缺失
- decision: status=reproduced，severity=P2
- next_agent: product
- unresolved: 本次未访问真实线上服务；真实线上问题仍需要单独生产证据闭环

## 2026-06-27 document-level production verification

- message_id: 2026-06-27-docs-practice-prodverify-real-001
- action: 完成文档级演练生产复核
- input: QA 第二次 recheck 已返回 pass，主控线程已发送 `2026-06-27-docs-practice-prodverify-real-001`
- output: document_flow_verified
- evidence: 本地文件存在，模板、登记表、worklog、演练记录齐全；真实 prod-diagnosis 线程已返回文档级复核结论
- decision: 四 Agent 文档协作闭环已完成；本结论不代表线上业务恢复
- next_agent: product
- unresolved: 本次未访问真实线上服务；真实线上问题仍需要单独生产证据闭环

## 2026-07-04 搜索目标群点击任务 pacing 发布生产复核

- message_id: 2026-07-04-search-join-pacing-prodverify-001
- action: 对 `search_join_group` pacing / 账号上限发布完成生产复核
- input: 监督子代理指出 4 个 release blocker，dev 已修复并完成本地验证，随后按 `master -> release -> Deploy Production` 发布
- output: release_gate_passed_prod_health_ok
- evidence: Deploy Production run `28694612968` 在 release head `52c97c93b47d52781f4d6e4b0b47f431a13e49fc` 通过 checks、build-images、deploy；公网 `/api/health` 返回 `{"status":"ok"}`；公网 `/task-center` 返回 HTTP 200 text/html，Last-Modified 为 `Sat, 04 Jul 2026 04:19:42 GMT`
- decision: 代码发布与生产健康通过；不声明郑州 3 账号真实搜索入群灰度完成
- next_agent: product
- unresolved: 真实目标机器人协议样本、真实代理出口、机场节点容灾、授权槽位环境栈和郑州 3 账号线上加入测试仍需单独生产执行证据

## 2026-07-04 授权槽位代理事实源修正发布生产复核

- message_id: 2026-07-04-account-proxy-slot-runtime-release-001
- action: 对账号面具授权槽位代理/指纹运行时修正完成生产发布复核
- input: 2026-07-04-account-proxy-slot-runtime-fix-local-qa-001；子代理指出的代理重绑唯一索引冲突和 Dispatcher 未校验 `account_proxy_bindings` 行本身已修复
- output: release_gate_passed_prod_health_ok
- evidence: Deploy Production run `28700295899` 在 release head `f44a5e25500ce940cfff556eb83fdc7022682af0` 通过 checks、build-images、deploy；`origin/release` 与 `origin/master` 均为 `f44a5e25500ce940cfff556eb83fdc7022682af0`；公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`；公网 `/task-center` 返回 HTTP 200 text/html
- decision: 代码发布与生产健康通过；不声明远端 Telegram 授权设备已立即变更，不声明真实 Clash 同步、真实出口 IP 观测或郑州 3 账号真实加入测试通过
- next_agent: product
- unresolved: 本次 workflow 中 `Configure Clash proxies and Zhengzhou smoke task` 等可选生产动作是 skipped；线上 Clash 订阅同步、账号授权指纹重登生效、远端授权快照刷新和郑州 3 账号真实加入测试仍需单独生产执行证据

## 2026-07-04 账号面具环境同步发布生产复核

- message_id: 2026-07-04-account-mask-environment-sync-release-001
- action: 对账号面具/全局 Clash/授权指纹/远端观测/search_join 设计补齐和实现完成生产发布复核
- input: 用户要求完整梳理 PRD 缺口并写入 PRD，按 PRD 完成功能代码、子代理监督、代码校验和线上部署
- output: release_gate_passed_prod_health_ok
- evidence: 本地定向验证 `20 passed`，search_join / Clash 相关套件 `47 passed`，全量 `pytest -m no_postgres` 为 `740 passed, 787 deselected, 5 warnings`，`compileall`、迁移 `py_compile`、`git diff --check` 和 `npm --prefix frontend run build` 均通过；Deploy Production run `28702682033` 在 release head `c79926d6f4eec2481a679eaa70d62f41a2b29f67` 通过 checks、build-images、deploy；`origin/release` 与 `origin/master` 均为该 commit；公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，公网 `/task-center` 和 `/` 均返回 HTTP 200 text/html
- supervisor: 子代理复核指出 Clash 保存后沿用旧节点、远端观测全量授权列表误归属、测试文件超 500 行等 blocker；主线程已修复并补回归测试
- decision: 代码发布与生产健康通过；PRD、专项设计、数据流转索引和项目结构索引已同步
- next_agent: product
- unresolved: 本次 push deploy 中 `Configure Clash proxies and Zhengzhou smoke task` 等 workflow_dispatch-only 生产动作是 skipped；真实生产 Clash egress、全账号远端授权刷新、授权指纹重登生效和郑州 3 账号真实搜索点击/加入测试仍需单独生产执行证据

## 2026-07-05 Clash 多订阅源池发布生产复核

- message_id: 2026-07-05-clash-multi-subscription-release-001
- action: 对系统配置 Clash 多订阅源池、主备优先级、订阅级同步和 search_join 全订阅不可用 fail-closed 完成生产发布复核
- input: 用户补充“Clash 支持配置多个地址，主的掉了使用备用的，可以配置多个”；Hegel 只读监督指出 plural sync 未跑健康探测、全订阅不可用未发管理员通知两个 P0，dev 已修复并完成本地验证
- output: release_gate_passed_prod_health_ok
- evidence: 本地 `pytest backend/tests -m no_postgres -q` 为 `757 passed, 787 deselected, 5 warnings`；前端 `npm run build` 通过；`git diff --check` 通过；commit `7415de0ab033cc76837b3e1976eac455485b42e9` 已推送 `release` 和 `master`；Deploy Production run `28732700242` 通过 checks、build-images、deploy；公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，公网 `/task-center` 和 `/` 均返回 HTTP 200
- decision: 代码发布与生产健康通过；系统配置已具备多个 Clash 订阅地址、已有订阅编辑、主备优先级、启停、逐条同步和全部启用订阅不可用停手通知的代码路径
- next_agent: product
- unresolved: 本次 push deploy 中 `Configure Clash proxies and Zhengzhou smoke task` 是 skipped；真实生产 Clash 订阅拉取、真实节点出口 IP 观测、健康节点自动固定到授权槽位、运行中 failover event、warmup 重置和郑州 3 账号真实搜索点击/加入测试仍需单独生产执行证据

## 2026-07-05 Clash 运行中 failover 绑定发布生产复核

- message_id: 2026-07-05-clash-failover-runtime-release-001
- action: 对 search_join 运行中代理失败后的 Clash 同订阅 / 备用订阅 failover、授权槽位绑定切换、warmup 重置和通知状态完成生产发布复核
- input: Nash / Copernicus 子代理监督指出运行中 failover、exit observation、warmup、pending action 重指向和全订阅不可用通知缺口；dev 已按红测修复并完成本地验证
- output: release_gate_passed_prod_health_ok
- evidence: 本地定向 `backend/.venv/bin/python -m pytest backend/tests/test_proxy_airport_subscription.py backend/tests/test_proxy_airport_failover.py backend/tests/test_search_join_group_linked_tasks.py backend/tests/test_merge_integrity.py -q` -> 32 passed；全量 no_postgres 60s gate -> `764 passed, 787 deselected, 5 warnings`；`backend/.venv/bin/python -m compileall -q backend/app`、`git diff --check`、迁移 / 服务 `py_compile` 和 `npm --prefix frontend run build` 均通过；commit `47a943ba916d2cdaebb8e0e1cb61c6bca5d8a223` 已推送 `release` 和 `master`；Deploy Production run `28742136991` 通过 checks、build-images、deploy；公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，公网 `/task-center` 和 `/` 均返回 HTTP 200 text/html
- supervisor: Copernicus 复核确认 3 个 blocker 均已修复：健康候选不再依赖 observed exit IP、failover 后 pending / retryable_failed search_join payload 会重指向新 `proxy_binding_id`、运行中全订阅不可用会写 `admin_notification_status/detail` 并走审计
- decision: 代码发布与生产健康通过；运行中代理失败后的授权槽位 failover 数据闭环已具备代码路径和本地测试证据
- next_agent: product
- unresolved: 本次 push deploy 中 `Configure Clash proxies and Zhengzhou smoke task` 仍是 skipped；真实生产 Clash 订阅拉取、真实出口 IP 观测、真实运行中 failover 触发和郑州 3 账号真实搜索点击 / 加入测试仍需单独生产执行证据

## 2026-07-05 搜索目标群点击任务耗尽停止线上复核

- message_id: 2026-07-05-search-join-exhausted-stop-release-001
- action: 修复并发布搜索目标群点击任务在搜索机器人提前没有“下一页”时仍不标记耗尽、持续重试的问题。
- input: 生产郑州 3 账号任务 `1ff45f3a-31b8-468f-af8d-cac4de72fdbf` 使用精确关键词 `xiaozisk`，目标 payload 为 `target_username=xiaozisk`、`target_peer_id=-1002188784621`；真实执行返回 `target_not_in_results`、`total_results=53`、`page=2`、`max_pages=70`、`pages_exhausted=false`，说明搜索机器人结果提前没有下一页时未触发自动停止。
- output: `backend/app/integrations/telegram/search_join.py` 在 `next_button is None` 或到达 `max_pages` 时统一写 `pages_exhausted=true`；PRD、专项 PRD 和数据流转索引明确“找满 70 页或提前没有下一页仍未命中”都必须自动停止；回归测试覆盖无下一页不加入、不假装点击且返回耗尽。
- evidence: 本地定向 `backend/.venv/bin/python -m pytest backend/tests/test_search_join_group_gateway.py backend/tests/test_search_join_group_linked_tasks.py -q` -> 24 passed；全量 no_postgres 60s gate -> `769 passed, 787 deselected, 5 warnings`；`py_compile` 和 `git diff --check` 通过。Commit `647df09d2e78b2a8c6fa00580c6ce93777ab249f` 已推送 `release` 和 `master`；Deploy Production run `28745691543` 通过 checks、build-images、deploy；生产 backend / workers 均运行 `ghcr.io/chenxi1280/tg-yunying-backend:647df09d2e78b2a8c6fa00580c6ce93777ab249f`，公网 `/api/health` 返回 `{"status":"ok"}`，`/task-center` 和 `/` 均 HTTP 200。部署后同一任务再次执行，action `de0a0ced-b358-42da-a862-d6117e18e76e` 于 `2026-07-05 23:40:50+08:00` 返回 `target_not_in_results`、`page=2`、`max_pages=70`、`pages_exhausted=true`、`pre_join_decoy_clicks=[]`；任务状态变为 `stopped`，另外两个 action 自动 `skipped`，错误码 `search_join_target_not_found_task_stopped`。
- decision: 生产已证明搜索目标群找不到时 fail-closed 自动停止，且没有把非目标结果伪造成点击 / 加入成功；本证据不是成功入群证据。
- next_agent: product
- unresolved: 目标群 `xiaozisk` 当前未出现在搜索机器人返回结果中，因此郑州线上测试没有产生 `membership_observed`；如目标必须通过搜索链路成功点击 / 入群，需要先让目标群出现在选定搜索机器人结果中或更换可被搜索机器人返回的目标。

## 2026-07-06 AI 活群 hard-hourly 账号轮转线上复核

- message_id: 2026-07-06-ai-group-hard-hourly-account-rotation-prodverify-001
- action: 修复并发布 AI 活群 hard-hourly 补量时账号被历史记忆固定、表现为单账号连续发言的问题。
- input: 用户反馈线上 AI 活群好像都是一个账号在发送消息；本地修复已完成，用户要求线上检查测试直接使用 SSH，不使用 Actions 长诊断。
- output: `production_verified_ssh_account_distribution`
- evidence: Commit `a9b8a2a2` 已推送 `release`，Deploy Production push run `28791708035` 通过 checks、build-images、deploy；生产直连 `root@47.251.126.134`，`/data/tgyunying/current -> /data/tgyunying/releases/20260706124427_a9b8a2a`，`tgyunying-backend` healthy；公网 `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，`/task-center` 返回 HTTP 200。发布后按 `created_at >= 2026-07-06T20:44:43+08:00` 查询初始无新 action；发布前生成但排在发布后的集中 pending action 已由 dispatcher 标记为 `skipped`，未继续发送。生产 SSH 直连执行 `drain_task_planner(SessionLocal, limit=20)` 后，新代码生成 74 条 AI 活群 action；截至复核时 11 条真实 `success`，覆盖 10 个账号，发送账号序列 `[269, 282, 301, 285, 310, 286, 211, 280, 324, 265, 310]`，`sent_max_same_account_run=1`，样本均有 `telegram_msg_id`，证明真实发送侧不再由单账号连续刷屏。
- supervisor: Locke 只读诊断确认 hard-hourly 旧逻辑存在账号记忆压制轮转风险；James 代码复核确认修复方向无阻塞，并指出状态板路由问题，已修正。
- decision: 本次 bug 的代码发布、线上容器版本、公网健康、新规划分布和真实发送分布均已验证；生产 AI 活群“单账号连续发送”的新代码路径已恢复。郑州大学任务仍因 `account_offline_count=22` 阻断补量，这是账号在线健康问题，不归入本次轮转修复失败。
- next_agent: product
- unresolved: 按用户要求取消 workflow_dispatch 长诊断 run `28792149725`，本次不使用 Actions `AI_GROUP_QUALITY_DONE` 作为证据；仍需后续单独处理账号在线健康和历史 hard-hourly backfill debt。

## 2026-07-06 MiniMax 默认模型切换到 M2.5 线上复核

- message_id: 2026-07-06-minimax-default-m25-prod-config-001
- action: 按用户要求将线上租户默认 MiniMax 文本模型优先改为 `MiniMax-M2.5`。
- input: “阿哥日记”频道评论任务 `b2fa5cb5-5878-4b29-ba5c-6bc61f92d59e` 此前默认走 `MiniMax-M3`，provider 返回 `HTTP 422 input new_sensitive (1026)`；用户补充“线上的优先模型使用 M2.5”。
- output: `production_config_updated_task_recovery_unproven`
- evidence: 生产 SSH 直连 `root@47.251.126.134`，在 `tgyunying-backend` 容器内只读确认租户 1 默认 provider id=4、`provider_name=MiniMax`、`model_name=MiniMax-M3`、`health_status=健康`、`fallback_to_mock=false`。随后使用 provider 已保存的加密密钥解密到进程内存，构造 `MiniMax-M2.5` 凭据调用真实 `AiGateway.check`，返回 `provider ready; chat capability ready` 后提交配置。独立复查确认 provider id=4 已为 `model_name=MiniMax-M2.5`、`health_status=健康`、`last_error=''`、`last_check_at=2026-07-06T22:19:49.540863`，租户 1 仍指向 default_provider_id=4 且 `ai_enabled=true`、`fallback_to_mock=false`。
- decision: 线上默认模型切换已完成且经过 provider check；未打印或落盘 API key。
- unresolved: 同一任务当前仍 `running`、`action_count=0`，`last_error=AI 评论候选不足，已跳过本轮`；因此只能证明默认模型配置已切换，不能声明“阿哥日记”评论/回复已经真实恢复。按用户最新口径，本地不再发布敏感上下文净化或 M3->M2.7->M2.5 自动降级代码。

## 2026-07-07 频道 AI 评论过程性内容线上排查（当前环境 blocked）

- message_id: 2026-07-07-channel-comment-ai-meta-filter-prod-diagnosis-001
- action: 按用户要求尝试 SSH 到生产环境检查 AI 评论异常。
- input: Telegram 截图显示频道评论发出了 `<think>`、`让我分析这个频道内容`、`让我仔细分析这个请求` 等过程性内容；用户要求“你也线上ssh 链接到线上检查一下问题”。
- output: `production_ssh_blocked_local_fix_ready`
- evidence: 本地 SSH alias `silicon-valley-production-server` 解析到 Clash fake-ip `198.18.0.153` 后连接关闭；`ssh codex_usa01_server` / `ssh codex_kl_server` 到 `47.251.126.134:22`、`47.250.167.174:22` 均在 banner exchange 前超时；当前环境 `curl https://tgyunying.telema.cn/api/health` 和 `/task-center` 均 10 秒超时。GitHub Actions 可访问，最近生产 workflow：`28808100947` workflow_dispatch success（2026-07-06T16:48:32Z，head `e2314453`），`28802430977` release push success（2026-07-06T15:19:02Z，head `92e36126`）。这些只能证明最近发布链路记录，不能证明当前任务恢复。
- decision: 当前线程无法取得新的生产容器 / DB / worker E4 证据，不能写 `production_fixed`；本地根因已按 `2026-07-07-channel-comment-ai-meta-filter-devcomplete-001` 修复并等待 QA / release。
- unresolved: 需要在可达生产网络或 GitHub Actions SSH 环境中复核最近 `channel_comment` action 的 payload / result / worker 日志，并在发布后确认过程性内容被 `content_policy` 拦截或不再生成。

## 2026-07-07 频道 AI 评论过程性内容发布后运行时复核

- message_id: 2026-07-07-channel-comment-ai-meta-filter-release-prodverify-handoff-001
- action: 接收 product 发布恢复 handoff，记录当前可得生产运行时证据和剩余任务级取证缺口。
- input: 用户要求“拉到线上验证”；本地修复 commit `71dd41cdd11d1768154b7603e7d0360f0b18eb52` 已推送 `master` / `release`。
- output: `released_prod_runtime_ok_task_sample_unproven`
- evidence: Deploy Production run `28836948792` attempt 3 success；deploy job `85548441254` 在 `2026-07-07T06:10:23Z` 到 `2026-07-07T06:13:51Z` 执行 `Deploy via SSH release script` 成功。release `20260707061024_71dd41c` 安装到 `/data/tgyunying/releases/20260707061024_71dd41c`；后端和 worker 镜像均为 `ghcr.io/chenxi1280/tg-yunying-backend:71dd41cdd11d1768154b7603e7d0360f0b18eb52`；backend 与 planner、dispatcher-1/2/3/4、listener、recovery、account-security、account-online、ai-memory、metrics workers 均 healthy。发布脚本确认 local api health、host nginx api health、public frontend、public api health 均 HTTP 200；本地复查公网 `/api/health` 返回 `{"status":"ok"}`，`/task-center` 返回 HTTP 200。
- limitation: 本机 SSH 到 `47.251.126.134` 当前为 `Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password)`，未取得发布后生产 DB / worker / Telegram action 样本。
- decision: 发布和生产运行时健康已通过；任务级恢复仍 unproven，不能写 `production_fixed`。
- unresolved: 需要补发布后 `channel_comment` 真实样本：确认新生成评论不含 `<think>` / “让我分析...” 等 AI 过程性内容，或旧 pending 脏 `comment_text` 在 `reply_channel_message` 前以 `content_policy` / `拦截 AI 过程性内容` 失败且未进入 Telegram gateway。

## 2026-07-07 频道 AI 评论过程性内容二次线上防线复核

- message_id: 2026-07-07-channel-comment-ai-meta-filter-a0f1a8e-prodverify-001
- action: 按用户再次要求“拉到线上验证”，使用可用生产 SSH key 直连生产并完成只读 DB / 容器 / 过滤器核查。
- input: 首轮修复 `71dd41c` 已发布；需要确认真实线上 `channel_comment` 是否仍可能发送 AI 过程性内容。
- output: `production_guard_verified_post_comment_send_absent`
- evidence: 当前生产曾先运行 `a8c684fa`，且 `71dd41c` 是其祖先；生产 DB 发布后 `channel_comment` 共 24 条，均为 `ensure_target_membership`，最近 50 条 payload/result 的 `<think>`、`让我分析`、`让我仔细分析` 等命中数为 0，发布后 `post_comment` 样本为 0。只读检查旧 pending `post_comment` 发现 `d04d35d3...` 为 `让我分析一下上下文`，`491aca40...` 为 `这是一段明显带有色情性质的内容 描述了性行为的详细过程`；首轮过滤会拦截前者但放行后者。新增红测后修复并发布 commit `a0f1a8e3aea6b59c8865efa52d244ce81b62f4ba`，Deploy Production run `28850140650` 的 checks、build-images、deploy 全部 success；生产 `/data/tgyunying/current -> /data/tgyunying/releases/20260707075044_a0f1a8e`，backend 和所有 worker 镜像均为 `ghcr.io/chenxi1280/tg-yunying-backend:a0f1a8e3aea6b59c8865efa52d244ce81b62f4ba` 且 healthy；公网 `/api/health` 返回 `{"status":"ok"}`，`/task-center` HTTP 200。生产容器内验证 `<think>`、`让我分析这个频道内容`、`这是一段明显带有色情性质的内容...`、`这是一个明显的色情内容频道` 均 `looks_like_ai_meta_content=True`；实际旧 pending `d04d35d3...` 和 `491aca40...` 当前 `filter_outbound_content` 均返回 `False / 拦截 AI 过程性内容`，普通 pending `5781a0f2...` 返回 allowed。
- decision: 生产防线已验证：当前线上代码会在 `reply_channel_message` 前拦截已知 AI 过程性 / 审查口吻旧 pending 文本，避免进入 Telegram gateway。
- unresolved: 发布后没有真实 `post_comment` 成功发送样本，因此不能证明“已经发出一条干净评论”；本条不写 closed。

## 2026-07-07 频道 AI 评论重描述重试和表情兜底线上复核

- message_id: 2026-07-07-channel-comment-redescription-emoji-fallback-prodverify-001
- action: 按用户最新口径“只重试3次，如果还是失败，失败的部分发送表情来兜底”，完成频道评论 / 引用回复 AI 生成链路发布和生产复核。
- input: MiniMax-M2.5 对成人语境频道评论可能返回拒绝、审核口吻或候选不足；用户要求拒绝审核时换描述重新输出，最多 3 次，仍失败的缺口使用表情兜底。
- output: `release_gate_passed_prod_behavior_probe_ok_task_send_unproven`
- evidence: 本地定向 `backend/tests/test_ai_gateway.py::test_channel_comment_retries_review_tone_then_fills_missing_with_emojis`、`test_channel_reply_comment_retries_then_fills_missing_with_emojis`、`backend/tests/test_operations_center_runtime.py::test_channel_comment_pre_send_validation_blocks_ai_meta_text` 为 `3 passed`；`backend/.venv/bin/python -m pytest backend/tests/test_ai_gateway.py -m no_postgres -q` 为 `24 passed, 30 deselected`；`compileall` 和 `git diff --check` 通过。Commit `1a915d9aea14b3bdd1494187b581f93db25298d0` 已推送 `master` / `release`；Deploy Production run `28854291743` 的 checks、build-images、deploy 全部 success；生产 `/data/tgyunying/current -> /data/tgyunying/releases/20260707090543_1a915d9`，backend 和所有 worker 镜像均为 `ghcr.io/chenxi1280/tg-yunying-backend:1a915d9aea14b3bdd1494187b581f93db25298d0` 且 healthy。线上 Nginx 入口 `https://tgyunying.telema.cn/task-center` 经本机 `--resolve` 返回 HTTP 200，`/api/health` 返回 `{"status":"ok"}`。生产容器内只读确认 `CHANNEL_COMMENT_MAX_REDESCRIPTION_ATTEMPTS=3`，表情池为 `('👀', '🙂', '👍', '👌', '🙌', '🤔', '😅', '🔥')`，重试 prompt 包含“换一种描述方式”；租户 1 默认 provider 仍为 `MiniMax` / `MiniMax-M2.5` 且 `health_status=健康`。生产容器内纯函数探针模拟前三次均返回审核口吻、第四次返回 1 条正常评论，结果为 `calls=4`、`has_retry_prompt=True`、第二条缺口由表情 `🙌` 补齐。
- decision: 代码发布、生产健康和生产镜像内行为探针均通过；频道评论 / 引用回复已具备最多 3 次重描述重试，缺口表情兜底，不再用审核说明或模板句兜底。
- unresolved: 本次没有创建真实 Telegram 评论发送动作；因此只证明生产代码路径和探针行为，不声明已发出新的真实评论样本。

## 2026-07-11 生产核心页面有界加载 E4 验收

- message_id: `2026-07-11-production-page-performance-prodverify-001`
- input: `2026-07-11-production-page-performance-product-accepted-001`，用户要求继续完成线上慢页修复。
- output: `production_fixed_page_performance_log_correlation_blocked`
- release: Commit `357c844d951f90659c077d91e002e9a1e7430ee2` 已同步 `master/release`；Deploy Production run `29110463190` success，release `20260710172417_357c844` 已上线，backend/frontend 镜像 SHA 一致，backend 与全部 worker healthy，公网 `/api/health` HTTP 200。
- serial evidence: 两个列表各 30 次真实登录态刷新全部 HTTP 200。任务列表 `/api/tasks/page?page=1&page_size=20` p95/p99 `446/451ms`、最大 7.1KB；运营目标 `/api/operation-targets?page=1&page_size=20` p95/p99 `339/346ms`、最大 1.85KB；零 408/499/502。
- concurrent evidence: 10 路同时打开任务中心全部成功，任务接口最慢 1.699 秒、最大 7.0KB；10 路运营目标全部成功，接口最慢 830ms、最大 1.85KB；所有页面均无失败提示。
- interaction evidence: 任务中心首屏 1.224 秒，运营目标首屏 1.472 秒；任务详情 1.700 秒；编辑弹窗 427ms 可操作，目标 ids 水合 323ms / 719B，目标候选查询 247ms / 3.9KB 且带 `page_size=50&capability=task`。
- related pages: 消息发送、规则、归档分别 1.328/2.721/0.710 秒可用；归档弹窗 308ms，目标请求带 `page_size=50&capability=archive`，302ms / 3.9KB；生产 console error 为 0。
- pass: 当前生产慢页、任务编辑超时和连续刷新 502 症状已恢复，满足 p95 < 2 秒、p99 < 5 秒、单页 < 100KB 和零 408/499/502 的 E4 口径，`production_fixed=confirmed`。
- blocked: 本机 SSH 只读日志复核被远端关闭连接，未取得同窗口 nginx/backend 请求日志；服务器侧发布、容器和健康证据来自成功部署工作流。
- unproven: 历史 `/api/tasks` 间歇 502 的唯一直接 upstream 原因；不把发布后的恢复结果倒推成旧故障的唯一底层机制。
