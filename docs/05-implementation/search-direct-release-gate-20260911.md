# 搜索直连与历史代理熔断修复 Release Gate

本次变更基于已运行的 `84c1c1fc` owner 版本。账号恢复原范围 318，已恢复 144；另有初始 3 个恢复账号。剩余 174 未恢复不缩小、不等同永久丢失。P1 发布必须以这些已恢复账号为保全基线。

## 变更与边界

- 新建搜索点击和排名观察显式保存 `sv_current_direct_v1`。当前 SV 授权、同一 owner、固定直连出口、授权代次和业务结果分别取证。
- 排名启动、候选查询、规划、正式执行及 nullable 代理节点统计已适配。账号/组的业务选择和原目标、预留语义保留。
- 搜索缺传输证据且有远端操作迹象时保留未知结果，不释放重放。历史确认不补造新证据。
- 活群覆盖去掉历史代理熔断关联，直连 fence 标记实际故障域；未引入共享出口并发上限。
- 备用授权登录去掉代理选择；历史记录保留。新增展示组件及索引同步。
- 精确 Task 配置迁移提供 preview/CAS/apply/审计及 protected hash 读回，不写已有 Action/Attempt/ledger/assignment。本轮生产只发现一个运行中的搜索点击任务，无排名任务。

## 代码审查与本地证据

已核对规范化结果与远端未知优先级、schema 对 owner metadata 的接受、当前授权及代次校验、rank 预留与 nullable 统计、owner IPC 到协程上下文传播、空代理资源隔离、历史代理熔断不影响其他账号、迁移 CAS 与 unknown 保全。

本地定向 PostgreSQL、owner IPC、前端类型与构建已通过；冻结后使用下面完整选择再执行。每个后端进程硬超时 60 秒，制品内保留逐批日志。前端构建的 chunk size 提示是已知构建提示，构建退出 0。

额外历史对照：旧 `test_search_rank_deboost_executor.py` 仍以允许代理为前提，28 个用例在本次变更前的冻结 `84c1c1fc` 基线与本轮均失败；证据分别为本地 QA 的 `rank-regression-baseline-84.log` 和 `rank-regression-1.log`。不把这些失败计入通过数，也不以重开代理使旧测试变绿。当前合同由 `test_direct_rank_pipeline_postgres.py`、`test_direct_rank_search_transport.py`、`test_direct_search_postgres.py`、Gateway、原 pacing/config、迁移和隔离测试共同验收。三个 API 创建旧代理参数测试已改为当前直连创建并保留历史代理数据的断言；单条旧代理 Gateway 正例改为未调用拒绝反例。

## 冻结制品测试选择

1. `tests/test_direct_search_transport.py tests/test_direct_rank_search_transport.py tests/test_direct_search_owner_context.py tests/test_search_rank_deboost_gateway.py`
2. `tests/test_direct_search_postgres.py tests/test_search_direct_migration_postgres.py`
3. `tests/test_direct_rank_pipeline_postgres.py tests/test_direct_circuit_postgres.py`
4. `tests/test_telegram_owner_fencing_postgres.py tests/test_local_activate_complementary_postgres.py`
5. `tests/test_telegram_owner_client_cache.py tests/test_telegram_owner_rpc.py tests/test_telegram_owner_invocations.py tests/test_telegram_owner_attempts.py tests/test_telegram_owner_release.py`
6. `tests/test_telethon_lifecycle.py tests/test_telegram_gateway_probe_lifecycle.py tests/test_telegram_gateway_login_lifecycle.py tests/test_telethon_disconnect_awaitable.py tests/test_telegram_direct_transport.py`
7. `tests/test_account_authorizations.py tests/test_authorization_credentials.py tests/test_account_authorization_assets_dataflow.py tests/test_account_direct_egress_postgres.py`
8. `tests/test_ai_group_independent_progress.py tests/test_search_rank_deboost_dataflow.py tests/test_search_rank_deboost_runtime_config.py tests/test_search_click_only_contract.py tests/test_search_click_contract_regressions.py`
9. `tests/test_search_click_assignment_solver.py tests/test_search_click_assignment_epoch.py tests/test_search_click_epoch_release_quarantine.py tests/test_search_click_safe_settlement.py`
10. `tests/test_dispatcher_lifecycle.py tests/test_dispatcher_dataflow.py tests/test_telegram_worker_exit_reconcile_postgres.py`
11. `tests/test_release_worker_cutover.py tests/test_worker_cutover_shell.py tests/test_local_release.py tests/test_local_release_cleanup.py`

## 发布及业务 Gate

1. 冻结 master/release 同 SHA；本地 linux/amd64 构建与压缩包 hash/size、实际导入 image ID/platform 全部一致。禁止 Actions/应用仓库发布。
2. 直连 IP 来自当前 owner HTTPS 实测，显式冻结 REGION/IP；保存原 .image.env 的完整授权参数并核对指纹。native readback 对每个 caller 和 owner 核验配置一致。
3. 安装仅一次；若传输需要复用已缓存的 OCI 内容，只允许重建与本地原始 gzip 字节完全一致的包，仍走原完整核验/导入/安装入口。
4. 发布前后逐账号核对当前授权失效变化和 144+3 已恢复账号；核对 53 个旧代理容器仍关闭且不自动重启。
5. 配置迁移前后保留任务目标、确认数、全部既有 Action 哈希，特别保留 closed_unknown 历史身份。再观察新 Action 的当前授权、owner、固定出口及真实 typed click；无实例的排名观察不虚构生产 E4。
6. 成功安装及运行读回后，仅清理上一轮成功发布的精确包和未被任何容器引用的旧镜像。本次制品与失败候选保留。

本文件为发布合同与本地证据索引。最终制品、运行结果、迁移回执、逐账号保全及真实业务结果写入该次不可变发布证据目录，不能用此文件代替完成事实。
