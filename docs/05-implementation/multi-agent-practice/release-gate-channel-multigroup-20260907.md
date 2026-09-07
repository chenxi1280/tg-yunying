# 频道多账号组成员前置修复 Release Gate

- intake: AI-GROUP-LOW-FULFILLMENT-20260907 频道问题回流；L3；用户要求继续完成修复。
- base: e5d580225762671a53bcab2da6bb9bf3a11de69a；该 AI 内容槽修复已于 22:52:59 北京时间上线，20 容器健康。
- design_status: complete；local_qa_passed；CI/deploy/业务验收 pending。

## 证据与边界

13 个 running 频道任务均配置 selection_mode=group、11 个 account_group_ids，单组字段为空。已部署函数返回零候选；相同线上只读 Session 内的候选查询返回 1554，保留原租户/用途/状态/救援管理员排除条件。无账号/Task 写入或 Telegram 调用。

合并已有多组选择修复，抽出 channel_membership_candidates 模块，保留旧公开函数入口。非空多组优先，空多组兼容旧单组，空范围不扩大到 all；manual/all 不变。原测试内容保留在新独立回归文件，追加多组优先/单组/空范围参数化用例。

## QA 与发布

105 项账号池、用途边界、成员策略与浏览覆盖回归通过（backend/.venv，60 秒硬超时）；只读线上旧/新选择函数 13 个 Task 对照全部 0 -> 1554。不混入同工作区仍在修改的 schema/grounding 评论/dispatcher/listener 等其他工作。结构和数据流索引只暂存本修复追加记录。

正常 master -> release -> Deploy Production；无数据库迁移，无生产配置或批量重试。应用可回滚，既有 Action/Attempt/unknown/事实保留。上线后核对全角色版本和健康、候选及成员前置推进，再按各任务的 typed view/reaction/message fact 验收。历史已结束 Session 不通过放宽时间补发。

## CI 回流修复

Actions 34135669870 在 backend-no-postgres-checks (2) 暴露一条静态合同测试仍读取旧 channel_membership.py。已将查询职责定位改为 channel_membership_candidates.py，继续断言真实 apply_operational_account_filters 调用；不通过无用导入或注释满足检查。定向合同、多组候选、账号用途隔离共 20 项通过（5.60 秒，60 秒硬超时）。该次流水线未部署，待新提交重新执行完整 CI。
