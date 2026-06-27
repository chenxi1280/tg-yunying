# 04-ops：运维与上线

本目录保存生产部署、上线验收和排障文档。

| 文件 | 定位 |
| --- | --- |
| [deployment/PRODUCTION_RUNTIME.md](deployment/PRODUCTION_RUNTIME.md) | 生产部署说明、容器、worker 和验证口径 |
| [ai-group-hard-hourly-target-ops.md](ai-group-hard-hourly-target-ops.md) | AI 活跃群每小时硬目标排障和运营动作 |

维护规则：

- 运维文档可以引用 PRD 的规则，但不重复定义产品需求。
- 上线/生产验收必须写清楚真实环境证据，不把本地构建当线上完成。
