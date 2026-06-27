# Mini Bug Card Template

- bug_id:
- intake_id:
- level: L0 | L1
- route: quick_fix
- owner_agent: product
- to_agent: dev
- evidence_level: E0 | E1 | E2
- locked_paths:
- release_gate: not_required

## 现象

## 期望结果

## 复现路径 / 截图 / 日志

## 限定范围

## 不允许改的内容

## 快速验收方法

## 升级标准流程的触发条件

- 影响生产、worker、数据库、权限、TG 真实链路。
- 需要改 PRD、数据流转索引或项目结构索引。
- 修复范围超过 `locked_paths`。
- QA 无法定向验收。
