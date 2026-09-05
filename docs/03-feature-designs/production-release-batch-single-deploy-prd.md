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

## 9. 正常发布移除全局 Docker 清理

2026-09-05 Deploy33963567986在共享后端镜像拉取阶段两次TLS握手超时，尚未进入worker fence。第二轮从19:48:15的Docker磁盘统计到20:06:47的实际pull，相隔18分32秒；正常发布执行的全局system df、container prune、builder prune与image prune形成额外阻塞。20:33只读主机事实为磁盘仍余18.34GB、Docker本地API超时、daemon有132440kB换出页，不能将本次错误当作磁盘已满，也不能将清理完成当作性能恢复。

正常compose-up直接执行既定的精确镜像pull，不运行全局Docker磁盘统计或自动prune。CI、镜像校验、pull错误退出、迁移、fence和全部运行验收保持原顺序与要求。若实际出现磁盘不足，原错误明确暴露，再按精确对象preview/授权apply/readback处理；不自动选择其他项目容器、构建缓存或镜像进行清理。本修订只删除已证明拖慢发布的前置工作，不增加重试、超时放宽或成功降级，也不声称已修复主机内存压力。

## 10. 已开始的远端安装不得按连接重试重放

2026-09-06的Deploy33978775792已经安装14d09414、完成迁移并激活调度，随后Antigravity双模型真实探测返回`unknown/antigravity_quota_limited`。外层SSH重试器把该业务失败当成连接失败，完整安装共执行三次，两次额外停止全部worker并重新收口在途动作；这不是网络恢复，也没有修复模型额度。

远端安装命令每次发布只派发一次。其非零退出（包括业务失败、SSH断开255和本地等待超时124）直接保留原错误并结束当前发布，不自动重放已可能生效的安装；后续先读取原run、current/容器/调度与失败依赖的实际状态，再按对应阶段修复。只读SSH连通性检查和尚未执行安装的归档/镜像配置上传仍保留现有重试。不得通过跳过模型探测、吞掉quota/unknown或回滚业务事实来得到绿色状态。

本修订design_status=complete。QA须执行真实release.sh与隔离的SSH/SCP边界替身，验证业务失败1、SSH未知255、等待超时124均只派发一次安装、保留退出码且无完成输出；连接前置失败仍可按原次数重试。该变更只修复发布重复执行，不声称解决外部模型额度不足或本轮四类E4。

本发布性能子项design_status=complete；QA验证bash语法、拉取先于fence、拉取失败明确退出、既有SSH编排回归和完整Release Gate。运行恢复必须另核Docker响应、实际SHA、容器健康及业务E4。

## 11. 未变更的独立Provider运行时保持原进程

02:04实机比对证明Antigravity当前73388cd1的四个独立runtime文件与本候选逐字节一致，均root:root/0644；服务active，认证只读health可达，CLI正常，但明确degraded/quota_limited。应用已将新生成切换到用户选择的M3，原Gemini未知请求仍由原bridge保存。每次应用发布重装相同bridge、重启进程并重新调用两模型，增加无必要的中断和请求，不会恢复外部周额度。

发布先按同一份runtime文件清单比较候选与当前独立运行目录。只有四文件内容一致、原保护的owner/mode/非软链接文件条件成立且所有enabled slot均active，才保留原runtime symlink、原进程、原ledger和请求；以认证GET读取每个slot的health，核对slot身份、bridge版本、CLI二进制及版本可用性，并原样报告provider status、quota和模型可见性。输出明确区分application candidate SHA与retained runtime SHA。该路径不调用generate、不创建模型探针、不重置模型健康，也不能输出双模型已通过。

如果runtime内容变化、没有当前运行目录或有enabled但未active的slot，继续原完整安装、restart及双模型真实探测；探测失败仍非零退出并按原方式回滚slot。相同runtime的认证GET失败或运行结构不可用直接使发布检查失败，不转成静默安装或假健康。disabled/active漂移、slot操作锁、先pull后fence、迁移和应用health检查保持原规则。

本修订design_status=complete，仅改变独立组件没有代码变化时的部署动作，不把既有外部quota故障宣称为修复，不覆盖四类业务E4。QA须执行真实shell编排验证相同文件且active时安装/重启/POST均为0、原symlink与进程状态保留、degraded/quota被明确输出；变更文件与inactive仍走完整安装/模型探针，读回失败不能转入安装，原失败回滚和锁竞争继续回归。
