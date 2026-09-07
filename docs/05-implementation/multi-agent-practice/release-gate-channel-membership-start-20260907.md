# 创建并启动全量关注与 10～24 小时排程 Release Gate

- Intake / L2：用户要求创建时全量安排频道关注；确认保存草稿不执行；最终选择拟人化 10～24 小时随机排程。
- 产品真相源：`docs/03-feature-designs/channel-membership-precondition-design.md` §7、§14.5、§14.6。
- 本地基线检查时 HEAD：`585e2a0c`。本轮代码仍在工作区，保留同时进行的账号分组/在线状态等改动，没有混合提交。

## 最终行为

- 创建并启动/显式启动事务内为完整有效账号范围保存关注动作，独立于帖子、AI 内容与 Planner；草稿零动作。
- 每批随机冻结 10～24 小时跨度、打散账号及间隔，保持最小 15 秒间隔、同频道最多两次未结束调用及账号成功后 60 秒冷却。0/1 个待关注账号不强制等候窗口。
- 当前启动 epoch 的频道成员动作可以先于定时主任务执行；浏览、点赞、评论仍等待计划时间和逐账号成员关系。
- 新版评论不再以 grounding enrollment 豁免频道关注；讨论组发言能力独立检查。
- 幂等重放保持动作/排程；恢复仅锁后重绑零 Attempt/journal/fact 的未调用行，保留随机间隔并审计整体顺延。已有调用和 unknown 不重放。
- 旧 1～6 小时配置显式废弃，新配置序列化不保留、前端不再提供入口；已经冻结的历史 Action 排程不自动重写。

## 验证证据

- 263 项本地定向测试通过，36.90 秒；每次后端测试均设 60 秒硬超时。
- 6 项真实 PostgreSQL 事务/并发用例通过，6.20 秒。独立本地临时数据库名为 `tg_yunying_test`，遵守 pytest advisory lock，不使用生产数据库，仅 Unix socket 访问，结束后已停止实例。
- `frontend/npm run build` 的 TypeScript 与 Vite 构建通过。
- 新模块/测试 Python 编译和函数长度检查通过；`git diff --check` 通过。
- 测试入口：`test_channel_membership_start.py`、`test_channel_membership_start_postgres.py`、`test_channel_membership_window_runtime.py`、`test_channel_membership_strategy.py`、`test_task_retirement{,_postgres}.py`，以及账号范围、创建幂等、评论计划/讨论组/恢复/设置回归。

## 发布状态

- `design_status=complete`，`local_qa=passed`，`release_status=not_released`，`production_status=unproven`。
- 本次未提交、未推送、未部署，未操作生产任务或运行历史积压清理脚本。
- 生产接受仍须独立证明候选提交、CI、部署 SHA/运行版本，以及创建时全量成员 Action 和逐账号真实关注/主互动事实；本地测试不能代替这些证据。

## 合并发布复核

用户在主发布任务明确要求合并上线。基于 585e2a0c 合并创建关注、新版评论关注检查和 account_group_ids 的预检/在线托管/监听/降权账号选择修复。复核 263 项定向测试通过（37.32 秒）、6 项隔离 PostgreSQL 事务并发测试通过（5.80 秒），前端构建通过，git diff --check 通过。无数据库迁移；保留既有冻结 Action 排程。未验收的 abandon_channel_historical_backlog.py 保留本地，本次不发布也不执行。发布后独立记录 CI、部署版本与运行健康，真实关注与互动按各自远端事实验收。

## 完整 CI 回流

Actions 34139514685 暴露 4 个旧启动/恢复测试缺少目标频道或未将目标写入 Task 配置，触发 channel_membership_start_target_invalid。测试夹具补齐真实目标，目标汇总按外键顺序清理；生产启动校验保持。重新在独立 tg_yunying_test PostgreSQL 环境执行 AI 任务限额、频道启动、评论容量与生命周期测试：79 passed、2 原有 xfailed（17.70 秒，60 秒硬超时）。未运行生产清理；该流水线没有部署。
