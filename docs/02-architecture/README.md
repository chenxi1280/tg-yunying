# 02-architecture：架构与容量

本目录保存技术架构、容量、调度和压测报告。

| 文件 | 定位 |
| --- | --- |
| [architecture.md](architecture.md) | 当前技术架构简版入口 |
| [architecture-scale-assessment-and-upgrade-plan.md](architecture-scale-assessment-and-upgrade-plan.md) | 架构容量评估和升级方案 |
| [capacity-and-dispatch-upgrade-plan.md](capacity-and-dispatch-upgrade-plan.md) | 容量与 dispatcher / worker 调度升级方案 |
| [reports/capacity-report-100-300-1000.md](reports/capacity-report-100-300-1000.md) | 100 / 300 / 1000 账号容量报告 |

维护规则：

- 系统边界、worker 角色、数据库、Redis、容量调度放在这里。
- 产品流程和按钮验收放在 `01-product/` 或 `03-feature-designs/`。
