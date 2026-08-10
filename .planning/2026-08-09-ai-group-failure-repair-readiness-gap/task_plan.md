# Task Plan: AI 活群 + 浏览任务整体修复与生产闭环

## Goal

收口 AI 活群稳定义务设计，同时对线上 `channel_view` 浏览任务定位首个断点；从最新 `master` 实施最小根因修复，通过真 PostgreSQL/QA/Release Gate 后走 `master -> release -> Deploy Production`，最终用各自 task-type 的 typed remote fact、投影与结算证明真实修复。

## Current Phase

Phase 6

## Phases

### Phase 1: 基线与需求重新同步

- [x] 核对独立工作树、最新可见 `origin/master` 与已有设计差异
- [x] 读取当前设计、开发交接、迁移、Release Gate 和 E4
- [x] 记录当前生产证据与未验证边界
- **Status:** complete

### Phase 2: 实现就绪审查

- [x] 按模型/状态机/worker/API/UI/迁移/回滚拆解实现依赖
- [x] 核对数据库并发、事件源、Gateway 证据和历史兼容的可实现性
- [x] 识别缺少的 ownership、顺序、入口/退出条件和验收证据
- **Status:** complete

### Phase 3: 设计缺口补齐

- [x] 将当前独立复核的所有 P0/P1 补入AI专项、主PRD、DF与运行交接
- [x] 补齐AI blocker/projection/settlement/lifecycle/takeover/role capability并发合同，并建立channel_view独立due-unit/fleet/settlement专项
- [x] 获得新的独立复核“零 P0/P1”结论
- **Status:** complete

### Phase 4: 设计独立验收与静态校验

- [x] 独立复核是否仍有 P0/P1 实现阻断
- [x] 检查文档 diff、引用和旧合同冲突
- [x] 输出 `product_design_complete pass`；实现/生产仍为unproven
- **Status:** complete

### Phase 5: 浏览任务生产诊断

- [x] 解析精确 Task/target/time window 和 deployed SHA
- [x] 跟踪 Task→ledger/obligation→Action→Attempt/Gateway→typed view fact
- [x] 以线上证据锁定future-tail整体平移、Task级180秒上限、source state缺失与E4假绿
- **Status:** complete

### Phase 6: 代码与迁移实施

- [ ] 从合并时最新 `master` 创建干净实施工作树，保留所有用户脏改动
- [ ] 先写 AI 活群和 channel_view 的可复现回归，再实施最小根因修复
- [ ] 补模型/迁移/worker/API/UI/结构与数据流索引
- **Status:** in_progress

### Phase 7: QA 与产品验收

- [ ] focused pure tests、真 PostgreSQL 并发/崩溃/迁移、frontend/static 均通过
- [ ] 独立 QA 与 Product 验收数量、内容、unknown、浏览 typed fact 合同
- **Status:** pending

### Phase 8: 发布与运行验证

- [ ] 通过 Release Gate并以不可变 SHA 走 `master -> release -> Deploy Production`
- [ ] 核对生产 current-release、migration、全 role/capability/heartbeat 与线上读回
- **Status:** pending

### Phase 9: 生产 E4 与持续监督

- [ ] AI 活群取得 post-release remote message fact、quantity binding/timeliness、target/coverage/read-model/settlement 链
- [ ] channel_view 取得冻结 source message 对应 typed view remote fact 与投影
- [ ] 持续核对 backlog/failure/unknown/lag，只有真实业务事实达标才写 `production_fixed`
- **Status:** pending

### Phase 10: 交付

- [ ] 输出 pass/blocked/unproven、候选/部署 SHA、审计与 E4 证据
- **Status:** pending

## Key Questions

1. Product Design Complete 之后，是否仍缺少能直接驱动开发的依赖顺序、ownership 或数据契约？
2. 哪些事项只是代码实现，哪些仍属于产品/迁移合同缺口？
3. 什么证据才能分别关闭“失败风暴”“消息发送链路”和“自然日目标完成”？

## Decisions Made

| Decision | Rationale |
| --- | --- |
| 设计继续使用隔离文档工作树，实施另从最新master建干净工作树 | 用户已明确授权修复、发布和线上验证；仍需保留主release工作树的用户脏改动 |
| 不把设计通过等同修复完成 | 必须分别通过实现、QA、迁移、发布和生产 E4 |
| “浏览”按 `channel_view` 任务类型处理 | 与本项目现有任务类型一致；以生产真相源解析精确对象，不根据名称猜测 |

## Errors Encountered

| Error | Attempt | Resolution |
| --- | ---: | --- |
| 搜索了不存在的 `backend/app/api/task_center.py` | 1 | 路由实际位于 `backend/app/api/routers/task_center.py`，后续使用真实路径 |
| 隔离工作树没有 `backend/.venv/bin/alembic` | 1 | 使用主项目只读共享 venv，在隔离工作树 backend 目录运行 Alembic |
