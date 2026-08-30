# Antigravity CLI 五账号 Provider 对接与服务器部署设计

> 日期：2026-08-30
>
> 状态：`design_status=complete` / `implementation_status=in_progress` / `production_status=unproven`
>
> 冻结模型顺序：`gemini-3.5-flash-medium` 第一，`gemini-3.1-pro-low` 第二；生成 route 中 Antigravity 优先于既有 Provider。
>
> 证据边界：CLI 生成成功不等于 Gateway 完成、远程已部署、GenerationJob 完成或 Telegram 业务恢复

## 1. 原始需求和交付顺序

目标是将五个独立 Google Antigravity CLI 运行身份纳入 `tg-yunying` AI Provider route，并在美国远程服务器持久运行。

1. 审计已有 `AiGateway`、Provider 配置和 route/admission 合同。
2. 本机真实 OAuth 登录，用最小脚本验证文本和 JSON Schema 输出。
3. 修复本文、产品总纲和数据流合同，通过 Product Design Complete 后才开发。
4. 实现显式 `antigravity_cli` adapter/bridge，完成本地自动 QA 和真实 CLI integration POC。
5. 获得新的远程变更授权后，对五个隔离 slot 逐一安装、OAuth、脚本验证和读回。
6. 受保护 Provider/route preview/apply/readback，再发布并执行真实生产 Gateway 调用；Telegram E4 仍按独立任务事实验收。

### 1.1 本轮生产切片

- 先上线一个完成独立 OAuth 的 `slot-01`，同一 slot 暴露两个固定模型 Provider 行；其余四个 slot 未逐个 OAuth 前保持不存在或 disabled，不能虚报五账号就绪。
- 生成类 purpose 的顺序固定为 3.5 Flash Medium、3.1 Pro Low、原路由供应商；`group_semantic_review` 和评论审查保持既有独立 Provider，禁止同一 slot 同时生成和审查。
- 两个模型共享 slot-01 的账号配额和单并发，因此第二模型是模型级后备，不是账号级容灾；五账号容灾必须等五个 Linux user 分别完成登录和读回。
- 线上完成口径为：部署 SHA 一致、slot process/auth/schema 健康、Provider/route 独立读回、生产容器经 Gateway 真实结构化调用成功。该口径不等于 Telegram 消息已发送。

## 2. 本轮已验证事实

### 2.1 本机默认 profile POC

| 项目 | 证据 | 结论 |
| --- | --- | --- |
| CLI | `/Users/xida/.local/bin/agy`，macOS arm64，`1.1.19` | 已安装 |
| 默认 profile | cached OAuth 可用；未读取/复制 token | 可做本地 POC |
| 文本调用 | exit 0，`status=SUCCESS`，`response=pong` | 通过 |
| Schema 调用 | `structured_output={status,reason}` | 通过 |
| 时延 | 约 6.68s / 7.29s | 不是低延迟 API |
| usage | 极简文本 total 14,491；Schema total 30,509 tokens | Agent harness 自带大上下文，需独立容量评估 |

调用只使用 `--sandbox --disable-slash-commands --print-timeout --output-format json`，未使用 `--dangerously-skip-permissions`。严格业务数据必须只读顶层 `structured_output`；实测 `response` 仍可包含 Markdown 和额外文本。

### 2.2 持久化 `slot-01` 登录结果

- 已建立独立本场 HOME/profile，没有复用默认 profile。
- 首次 OAuth 回填超过 CLI 60s 窗口，明确退出 1，模型调用为 0。
- 第二次 OAuth 与 PKCE state 匹配，但 Google 返回 `Eligibility check failed: current account is not eligible for Antigravity`。
- 2026-08-30 换用另一个 Google 账号重新 OAuth；新账号通过 eligibility check，首次 Schema 生成 exit 0、`status=SUCCESS`、total 14,839 tokens。
- 关闭首次授权进程后，使用仓库 POC 脚本从独立 profile 再次调用成功：exit 0、2 turns、约 8.51s、total 30,186 tokens。这证明登录态已持久化，不只是单进程临时状态。
- 当前逻辑 `slot-01` 可记为 `authenticated/poc_passed`；此前被拒账号只保留失败历史，不计入可用身份。

