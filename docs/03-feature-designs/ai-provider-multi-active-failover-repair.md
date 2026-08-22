# AI 供应商多启用与兼容降级修复

> 日期：2026-08-22  
> 分级：L3 生产故障  
> 状态：Product Design Complete / development handoff  
> 生产完成口径：仅真实任务生成、Gateway 与 Telegram typed remote fact 读回可写 `production_fixed`

## 1. 原始问题与生产事实

用户要求同时修复两项问题：AI 供应商发生限流时没有降级到下一供应商；供应商切换失败，而且系统应允许多个供应商同时启用。

2026-08-22 生产只读证据显示：

- 6 个运行中的 `group_ai_chat` 任务均未启用内容路由 v2、两阶段生成或内容 policy；本修复不得借机强开 v2。
- MiniMax M3 是唯一 `is_active=true` 的供应商，持续返回 HTTP 429；claim admission 因单活集合全部 cooldown，在生成调用前停止领取。
- `group_realize_general` route-set 已存在，但只有 MiniMax M3 一个 item。
- 数据库部分唯一索引 `uq_ai_provider_single_active` 与 `ai_config._disable_other_active_providers` 会在启用一个供应商时关闭其他供应商。
- 现有候选执行器已经支持 route-set 顺序与 typed transport failover，但 legacy 任务没有绑定该 route-set。

## 2. 范围与非目标

本次只修复供应商可用性边界，不改变数量义务、账号选择、节奏、Prompt、质量门禁、Action/Gateway/Telegram 事实合同。

不在本次范围内：

- 不给现有任务开启 `ai_content_route_v2_enabled`、`ai_two_stage_enabled` 或补造内容 policy。
- 不用静态文本、mock、签到或 emoji 伪装供应商降级成功。
- 不把 JSON/schema/内容质量/敏感词/独立 reviewer 失败解释成可跨供应商的 transport failure。
- 不修复同轮发现的搜索点击义务唯一键或频道节奏问题。

## 3. 目标语义

### 3.1 三个独立状态

- `AiProvider.credential_enabled`：凭证可被健康检查和显式 route-set 使用。
- `AiProvider.is_active`：供应商可被 legacy/default 运行链选用；允许多行同时为 true。
- `TenantAiSetting.default_provider_id`：该租户的首选供应商；与其他供应商是否启用无关。

设置默认供应商不再批量停用其他供应商。停用仍被租户默认配置引用的供应商必须明确失败，先切换默认再停用。

### 3.2 兼容降级开关与顺序

新增租户级 `ai_provider_route_fallback_enabled`，默认 false。开启后，未启用内容路由 v2 的 AI 活群任务在每个生成批次读取并冻结当前 active `group_realize_general` route-set：

```text
legacy group generation batch
  -> tenant default provider remains the compatibility binding
  -> freeze active group_realize_general route items by priority
  -> provider-level admission for candidate 1
  -> typed transport failure/cooldown => candidate 2 ... candidate N
  -> success => existing parser and quality gates
  -> all candidates temporary unavailable => explicit provider wait/failure
```

开关开启但 route-set 缺失、没有候选、候选凭证未启用或全部不健康时显式失败，不退回单供应商或静态成功。

### 3.3 允许切换的失败

只允许以下类型进入下一优先级：

- provider admission cooldown / probe in flight；
- HTTP 429；
- timeout、连接失败、明确 HTTP 5xx；
- 明确 quota exhausted。

HTTP 4xx（429 除外）、空最终内容、JSON/schema 错误、质量拒绝、安全拒绝、路由不匹配都不得切换供应商。

claim admission 必须读取所有 `is_active=true AND health=健康` 的供应商；只要至少一个未 cooldown 就允许领取。真正调用前仍对每个候选执行独立 admission。

## 4. 后端、迁移与 API

- migration `0161` 删除 `uq_ai_provider_single_active`，并为 `tenant_ai_settings` 增加 `ai_provider_route_fallback_enabled=false`。
- `create_ai_provider/update_ai_provider` 不再关闭其他 active provider，也不再用 active 状态隐式改写所有租户默认供应商。
- `update_tenant_ai_setting` 切换默认供应商时校验目标存在、`credential_enabled=true`、`is_active=true` 且健康；错误保持 422 且说明原因。
- legacy runtime 只在新开关开启时绑定 `group_realize_general` 快照；内容路由 v2 继续使用自己的完整 purpose/job snapshot 合同。
- 候选执行器以“显式 route-set”作为跨供应商授权，不扩展无 route 的隐式轮询。

## 5. 前端状态

- 供应商卡片的“已启用”来自 `is_active`，允许多张卡同时显示已启用。
- “默认模型”只比较当前租户 `default_provider_id`。
- 供应商操作改为“启用/停用”，不再叫“设为默认”。默认模型仍在“运营空间 AI 配置”中单独切换。
- 默认模型下拉只展示凭证启用、供应商启用且健康的候选。
- 租户 AI 配置新增“启用供应商优先级降级”；说明它使用 `group_realize_general` route-set，且只处理 transport 故障。

## 6. 生产变更合同

生产配置只允许通过受版本控制的脚本/工作流执行：

1. preview 精确列出 tenant、provider old state、active route revision/items、setting old state和目标 diff，并输出 SHA-256 fingerprint。
2. apply 必须提交 deployed SHA、expected fingerprint、actor、approval reference；行锁后重算 fingerprint，漂移则零写入失败。
3. 先多启用候选供应商并逐个执行真实 health check；未达到至少两个健康候选时不得开启租户降级开关。
4. 以新 revision 创建 `group_realize_general` route-set，旧 active revision 改为 retired；同事务开启租户降级开关并写 AuditLog。
5. 独立 readback 核对 provider 状态、route revision/hash/items 和 setting。

任何 Provider check 只证明接口可调用，不是业务 E4。

## 7. 验收标准

### 自动化 QA

1. schema/migration 允许至少两个 provider 同时 active，且旧单活索引不存在。
2. 启用/停用 provider 不修改其他 provider；设置默认不修改 provider active 集合。
3. 默认切换拒绝不存在、停用、凭证禁用或不健康的 provider。
4. legacy 开关关闭时行为不变；开启时按 route priority 冻结候选。
5. 首选 provider cooldown/429/timeout/5xx 后调用下一候选并成功；4xx、空内容与质量错误不切换。
6. 所有候选临时不可用时持久等待/失败，不生成 mock/static success。
7. 前端构建通过，卡片可同时显示多个启用供应商，默认标签唯一。

### Release Gate 与生产 E4

- `master -> release -> Deploy Production` 成功，生产 release SHA 与运行容器一致。
- guarded provider/route preview、apply、readback 一致，至少两个候选为启用且健康。
- 受控验证首选 provider 被 cooldown 时，日志/attempt 证明同一生成请求转到下一 route item。
- 至少一个原运行任务出现新生成结果、ready Action、Gateway attempt 和 Telegram `remote_message_id`/typed remote fact；任务 confirmed 数量增加。
- 只有满足上一条才能写 `production_fixed`；健康、CI、部署、route readback 或本地生成均只到 E0-E3。

## 8. 回滚

先通过 guarded mutation 关闭 `ai_provider_route_fallback_enabled` 并读回；新批次恢复原 legacy 默认供应商路径，已冻结/已进 Gateway 的批次继续按原身份对账。route-set 使用新 revision 前向切换，不删除历史 revision。应用回滚不得修改 Action、Attempt、GenerationJob 或 typed remote fact。
