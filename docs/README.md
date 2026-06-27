# TG 运营管理平台文档中心

> 本目录已经按“用途 + 权威层级”整理。遇到文档内容重复时，优先按本文的权威顺序判断，不要在多个旧文档里来回猜。

## 阅读顺序

| 目的 | 先看 | 再看 |
| --- | --- | --- |
| 了解当前产品需求 | [完整 PRD](01-product/tg-ops-platform-prd.md) | [当前代码到 PRD 的实施清单](05-implementation/tg-ops-platform-prd-refactor-checklist.md) |
| 查代码结构 | [项目结构索引](00-index/project-structure-index.md) | [项目数据流转索引](00-index/project-dataflow-index.md) |
| 查架构和容量 | [技术架构设计](02-architecture/architecture.md) | [容量与调度升级方案](02-architecture/capacity-and-dispatch-upgrade-plan.md) |
| 查专项能力 | [专项设计目录](03-feature-designs/README.md) | 对应专项 PRD / 设计文档 |
| 查上线和运维 | [运维目录](04-ops/README.md) | [生产部署说明](04-ops/deployment/PRODUCTION_RUNTIME.md) |
| 查历史计划 | [历史记录目录](99-history/README.md) | 历史 specs / plans |

## 权威层级

1. **当前产品源头**：`01-product/tg-ops-platform-prd.md`
2. **代码维护索引**：`00-index/project-structure-index.md`、`00-index/project-dataflow-index.md`
3. **专项 PRD / 设计**：`03-feature-designs/*`
4. **架构和容量方案**：`02-architecture/*`
5. **运维手册**：`04-ops/*`
6. **历史记录**：`99-history/*`

如果专项文档和主 PRD 冲突：先按专项文档中写明的验收口径处理，再把结论回写主 PRD。

如果历史记录和当前文档冲突：以当前文档为准，历史记录只作为背景证据。

## 目录结构

```text
docs/
  00-index/            # 代码结构、数据流转和维护索引
  01-product/          # 当前产品 PRD、总纲、产品设计和功能分析
  02-architecture/     # 架构、容量、调度和报告
  03-feature-designs/  # 账号、安全、风控、素材、规则、准入等专项设计
  04-ops/              # 生产部署、上线验收、运维排障
  05-implementation/   # 当前代码到 PRD 的实施清单
  99-history/          # 历史 specs / plans，保留证据但不作为当前源头
```

## 内容优化原则

- 主 PRD 只放产品源头、验收口径和跨模块规则。
- 专项文档只放某个能力的细节，不重复维护全局产品描述。
- 架构文档只放系统边界、容量、worker、数据库和部署结构。
- 索引文档只负责“去哪找代码 / 数据怎么流转”，不替代 PRD。
- 历史计划保留原貌，避免丢失决策背景，但不作为当前需求依据。