### 2.3 官方能力

- [Headless mode](https://antigravity.google/docs/cli/headless/) 支持 CI/headless、JSON/NDJSON、JSON Schema、model pin、timeout 和终态退出码。
- [Installation & Auth](https://antigravity.google/docs/cli/install/) 明确认证优先使用 OS secure keyring，SSH 登录需 URL+授权码。复制 `~/.gemini` 不是凭据迁移合同。
- [Model quotas](https://antigravity.google/docs/cli/commands/usage) 按帐户/模型记录配额；[Pricing](https://antigravity.google/pricing?app=cli) 将个人周配额/信用额与 Google Cloud Organization 按量计费分开。
- 未登录 headless 在实测中会输出 OAuth URL 并等待 60s，运行时必须有外层硬超时和独立 auth precheck。

### 2.4 远程历史记录

本轮 fresh readback 确认 `codex_usa01_server` 的 Linux x86_64 `agy 1.1.22` 位于 `/root/.local/bin/agy`；首次检查未登录、无 bridge/service/Provider/route。随后使用新 OAuth 码完成临时 root profile 登录，`agy models` 真实列出本轮两个冻结模型。该结果只证明远程账号资格和模型可见，不替代专用 `tgy-agy-01` service user 的独立 OAuth 与 slot 健康。

## 3. 现有系统合同

### 3.1 `AiGateway`

`backend/app/ai_gateway.py` 当前只接受 `provider_type=openai_compatible`，发送 `model/messages/temperature/max_tokens/stream=false`，解析 OpenAI `choices/usage`，并依赖 HTTP 429/`Retry-After` 语义。

Antigravity 是 Agent CLI，不是 Chat Completions API。它没有等价的 temperature/max_tokens/HTTP 合同，不允许伪装为 `openai_compatible` 后静默忽略字段。

### 3.2 多 Provider route/admission

现有运行时已支持 `credential_enabled/is_active/default_provider_id`、tenant+purpose 不可变 route-set、Provider admission/cooldown 和 append-only attempt。新 slot 必须作为显式 route item，不在 bridge 内做无审计的随机轮询。

当前 transport route 会在 timeout 后尝试下一 Provider。对 CLI 而言，进程已启动后的 timeout 必须归类 `provider_result_unknown`，先按稳定 request ID 对账，不得直接换账号重复生成/计费。

### 3.3 历史 Grok CLI Bridge

`backend/app/services/grok_cli_bridge.py` 提供了有界 subprocess、临时 workspace、`shell=false`、容量锁、退出码和 JSON envelope 先例。可复用其工程原则，不得重启已被 current contract 废弃的 Grok fallback 拓扑。

## 4. 已否定的旧方案

1. **共享一个 root HOME/profile 承载五账号**：否定。OAuth/keyring、配额、会话 DB、updater lock 和日志必须隔离。
2. **把宿主 `127.0.0.1:18099` 配给 Docker worker**：否定。容器内 loopback 不是宿主机。
3. **只加 OpenAI 响应外壳**：否定。会丢失 CLI status/auth/quota/turns/usage/unknown 语义。
4. **超时后立即换下一账号**：否定。只有 durable `not_started` 才允许 failover。
5. **复制 OAuth 目录到其他 slot/服务器**：否定。每个 slot 必须独立人工 OAuth。

## 5. 目标架构

```text
GenerationJob frozen request/route/request_id
  -> AiGateway(provider_type=antigravity_cli)
  -> authenticated internal slot endpoint
  -> host.docker.internal:18101..18105
  -> agy-provider@01..05 (one Linux service user per slot)
  -> agy --print --output-format json --json-schema ...
  -> typed envelope + durable request reconciliation
  -> common parser/quality gate
  -> Action -> ExecutionAttempt/Gateway -> Telegram typed remote fact
```

### 5.1 账号隔离

远程使用五个专用 Linux service user（例 `tgy-agy-01..05`）：

- 独立 HOME、keyring/profile、cache、conversation DB、log 和 updater lock；
- 每账号独立 provider row、bridge token、port、admission/cooldown 和单并发 worker；
- 日志仅保存 `slot-01..05`，不保存 Google 邮箱、OAuth code/token；
- 单 slot 失效只停用该 Provider，不覆盖其他 slot 凭据。

CLI 的隐藏 `app_data_dir/gemini_dir` 参数不在官方 flag reference，只可用于本场 POC，生产隔离只依赖 OS user/HOME 边界。

### 5.2 Docker-to-host

- 每个 slot service 只监听宿主内部端口，不开放公网。
- Compose 显式注入 `host.docker.internal:host-gateway`；防火墙只允许 Docker bridge 网段。
- `AiProvider.api_key` 保存的是每 slot 内部 bearer token 密文，绝不是 Google OAuth token。
- process health 和真实 model/auth/schema check 是不同证据。

### 5.3 内部 API

#### `POST /internal/v1/generate`

请求必须含 `request_id/model/system_prompt/user_prompt/json_schema/effort/timeout_seconds`。

- request ID + request hash 幂等；相同 ID 不同 hash 返回 409。
- model 必须固定官方 slug；无效 model 非零失败，不回退默认模型。
- json schema 必填；响应只返回顶层 `structured_output`。
- temperature/max_tokens 不得静默忽略；调用方强依赖时显式 `unsupported_provider_parameter`。

成功响应含 `request_id/state=confirmed/slot_id/model/structured_output/usage/duration_seconds/num_turns`。

#### `GET /internal/v1/requests/{request_id}`

返回 `not_started|started|confirmed|failed|unknown`。只有 `not_started` 可进入下一 route item；`started|unknown` 保持原 GenerationJob 待对账。

#### `GET /internal/v1/health`

只返回脱敏 `slot_id/version/process/auth_probe_age/inflight/last_terminal_code`，不返回邮箱、token、prompt、candidate 或 conversation ID。

## 6. CLI 执行合同

固定命令参数：`--sandbox --disable-slash-commands --print-timeout --output-format json --json-schema --model --effort -p`。

严禁：

- `--dangerously-skip-permissions`、`--continue` 或业务间复用 conversation；
- 在仓库/用户 HOME 运行业务 prompt；每次使用新的空临时 workspace；
- `shell=true`；
- 把 prompt/schema/token/OAuth URL/code 记录到命令日志。

同时校验 subprocess return code、单一 JSON envelope、`status=SUCCESS` 和 object `structured_output`。

| 证据 | bridge 状态 | 可否换 slot |
| --- | --- | --- |
| binary missing / capacity busy / process 未启动 | `not_started` | 可 |
| auth/quota 明确 pre-call | `failed` + typed code | 可 |
| `SUCCESS` + schema 通过 | `confirmed` | 否 |
| 进程已启动后 timeout/连接断开 | `unknown` | 否，先对账 |
| `SUCCESS` 但 schema/质量失败 | confirmed content failure | 否，不是 transport |

## 7. 幂等、并发与对账

- 每 slot 有 durable request ledger：`request_id/hash/slot/model/state/version/claimed/process_started/terminal/pid/exit/error/usage/duration`。
- confirmed result 加密并有 TTL，供 backend 连接中断后读回；不保存 OAuth token、邮箱或完整思考。
- 每 slot 默认 `max_inflight=1`；五 slot 物理上最多 5 in-flight，仍受 quota/cooldown 和 tenant+purpose admission 约束。
- busy 是 pre-call 容量事实，不在 worker 内 sleep 等锁。
- request ID 由已冻结 GenerationJob/request hash 派生；`provider_result_unknown` 不属于 `route_transport_failure`。
- 迟到结果必须重验 job/route/context/policy/request hash；漂移后只写 stale attempt，不创建 Action。

## 8. Provider、前端与安全

新类型为 `provider_type=antigravity_cli`；每 slot 一行 `AiProvider`，`base_url` 指向内网 slot endpoint，`model_name` 是官方 CLI slug，`api_key` 是内部 bridge token。

Provider check 必须分别报告 process/auth/model/schema/quota，不能因端口 200 就标记 healthy。前端展示 slot ID、CLI version/model、分项 health、inflight/busy/cooldown、最后 latency/usage 和 `provider_result_unknown`，不展示邮箱/OAuth/conversation ID。

安全合同：

- OAuth 只由 CLI/OS keyring 管理，不进 DB、Git、Docker image、日志或文档。
- bridge token 每 slot 独立、密文保存、可轮换。
- service user 无 Telegram Session/业务 DB 凭据访问权。
- prompt/candidate 不进普通日志/metrics label，只记 hash、长度、usage、latency 和 typed code。
- 使用五个人账号前，账号归属、订阅/信用额、商业授权和 Google 条款需业务负责人确认；技术 POC 不是计费/法务授权。

## 9. Typed errors

- `antigravity_binary_missing`
- `antigravity_auth_required`
- `antigravity_account_ineligible`
- `antigravity_model_invalid`
- `antigravity_capacity_busy`
- `antigravity_quota_limited`
- `antigravity_cli_exit_nonzero`
- `antigravity_invalid_envelope`
- `antigravity_schema_missing`
- `antigravity_schema_invalid`
- `antigravity_provider_result_unknown`
- `antigravity_request_id_reused`
- `antigravity_bridge_unauthorized`
- `antigravity_bridge_unreachable_pre_call`

## 10. 实施阶段

### Phase 0：本机真实 POC

默认已登录 profile 与换号后的独立 `slot-01` 文本/Schema 调用已通过。未证明其余四账号、并发、quota 和远程。

### Phase 1：可重复 POC 脚本

实现无密钥、默认无工具、受限时的本场脚本，校验 envelope/status/structured_output/usage，任何错误非零退出。它是 feasibility POC，不是生产 bridge。

### Phase 2：本场 bridge + adapter

- 先写 subprocess、幂等、busy、timeout/unknown、reconcile 和安全测试。
- 实现 `antigravity_cli` provider type，不改变 OpenAI 旧语义。
- 修复 candidate runtime：`provider_result_unknown` 不进入 transport failover。
- 更新 API/前端/数据流/结构索引并执行一个不发 Telegram 的真实 integration POC。

### Phase 3：远程安装/五账号

需新的精确授权和 fresh readback。对每个 OS user/slot 逐一执行：版本/checksum -> TUI OAuth -> 脱敏 profile ready -> text/schema POC -> slot service/network readback。失败 slot 保持 disabled，不用其他凭据覆盖。

### Phase 4：canary/发布/E4

guarded preview 冻结 provider/slot/route/task/deployed SHA/fingerprint；apply 校验 actor/approval 与漂移；只启用一个显式 canary task。生产验收继续要求 `GenerationJob -> Action -> ExecutionAttempt/Gateway -> Telegram typed remote fact`，Provider check 不等于 `production_fixed`。

## 11. QA 验收

### 11.1 POC 脚本

1. binary missing、CLI nonzero、invalid JSON、status 非 SUCCESS、structured output 缺失全部非零失败。
2. success 输出脱敏 status/duration/turns/usage/structured result。
3. 外层 timeout 终止子进程并失败，POC 阶段不伪造可重试语义。

### 11.2 Adapter/bridge

1. 五 slot 不共享 HOME/state/token/port；每 slot 单并发。
2. 同 request ID/hash 幂等，不同 hash 409。
3. invalid model 不回退；auth/eligibility/quota/process/schema 错误分类。
4. 连接断开后可读回 confirmed；started/unknown 不换 slot。
5. schema/质量失败不当 transport failover。
6. Provider attempt 含 purpose/provider/model/slot/outcome/latency/usage/code，不含敏感内容。
7. Docker worker 可访问 host slot，公网/未授权请求不可访问。

### 11.3 证据层级

1. 本场测试；2. 真实 CLI POC；3. 部署 SHA/运行读回；4. 五 slot 独立读回；5. guarded config canary；6. GenerationJob/Action；7. Gateway/Telegram typed remote fact。只有最后一层支持业务 E4。

## 12. 回滚

- 用新 route revision 前向移除 slot 或关闭 canary flag，不删除历史 attempt/revision。
- 停新请求后先对账 started/unknown，未对账前不停服务/换账号重放。
- Gateway-started 的 Action/Attempt 按 Telegram 远程事实对账，不因 Provider 回滚重发。
- OAuth 撤销前先从 active route 移除该 slot，排空/保持 unknown hold。

## 13. Product Design Complete 自检

| 维度 | 状态 | 说明 |
| --- | --- | --- |
| 需求、现有入口、目标架构 | complete_for_handoff | 已审计 Gateway/route/admission/Grok 先例 |
| 本场可行性 | complete_for_one_slot | 默认 profile 与换号后的独立 slot-01 POC 通过 |
| 五账号对应/资格/授权 | complete_for_slot_01 | 本轮只上线 slot-01；其余四个 slot 明确不在本轮完成口径 |
| model slug/effort | complete | 3.5 Flash Medium 第一；3.1 Pro Low 第二，Low slug 不额外传 effort |
| API、状态、幂等、unknown | complete_for_handoff | 已定义运行边界 |
| 前端、安全、QA、回滚、E4 | complete_for_handoff | 已定义 |
| 远程现状 | complete_for_handoff | 已 fresh readback 并完成临时 root 模型资格检查；专用 slot service/OAuth 仍属于实施步骤 |

当前 `design_status=complete`，本轮只授权并验收 `slot-01` 生产切片。五账号整体状态保持 partial，后续 slot 必须分别完成 OAuth、schema POC 和 readback 后才能加入 route；不得用 slot-01 的凭据复制或代替。

## 14. 多模型业务回复 POC

2026-08-30 已使用独立 `slot-01` 对 `gemini-3.5-flash-medium`、`gemini-3.6-flash-medium`、`gemini-3.7-flash-medium` 执行固定 AI 活群与频道评论合成样本。六个候选均形成终态；样本内跨场景质量为 3.6 最稳，3.7 活群质量和时延最佳但评论被当前地点/敏感交易 cleaner 拒绝，并触发事实边界硬失败。完整输入、候选、评分、运行异常与证据边界见 `docs/05-implementation/antigravity-cli-model-reply-poc-20260830.md`。

## 15. 成人方向 route-shaped POC

2026-08-30 又以同一独立 `slot-01`、三个 Medium 模型执行 7 个成人方向合成场景，共 21 个正式调用，覆盖 `adult_visual`、`adult_product`、`adult_service_inquiry`、`adult_service_sensory`、成人双关设计缺口、PII/性交易执行静默和弱词不得强转成人 route。

样本内，3.5 是唯一 7/7 通过结构与独立事实/安全硬门的模型；3.7 在多数正向文案中自然度最高，但 sensory 连续拒答且没有 `structured_output`；3.6 全部结构成功，但 visual 新增了证据未支持的“腿型”。因此当前单一基础候选为 3.5，3.7 只能作为经过 mode 路由后的风格候选，不能承接 sensory。成人双关仍不是当前 MessageBrief v2 正式 mode，PII/精确地址/代约意图必须在 Provider 前静默。

完整输入边界、候选、当前代码闸、盲评、耗时和未证明项见 `docs/05-implementation/antigravity-cli-adult-routes-poc-20260830.md`。本结果仍不证明 Gateway、远程 bridge、五账号、Telegram 发送或生产 E4。

同日补测 `gemini-3.1-flash-medium`：authenticated `agy models` 不提供 3.1 Flash；带 effort 和去掉 effort 的两次探针均在模型调用前失败，后者明确为 model not recognized，且两次都是 zero turn/usage。当前只有 3.1 Pro High/Low，不能替代为 3.1 Flash 结果。

用户明确改为 `gemini-3.1-pro-low` 后，按同一 7 场景和 Schema 补测：7/7 形成结构化终态，当前四个正式成人 v2 mode 为 4/4 parser/gate 通过，sensory 可用，两个安全边界均静默；成人双关的“最费腰”因新增源证据没有的身体效果被独立事实审查拒绝。其平均 CLI duration 为 11.22 秒，是四模型最慢，因此只形成可行 Pro 候选，不改变当前 3.5 单模型基础候选判断。
