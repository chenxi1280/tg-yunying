# 生产发布批次冻结与单次部署合同

## 1. Intake Card

| 字段 | 内容 |
| --- | --- |
| 原始问题 | 线上 GitHub Actions 在代码尚未收敛时反复执行完整生产部署；用户要求合并成一次完整代码、一次部署 |
| 分级 | L2 / P1 发布编排缺陷；会造成无业务必要的生产滚动与发布证据混淆 |
| 目标 | 开发提交先在 `master` 汇总；本批完成后只快进一次 `release`，再从冻结的 `release` 候选显式执行一次 `Deploy Production` |
| 非目标 | 不减少后端分片、PostgreSQL、前端构建、镜像或生产验证；不改变 Task/Action/Telegram 履约合同；不执行生产数据修复 |
| owner flow | `product -> dev -> qa -> product -> release -> prod-diagnosis` |

## 2. 根因与产品决策

旧 workflow 同时监听 `push release` 和 `workflow_dispatch`。当一个修复批次连续补入多个提交时，每次更新 `release` 都会立即启动完整 CI、镜像构建和生产部署；后续提交无法并入已经启动的不可变 run，因此形成多次排队或连续部署。

本合同取消 `release` push 的自动生产触发。`release` 只表示已经冻结、等待发布或已经发布的候选；`Deploy Production` 只接受人工从 `release` ref 发起的 `workflow_dispatch`。这不是跳过质量门，而是把质量门从“每次补丁自动运行”移动到“批次候选冻结后运行一次”。

## 3. 发布状态与数据流

```text
开发提交 -> master 汇总
               |
               | 本批范围、测试与文档完成
               v
master HEAD == release HEAD  <--- 唯一一次快进 release
               |
               | workflow_dispatch(ref=release)
               v
candidate guard -> 后端 3+2 分片 + frontend -> 三镜像 -> deploy -> runtime readback
```

### 3.1 冻结候选

1. 日常补丁只进入 `master`，不能用多次 push `release` 充当 CI 重跑按钮。
2. 批次完成后，发布人只把 `release` 快进到本批最终 `master` HEAD 一次。
3. 触发时 `github.sha` 是不可变候选 SHA。workflow 必须在任何昂贵测试、构建或部署前确认：
   - event 为 `workflow_dispatch`；
   - ref 精确为 `refs/heads/release`；
   - checkout HEAD 等于远端 `release` HEAD；
   - 同一 HEAD 也等于远端 `master` HEAD。
4. 任一不一致都在 candidate guard 显式失败，不能进入测试、镜像或生产 environment。

### 3.2 单次完整部署

1. candidate guard 通过后，完整执行既有后端 no-postgres 3 shard、PostgreSQL 2 shard、frontend build、三镜像构建和生产部署。
2. 所有测试 job 必须依赖 candidate guard；`build-images` 与 `deploy` 继续保持原有依赖，不允许 `continue-on-error`、skip 或减少分片。
3. 同一批次只保留一个 candidate SHA 和一个正式发布 run。CI/构建在 deploy 前失败时，可以对同一冻结 SHA 使用 GitHub rerun；不能追加无关提交后仍称为同一候选。
4. deploy 已进入 live 阶段后，不得为了补拿业务证据重复部署同一 SHA；使用独立只读生产监控/诊断 workflow 读取 runtime 与 typed E4。

## 4. 并发、幂等与失败边界

- `deploy-production` concurrency group 保持串行；默认新手工 run 不取消正在执行的正式 run。
- 只有操作者显式选择 `force_cancel_in_progress` 才能取消卡死的手工 run；取消前仍需确认旧 run 尚未进入不可逆 release live 边界。
- candidate guard 读取 remote refs 后，后续所有步骤继续使用 dispatch 时冻结的 `github.sha`，不能重新解释成更新后的分支 HEAD。
- guard 失败、测试失败、镜像失败和 deploy 失败是四类不同证据；不得用后一次成功覆盖前一次失败原因。
- 发布成功只证明代码/runtime；Task 履约仍必须沿 `Task -> ledger/coverage -> Action -> ExecutionAttempt/Gateway -> typed remote fact` 独立验收。

## 5. 前端、后端、API 与数据

- 前端：无交互和状态变化。
- 后端/API/worker：无业务代码、接口、schema、migration 或任务运行变化。
- 数据：不读取或修改生产业务表；candidate guard 仅比较 Git commit SHA。
- 权限：生产 secrets 仍只在 `deploy` job 的 `production-silicon-valley` environment 中使用；candidate guard 不接触 secrets。

## 6. QA 与 Release Gate

### 6.1 自动化合同

- YAML 顶层 trigger 只有 `workflow_dispatch`，不存在 `push`。
- 存在 `validate-release-candidate` job，包含 event/ref/master/release/checkout SHA 四项 fail-closed 校验。
- 三类测试 job 均直接依赖 candidate guard。
- build 与 deploy 的既有完整依赖集合不减少。
- workflow_dispatch 输入总数继续不超过 GitHub 上限。

### 6.2 发布验收

1. `master` 和 `release` 只在最终候选时对齐一次。
2. release push 本身不产生 `Deploy Production` run。
3. 从 `release` ref 手工触发后，candidate guard 和完整 CI 全绿。
4. Actions run 的 `headSha`、生产 current symlink、backend/frontend/worker runtime SHA 一致。
5. 公网 health 独立通过；业务 E4 未读取时明确写 `unproven`。

## 7. 回滚

- workflow 变更尚未 deploy 时，可 revert 本合同及 workflow commit；不涉及生产数据回滚。
- 新候选 deploy 失败时仍按上一不可变 production release 回滚，不移动历史事实、不在线上补代码。
- 如确需恢复 release push 自动部署，必须先 product resync 并说明为什么批次冻结失效，不能临时绕过 candidate guard。

## 8. Product Design Complete

- [x] 覆盖用户原话：先合并完整代码，再一次部署。
- [x] 定义触发、候选 SHA、依赖、并发与失败状态。
- [x] 前端、后端、API、worker、数据和权限均明确为无业务变化。
- [x] 覆盖重跑、live 后诊断、回滚和 typed E4 边界。
- [x] QA 与生产验收可由确定性 workflow 合同和 SHA readback 验证。

`design_status=product_design_complete`，`dev_handoff_ready=true`。
