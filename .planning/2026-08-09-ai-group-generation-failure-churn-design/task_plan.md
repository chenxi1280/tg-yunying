# Task Plan: AI 活群失败风暴整体修复设计

## Goal

形成 Product Design Complete 的整体修复方案，消除签到与普通正文重复失败的无效重建，保留质量门和真实远端事实合同，并给出可发布、可回滚、可 E4 验收的开发交接。

## Current Phase

Historical design pass superseded; current recheck由`../2026-08-09-ai-group-failure-repair-readiness-gap/task_plan.md`持有

> 2026-08-10：后续实现就绪审查发现新的P0/P1并加入频道浏览范围；本文件的Complete只表示第一版设计当时结束，不能作为current Product Design Complete证据。

## Phases

### Phase 1: 需求与现状收口

- [x] 核对当前产品合同、数据流和线上根因
- [x] 明确原始需求、影响范围与非目标
- [x] 记录发现到 findings.md
- **Status:** complete

### Phase 2: 产品与状态机设计

- [x] 设计 check-in 作用域、容量缺口和终态语义
- [x] 设计 extra-volume variation 与质量重规划语义
- [x] 设计账号面具资产、UI 与观测口径
- [x] 闭合并发、幂等、未知结果和跨任务边界
- **Status:** complete

### Phase 3: 开发与数据交接设计

- [x] 映射代码、模型、迁移和测试改动
- [x] 设计存量失败风暴的安全接管方式
- [x] 定义部署顺序、回滚和数据兼容
- **Status:** complete

### Phase 4: 文档与独立验收

- [x] 更新专项 PRD 与必要索引
- [x] 完成 Product Design Complete 自检
- [x] 独立复核 P0/P1、并发、回滚和 E4 口径
- **Status:** complete

### Phase 5: 交付

- [x] 检查文档 diff 与链接
- [x] 输出方案摘要、范围和未验证项
- **Status:** complete

## Success Criteria

1. 相同 `(task, group, account, task-day)` 的 check-in 最多一个 open/Gateway/unknown/confirmed；不同 Task/群不被普通正文 10 天去重误伤。
2. check-in 已用尽或不合法时形成可审计的 `content_capacity_gap`，不得回到立即可领取状态或创建无限失败 Action。
3. coverage 与 extra-volume 的每次质量重规划都有稳定义务身份和递增的新 variation；同一失败 variation 不重放。
4. 保留所有内容质量、账号、准入、Gateway unknown 和远端事实门禁，不以降质、跳过去重或伪成功换吞吐。
5. 方案覆盖存量接管、并发幂等、API/UI、监控、测试、发布、回滚和生产 E4。

## Decisions Made

| Decision | Rationale |
|---|---|
| 基于 `origin/master@9a1405aa` 的独立工作树设计 | 避免覆盖主 release 工作树和其他任务的脏改动 |
| 第一版阶段只更新设计文档 | 这是当时边界；用户后续已明确授权实施、发布和线上验证，current计划不再受此限制 |
| 不用重试次数上限掩盖问题 | 当前根因是错误作用域和缺失 variation 身份，应由业务状态机闭合 |
| 保留现有自然日 pacing 与最多 20 条有界批次 | 本次修复失败风暴，不重新设计已完成的 Planner 批次合同 |
| canonical `AiGroupMessageObligation` + 通用 FOP/GenerationJob | coverage/extra-volume 都有稳定 due 身份，Action 仅作尝试 |
| 新增 immutable content intent，不恢复 legacy ContentMix 真相源 | 保住 reply/material/act-type 合同且避免双账本 |
| waiting 使用 normalized event/time subscription | blocker basis 不变时不热循环，事实变化后可可靠唤醒 |
| 存量 remote facts 用 immutable alias 接管 | 不改写历史事实或 mutation identity，同时建立新读模型 |
| current Task 原位 manifest + route epoch 切换 | 不重置 4800/confirmed，并防止新旧 writer 双发 |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `init-session.sh` 无执行权限 | 1 | 改为显式 `bash init-session.sh`，成功创建作用域规划文件 |
| 专项 PRD 多段补丁末尾序号上下文不匹配 | 1 | 用 `rg -n` 定位真实锚点，拆成精确小补丁重试 |
| 独立 Agent 等待参数低于工具最小 10 秒 | 1 | 改用 10 秒有界等待；不影响设计产物 |
