# 2026-08-14 OCR Worker 重启风暴与 ECS OOM 修复

## Intake / Triage

- `intake_id`: `intake-2026-08-14-ocr-worker-oom-001`
- `incident_id`: `incident-2026-08-14-ocr-restart-oom-001`
- `raw_input`: 修复线上 ECS 服务集体失联、OCR worker 重启风暴与主机 OOM。
- `level`: `L3`
- `production_related`: `true`
- `current_deployed_sha_before_change`: `ab1418cb6c1c6d70dca82c4cc4239fde48de45fa`
- `workflow`: `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`

## Incident Report

- `symptom`: 同一 ECS 上服务集体超时，SSH TCP/22 可连接但 banner 间歇超时。
- `first_broken_boundary`: image-verification worker 的 native OCR deadline generation 收口。
- `confirmed_trigger`: 任一 OCR future 未在 25 秒 budget 内收口时，worker 抛出 `verification_local_ocr_timeout` 并以退出码 70 终止；Docker 立即重启并重新冷加载 OCR 模型。
- `confirmed_amplifier`: 7.3 GiB、无 swap 的 ECS 同时承载应用 worker 与 61+ Mihomo；重复冷启动和业务 worker 压力触发 global OOM。
- `confirmed_impact`: kernel 于 09:10 触发 global OOM 并杀死约 913 MiB RSS 的 planner Python 进程；Docker 健康检查成批超时。
- `live_reproduction`: OCR restart count 在只读观察中从 189 增至 191；对应 Docker `exitCode=70`，一分钟 load 达 620。
- `unproven`: planner 是否存在独立内存泄漏；Telegram 业务是否已恢复。

## Product Design Complete

- `design_status`: `complete_existing_contract`
- `truth_source`: `docs/03-feature-designs/dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md`
- `required_behavior`:
  - 一路 OCR 完成、另一路超时时，保留完整 source，超时 source 显式失败，generation 进入 draining。
  - draining generation 拒绝新请求，并在当前响应收口后由既有 Docker restart policy 重建。
  - 两路均超时继续 fail closed，以退出码 70 终止 unknown native generation。
  - 生产启动预热两套 OCR 引擎，健康检查必须使用带内部 token 的 functional `/ready`。
  - 不新增持久队列、HA、native fallback、Telegram 自动重试或验证码放宽。
- `frontend_impact`: none
- `database_migration`: none
- `security`: 沿用内部 token；日志和测试不输出 token、图片或账号信息。
- `rollback`: 上一不可变 release 可作为应用回滚锚点；本变更无迁移和数据写入。

## Development Handoff

- `locked_paths`:
  - `backend/app/image_verification_worker.py`
  - `backend/app/image_verification_worker_app.py`
  - `backend/tests/test_image_verification_worker.py`
  - `backend/tests/test_membership_challenges_image_solver_runtime.py`
  - `docker-compose.image-verification.yml`
  - `Dockerfile.image-verification-worker`
  - 本事故对应 PRD、结构索引、运行合同和本运行记录
- `implementation_status`: `development_complete`
- `candidate_branch`: `codex/ocr-worker-oom-fix-20260814`
- `base_sha`: `7f2c2ab712290aa637075d073735a085e1f0bbd4`
- `compatibility_fix`: 历史补丁使用的 `FastAPI.add_event_handler` 与当前 FastAPI 0.136.1 不兼容；候选改用仓库现行 lifespan 启动钩子，仍在接收请求前完成双引擎预热。

## QA Acceptance

- partial timeout 返回 completed source + explicit timeout source，且不触发 abnormal termination。
- partial timeout 后 health/readiness 暴露 draining，并拒绝新请求。
- dual timeout 仍触发 fail-closed abnormal termination。
- production app startup 预热引擎；Compose healthcheck 使用 authenticated `/ready`。
- image worker/client/solver 定向测试、Compose 解析、compile、`git diff --check` 全部通过。

## QA Result

- `status`: `qa_pass`
- `focused_tests`: `82 passed, 5 pre-existing warnings`
- `static`: `py_compile pass / git diff --check pass`
- `compose`: merged server + dispatcher + image-verification config pass
- `functional_readiness`: 本地真实 lifespan 初始化 RapidOCR/ddddOCR，认证 `/ready` 返回 200 和两套 engine。
- `local_image_build`: Debian 仓库下载 `mesa-libgallium` 返回 HTTP 502，失败发生在 COPY/项目依赖安装之前；不得改 Dockerfile 绕过，Actions 镜像构建仍是强制发布门禁。

## Product Acceptance

- `status`: `product_accepted_candidate`
- `acceptance`: 实现覆盖既有 partial timeout/draining/functional readiness 合同，没有新增队列、fallback、业务重试或数据迁移。
- `production_boundary`: 尚未部署，不得写 `production_fixed`。

## Release Gate

- `release_mode`: `github_actions`
- `release_path`: `master -> release -> Deploy Production`
- `status`: `workflow_resync_required`
- `worker_impact`: 替换单实例 OCR worker；Dispatcher 由依赖健康条件等待 functional readiness。
- `migration_impact`: none
- `external_platform_impact`: no direct Telegram mutation during deploy verification
- `observe_window`: restart count、exit code 70、load/memory、容器健康和公网健康；业务 E4 单独验证。
- `2026-08-14_resync`: 候选 `314c6d9d` 的 Deploy Production run `31809489025` 已执行到 `deploy/release.sh` live release，但 workflow 后续重复运行全量 `scripts.takeover_all_task_fulfillment` 并 300 秒超时。全量 takeover 已由 `deploy/compose-up.sh` Stage B 的零业务 writer 窗口执行，workflow post-deploy 只能做有界只读 gate；本次修复同步 PRD、运行合同、索引并移除 workflow 的重复 takeover 步骤。

## Production Verification

- `runtime_status`: `release_gate_failed_after_live_script`
- `business_status`: `unproven`
- `production_fixed`: `false`
