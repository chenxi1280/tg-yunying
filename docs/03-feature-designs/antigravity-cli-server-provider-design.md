# Antigravity CLI 五账号 Provider 对接与服务器部署设计

> 日期：2026-08-30
>
> 状态：`design_status=complete` / `resync_status=complete` / `implementation_status=complete` / `qa_status=pass` / `product_status=accepted` / `production_status=blocked`
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

### 2.2 本机 POC profile 的持久化登录结果

- 已建立独立本场 HOME/profile，没有复用默认 profile。
- 首次 OAuth 回填超过 CLI 60s 窗口，明确退出 1，模型调用为 0。
- 第二次 OAuth 与 PKCE state 匹配，但 Google 返回 `Eligibility check failed: current account is not eligible for Antigravity`。
- 2026-08-30 换用另一个 Google 账号重新 OAuth；新账号通过 eligibility check，首次 Schema 生成 exit 0、`status=SUCCESS`、total 14,839 tokens。
- 关闭首次授权进程后，使用仓库 POC 脚本从独立 profile 再次调用成功：exit 0、2 turns、约 8.51s、total 30,186 tokens。这证明登录态已持久化，不只是单进程临时状态。
- 该本机 POC profile 可记为 `authenticated/poc_passed`；此前被拒账号只保留失败历史，不计入可用身份。它不是生产服务器专用 Linux user `tgy-agy-01` 的登录事实，不能据此把生产 slot 标记为 authenticated。

### 2.3 官方能力

- [Headless mode](https://antigravity.google/docs/cli/headless/) 支持 CI/headless、JSON/NDJSON、JSON Schema、model pin、timeout 和终态退出码。
- [Installation & Auth](https://antigravity.google/docs/cli/install/) 明确认证优先使用 OS secure keyring，SSH 登录需 URL+授权码。复制 `~/.gemini` 不是凭据迁移合同。
- [Model quotas](https://antigravity.google/docs/cli/commands/usage) 按帐户/模型记录配额；[Pricing](https://antigravity.google/pricing?app=cli) 将个人周配额/信用额与 Google Cloud Organization 按量计费分开。
- 未登录 headless 在实测中会输出 OAuth URL 并等待 60s，运行时必须有外层硬超时和独立 auth precheck。

### 2.4 远程历史记录

本轮 fresh readback 确认 `codex_usa01_server` 的 Linux x86_64 `agy 1.1.22` 位于 `/root/.local/bin/agy`；首次检查未登录、无 bridge/service/Provider/route。随后使用新 OAuth 码完成临时 root profile 登录，`agy models` 真实列出本轮两个冻结模型。该结果只证明远程账号资格和模型可见，不替代专用 `tgy-agy-01` service user 的独立 OAuth 与 slot 健康。

截至本次 Bug Batch resync，生产 `tgy-agy-01` OAuth 尚未形成成功终态，slot unit 为 `disabled/inactive`、18101 无监听，数据库不存在两行 Antigravity Provider，生成 route 也未切换。因此发布 SHA/容器健康只能记为部署证据，生产 Gateway 调用仍是 `unproven`，不得写成线上可调用。

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
- slot unit 与 Compose 必须从同一个实际业务网络（默认 `infra_default`）读取 gateway；Compose 将该精确地址注入为 `host.docker.internal`，禁止使用可能指向默认 `docker0` 的裸 `host-gateway`。
- gateway 解析失败必须在 Compose 变更前 fail closed；backend 与三个 AI generation worker 必须读取相同地址，发布后逐容器验证 DNS 解析、带 token health 和真实模型调用。防火墙只允许该 Docker bridge 网段。
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

## 14. 生产 Bug Batch 与 Product Design Complete resync

### 14.1 Intake Card 与分级

| 字段 | 冻结值 |
| --- | --- |
| batch_id | `ANTIGRAVITY-PROVIDER-PROD-BUG-BATCH-20260830` |
| 原始目标 | 修复已发布实现，使生产 `slot-01` 可经正式 Gateway 结构化调用；3.5 Flash Medium 第一、3.1 Pro Low 第二、原 Provider 后备 |
| 当前事实 | 代码 SHA 已部署；专用 slot 未登录/未监听；Provider/route 未配置；生产调用未证明 |
| 分级 | `L3`，影响 Provider 计费、AI 活群生成和生产发布 |
| 当前决策 | route apply 与最高优先级切换保持阻断，先完成本 Batch 的开发和 QA |
| 不变边界 | 只上线 slot-01；语义审查不切 Antigravity；started/unknown 不重放；Telegram E4 独立验收 |

### 14.2 Root Cause Grouping

#### RC-1：请求身份只绑定 Job，未绑定一次真实模型调用

当前 request ID 仅含 `GenerationJob + purpose + stage`。同一 Job 的多个 brief/realizer slot、同 stage 重试或不同 route item 会复用 ID，而 payload hash 不同，第二次调用被 ledger 以 `antigravity_request_id_reused` 拒绝。

修复后的 invocation identity 必须在 Provider 调用前冻结并持久化，至少包含：GenerationJob/generation sequence、purpose/stage、brief 或 realization slot identity、realization attempt、route revision/item、provider/model。一次 invocation 的 transport retry/reconcile 必须复用完全相同的 ID 与 request hash；只有前一候选被证明为 `not_started` 或明确 pre-call failed 后，下一 route item 才使用其独立 invocation ID。不得用随机数、当前时间或仅在内存中的循环序号生成身份，也不得把 prompt hash 当作唯一业务身份来掩盖同一身份内容漂移。

#### RC-2：bridge 的 spawn、终态解析和 route 错误语义没有共用一套状态机

- ledger 必须先保存可恢复的 claim/request hash；`Popen` 成功取得 PID 后才 CAS 为 `started`。`FileNotFoundError/PermissionError/OSError` 等 PID 前失败写 `failed + process_started=false + typed code`，可进入下一 route item；PID 后无法证明终态的异常一律写 `unknown`。
- CLI 非零退出必须先依据 return code、脱敏 stderr 和可选 stdout envelope 分类 auth/eligibility/model/quota/exit，再对成功 stdout 做严格 JSON/envelope/schema 校验。不得因空或非 JSON stdout 把明确认证/配额错误覆盖为 `invalid_envelope`。
- `antigravity_auth_required`、`antigravity_account_ineligible`、`antigravity_model_invalid`、`antigravity_quota_limited` 只有在独立 preflight 或 zero-turn/zero-usage 等可证明的 process-start 前事实下才允许 route failover；不能证明是否开始模型调用时必须转 `unknown`，不得换 Provider。
- backend client、candidate runtime、attempt outcome、health/cooldown 使用相同 typed-code 枚举；不得把 401/422 一律降成普通 `RuntimeError`，也不得依赖模糊字符串识别 quota。

#### RC-3：host service 沙箱与发布生命周期未区分 disabled slot

- `ProtectSystem=strict` 保留；为专用 user 只开放 Antigravity 真实需要的 HOME 子目录（至少独立 `.gemini` 认证/profile/conversation/log/cache，以及经实测需要的专用 cache/state），禁止开放仓库、Telegram Session、业务数据库或其他用户 HOME。
- 专用 user 不获得 `/data`、release 仓库或 `current` 的目录穿越权限；bridge 的四个必需 Python 文件（server、protocol、ledger、canonical schema）由 root 按完整 release SHA 原子安装到 `/usr/local/lib/tgyunying-antigravity/releases/<SHA>`，`current` 只读 symlink 原子切换。host bridge 固定使用独立 `/usr/bin/python3.11` 和系统包 `python3.11-cryptography`，不得替换或复用生产其他 host 脚本依赖的 `/usr/bin/python3` 3.6。显式 slot 安装总是同步 runtime；普通发布只有存在 enabled slot 时才同步并重启，disabled/inactive slot 不因 runtime 安装失败阻断普通发布。
- 安装完成但 OAuth/Schema probe 未通过的 unit 必须是 `disabled/inactive`。发布脚本只 restart 已 enabled 的 slot；disabled unit 必须记录为 skipped，不能被发布重新激活，也不能使普通部署失败。已 enabled slot restart/health 失败则发布 fail closed。
- 每个 slot 在 systemd 沙箱上下文内完成可写目录探针、`agy models` 脱敏读回和真实 JSON Schema 调用；root profile 成功不替代 service user 证据。

#### RC-4：合并引入了与正式合同互斥的第二套 Antigravity 拓扑

`backend/app/services/antigravity_cli_bridge.py` 与 `backend/scripts/antigravity_http_bridge.py` 实现的是 backend/host 直接 CLI 加 OpenAI `/v1/chat/completions` text wrapper：使用全局 HOME/lock、`output-format=text`、模糊 model mapping、无 request ledger/reconcile、无内部 bearer auth，并用字符长度伪造 token usage。仓库运行入口没有引用它们，只有专属测试和后来合并的 Settings/UI heuristic 引用；它们与第 4、5、6、7 节正式合同直接冲突。

产品决策为**整套删除，不保留 feature flag、兼容入口或 deprecated wrapper**：

1. 删除 `backend/app/services/antigravity_cli_bridge.py`、`backend/scripts/antigravity_http_bridge.py` 及只验证该旧拓扑的测试。
2. 删除仅服务旧拓扑的全局 Settings 字段与 `ANTIGRAVITY_CLI_ENABLED` 路径；host unit 仍可使用自己的 `ANTIGRAVITY_CLI_BIN` 环境变量，不得重新接入 backend Settings。
3. 删除 UI 对名称、Gemini 字样和 18099 端口的启发式识别/过滤，以及旧 wrapper 引入的非冻结 model alias；Antigravity 只按 `provider_type=antigravity_cli` 判断，保留精确 `gemini-3.5-flash-medium` 与 `gemini-3.1-pro-low`。
4. 全仓必须只剩正式拓扑：`AiGateway -> AntigravityProviderClient -> /internal/v1/generate|requests|health -> antigravity_provider_server -> durable ledger -> agy JSON Schema`。禁止 `/v1/chat/completions`、`output-format=text`、fuzzy model mapping、18099 端口和伪造 usage 回流。

#### RC-5：Provider capability、Schema 与健康口径不闭合

- Antigravity adapter 不支持 temperature/max_tokens。Gateway capability 必须显式声明；内部调用在选择 Antigravity 时不得传这两个字段，任何显式非空输入都在 process-start 前返回 `unsupported_provider_parameter`。UI 对该类型禁用且不保存这些字段，不能静默忽略。
- planner/realizer Schema 必须来自 current MessageBrief v2/realizer canonical contract，覆盖 claims、reply binding、mode、speech act、evidence IDs 和各 route 必填字段；禁止只列少量字段再用 `additionalProperties=true` 代替合同。bridge Schema 通过后仍必须进入既有 parser、事实/安全 deterministic gate。
- `/health` 返回脱敏缓存事实：process、CLI version、auth probe age、固定 model 可见性、最近 schema probe、quota/cooldown、inflight、last terminal code。Provider check 必须执行有界真实 Schema probe 并分别写回这些分项；端口 200 不能把 Provider 标记 healthy。
- Provider create/update 对 `antigravity_cli` 做边界校验：base URL 只能是批准的内部 slot host/port，model 只能是冻结 slug，凭据只允许内部 bridge bearer token；不允许租户配置任意 URL 形成 SSRF，也不展示 Google/OAuth 身份。

#### RC-6：配置 apply 和发布 Gate 仍允许漂移或局部切换

- generation purpose 使用固定 allowlist：`group_context_route`、`group_realize_general`、`group_realize_adult_visual`、`group_realize_adult_product`、`group_realize_adult_service_inquiry`、`group_realize_adult_service_sensory`；`group_semantic_review` 明确禁止加入本批。
- provider/route preview 必须冻结 tenant、两行 provider identity、全量 generation routes 及原后备顺序、tenant default、bridge token fingerprint、完整 deployed SHA、actor/approval 和 snapshot fingerprint。apply 必须在同一事务核验未漂移并原子写入全部 generation routes；任一 purpose 失败则零写，禁止逐 purpose 留下半切换。
- 重复 apply 在 desired state 未变化时必须是审计可读的 no-op，不得重新加密相同 token、重置健康或改变 route revision。
- workflow 必须校验 `origin/master == origin/release == requested full SHA`、current release 完整 SHA，并读回 backend 及全部 AI generation worker 的相同完整 SHA/healthy；purpose 输入必须由上述 allowlist 校验，不能只校验字符格式。

### 14.3 Bug Batch Plan 与 dev 执行顺序

| 顺序 | 开发包 | 主要交付 | 进入下一步条件 |
| --- | --- | --- | --- |
| B1 | 删除冲突拓扑 | 删除 OpenAI/text wrapper、旧 Settings/UI heuristic/model alias/tests；全仓唯一正式入口 | 全仓引用扫描无旧类、18099、text wrapper 运行入口 |
| B2 | invocation identity + ledger | 冻结请求级 identity；spawn 前/后状态、PID、unknown/reconcile 正确收敛 | 多 brief、多 attempt、进程 spawn 异常和 crash-window 测试通过 |
| B3 | typed failure + route | client/server/candidate 共用 typed codes；只对可证明 pre-call 失败切换 | auth/quota/model/pre-call 与 started/unknown 路由矩阵通过 |
| B4 | host isolation + lifecycle | 精确 writable HOME/ledger；root-owned release-bound bridge runtime；独立 Python 3.11 与 cryptography 依赖；安装探针、key、迁移、发布探针和 bridge 全部以 `-E -s` 禁用 user-site/PYTHON 环境；agy 子进程环境不含 bridge/ledger secrets；发布与安装共享 host flock，安装只接受 `loaded+inactive+MainPID=0` 或显式首次 `not-found+MainPID=0`，前后复核；发布变更前校验 disabled/active 漂移，disabled skip、enabled restart/probe 失败恢复旧 runtime 和全部 unit 起始状态 | 专用 user 无 `/data` 权限仍可读取只读 runtime 并写 HOME ledger；WAL legacy ledger 按当前 env 指向判 authority，crash/retry 时执行 migrate/compare/verify，无 open/unknown 且 SQLite backup/readback 一致才原子切换；首次无任何 ledger 时创建 canonical 零行 service ledger，helper 与 env 切换间中断只接受同一 canonical 零行 staged ledger 后续接，env 已指向 service 后文件缺失则失败；systemd sandbox 实机 Schema probe；多 slot 第 N 个失败 rollback 测试通过 |
| B5 | capability/schema/health/security | 参数能力显式；完整 v2 Schema；分项健康；URL/model/token 校验 | current parser/gate、SSRF、健康衰减与 UI 测试通过 |
| B6 | guarded config/release | 全 generation routes 单事务 preview/apply/readback；完整 SHA 和 allowlist Gate | 漂移/半切换/no-op/rollback 自动测试通过 |
| B7 | slot-01 canary | 专用 OAuth、service probe、Provider 两行、route 读回和生产 Gateway 调用 | 第 14.5 节全部 Gate 通过后才允许最高优先级 |

开发不得并行修改同一 identity/ledger 状态机；若多人并行，B1、B4、B5 可独立 locked paths，B2+B3 必须由同一 merge owner 收口，B6 最后基于前述 typed contract 实现。任何阶段发现 process-started 事实不明，立即保持 unknown 并停止 canary，不通过新增 fallback 使测试变绿。

### 14.4 逐项 QA 验收标准

1. **请求身份**：一个 GenerationJob 含至少 3 个不同 brief、每个 brief 两次 realizer attempt、两个 Provider route item；所有不同 invocation ID 唯一，同一 invocation retry ID/hash 不变，不出现 409；篡改同一 ID 的 payload 必须 409。
2. **spawn 边界**：binary missing、permission denied、invalid cwd 为 `process_started=false` typed failed；PID 后 kill/连接断开/无法读回为 unknown；ledger 不得永久卡在 started 且无 PID/terminal/reconcile case。
3. **错误解析**：非 JSON stdout + auth stderr、quota stderr、invalid model stderr 均保留正确 typed code；成功才进入 envelope/schema parser；日志和 API 不回显 OAuth、prompt 或原始 stderr 敏感片段。
4. **route 安全**：busy、binary missing、可证明 auth/quota/model pre-call 进入下一候选；started、unknown、confirmed content/schema/quality failure 均不换 Provider。3.5 到 3.1 再到原 Provider 的每次转移都有独立 attempt 与原因。
5. **systemd**：专用 user 在 unit sandbox 内可写认证/profile/cache/ledger，只能从 root-owned、SHA-bound runtime 读取 bridge 代码；host 必须有独立 `/usr/bin/python3.11` 及其 `cryptography` 包，依赖探针、Fernet key、迁移 helper、发布探针和 unit 均以 `-E -s` 启动，不改变系统 Python 3.6；agy 正式调用和版本探针均不得继承 bearer token/ledger key；不能读取 `/data` release、Telegram Session、应用 secrets 或其他 slot HOME。slot 安装与普通发布持有同一 host flock；既存 unit 必须为 `LoadState=loaded, ActiveState=inactive, MainPID=0`，首次安装只额外允许 `LoadState=not-found, MainPID=0`，activating/deactivating/failed 一律拒绝并在 ledger 操作前后复核。legacy WAL ledger 以现有 env 的 `ANTIGRAVITY_LEDGER_PATH` 判 authority：legacy authoritative 时 destination 已存在必须 settled/integrity/count/hash 相等；首次 legacy 与 destination 都不存在时创建 canonical 零行 service ledger；若 helper 成功后、env 原子切换前中断，只允许结构完全匹配且 settled/integrity 正常的零行 staged ledger 续接；service authoritative 时 service ledger 缺失必须失败；无 env 的 crash retry 同样必须 compare，一致后才能重写 env。disabled/inactive 发布后仍 disabled/inactive；enabled 健康 unit 被重启；第 N 个 enabled slot 探针失败时恢复旧 runtime symlink，并按所有 enabled unit 的发布前 active/inactive 状态恢复。
6. **唯一拓扑**：生产代码、配置、UI 和测试不存在直接 backend CLI、OpenAI Antigravity text wrapper、18099 或 fuzzy model mapping；正式 bridge 未授权请求 401，公网不可达。
7. **参数和 Schema**：显式 temperature/max_tokens 预调用失败；planner v2 与全部 realizer mode 的成功 payload 通过 canonical parser/gate，缺 claims/reply/evidence 或多余非法字段失败；不能以 permissive schema 伪造成功。
8. **健康**：进程存在但 auth 过期、model 不可见、schema probe 失败、quota/cooldown 分别呈现非 healthy；probe 过期后健康衰减，端口 200 不构成 healthy。
9. **Provider 边界**：非批准 host/port、非冻结 model、空/错误 bridge token 创建和更新失败；旧 OpenAI Provider 不受影响。
10. **配置原子性**：preview 后任一 provider/route/default/SHA 漂移均 apply 零写；注入第 N 个 route 写失败时全部 route 回滚；相同 desired state 重复 apply 为 no-op；readback 精确保持原 Provider 后备顺序。
11. **发布**：全测试、前端构建、脚本静态检查通过；backend 和全部 generation worker 完整 SHA 一致且 healthy；不能用 CI success 代替 slot/Gateway 读回。
12. **生产 canary**：先从生产 backend 容器以显式选定但尚未进入 active route 的 Provider credentials，依次经正式 Gateway adapter 对 3.5 和 3.1 做真实结构化调用，校验 model/slot/request ID/usage/schema/ledger confirmed；通过后才执行 guarded route apply/readback，再跑一个按新 route 解析但不发 Telegram 的冻结 GenerationJob canary。Telegram 业务 E4 仍需独立 Action/Attempt/remote fact。

### 14.5 Release Gate 与回滚 Gate

以下条件必须全部为真，才允许把 Antigravity 写成生成 route 的最高优先级：

1. B1～B6 代码与专项 QA 已合并，相关 PRD、数据流和结构索引已由 dev 同步；候选 Git worktree clean。
2. `master == release == workflow candidate == current release == backend/all generation workers` 完整 SHA，容器全部 healthy。
3. 生产 `tgy-agy-01` 独立 OAuth 成功；unit `enabled/active`；18101 仅 Docker bridge 可达；分项 health 与两个固定模型 Schema probe 均通过。
4. 两行 Provider 已 guarded preview/apply/readback，内部 token 与 Google OAuth 严格分离；未出现重复 provider row。
5. 所有 generation purpose 在单事务 route revision 中读回为 `3.5 Flash Medium -> 3.1 Pro Low -> 原顺序 Provider`，semantic review 与非本批 purpose 零变化。
6. 生产 Gateway 两模型真实结构化调用和无 Telegram canary 均 confirmed；ledger 无无主 started/unknown，旧 Provider 可用性读回正常。
7. rollback revision/fingerprint 已预生成：停止新请求后先排空或 hold started/unknown，再以前向 revision 恢复原 generation routes；不得删历史 attempt/ledger 或重放 Provider/Gateway。

任一 Gate 失败，生产状态保持 `blocked/unproven`，不创建/启用 Antigravity route；若 apply 后 canary 失败，按冻结 rollback revision 前向恢复旧 route。CI、部署、进程、端口、Provider health 和 Gateway confirmed 是不同证据层，任何一层都不得替代 Telegram typed remote fact。

### 14.6 Product Design Complete resync 自检与开发交接

| 维度 | resync 状态 | 开发交接结论 |
| --- | --- | --- |
| 用户优先级与 slot 范围 | complete | slot-01；3.5 第一、3.1 第二、旧 Provider 后备 |
| 重复实现取舍 | complete | 删除 OpenAI/text wrapper，不保留兼容路径 |
| invocation identity/幂等/unknown | complete | 已冻结调用级身份和 route/reconcile 边界 |
| typed error/failover | complete | 只允许有证据的 pre-call failure 切换 |
| host/systemd/OAuth | complete | 专用 HOME 可写最小集、disabled 生命周期和实机验收已定义 |
| Schema/参数/健康/安全 | complete | canonical v2、显式 capability、分项健康和内网校验已定义 |
| 配置/并发/一致性 | complete | 全 generation route 单事务、漂移零写、重复 apply no-op |
| QA/发布/回滚/E4 | complete | 自动 QA、生产 canary、前向回滚与证据分层已闭合 |

本次 `resync_status=complete`；B1～B6 实现和项目结构索引已同步，`implementation_status=complete`。生产基线与独立 QA 先后暴露出 `/data` traverse/ledger、发布前 unit 漂移门禁、既有 runtime 元数据、legacy WAL ledger、空账本 authority crash-window、host Python 3.6、CLI 子进程 secret 继承、Python user-site 注入和多 slot rollback 缺口；候选已逐项修复并转成行为测试。首次完整 Actions 随后在服务器部署前暴露第 14.8 节的 stale migration-head assertions 与缺失标准库导入；quick fix 已完成本地定向/聚焦/相邻回归和独立 QA，未发现 P0/P1/P2，Product Design Complete 复核确认只同步验收与缺失导入、不改变迁移/route/Provider/生产配置，故恢复 `qa_status=pass`、`product_status=accepted`。blank PostgreSQL、Linux systemd、OAuth、agy、Gateway 与生产调用仍由下一次完整 Actions/生产 Gate 提供证据；`production_status` 在第 14.5 节真实读回前继续保持 `blocked`。

### 14.7 Dev Handoff：代码入口和修改责任

| 开发责任 | current 入口/目标文件 | 冻结交付 |
| --- | --- | --- |
| 删除冲突拓扑 | `backend/app/services/antigravity_cli_bridge.py`、`backend/scripts/antigravity_http_bridge.py`、`backend/tests/test_antigravity_cli_bridge.py`、`backend/app/config.py`、`frontend/src/app/views/AISettingsView.tsx`、旧 model alias | 删除旧文件和专属配置/启发式；保留正式 `antigravity_cli` Provider UI |
| 请求身份 | `backend/app/services/task_center/ai_structured_provider_runtime.py`、各 planner/realizer 调用点 | 持久化调用级 identity；多 brief/attempt/provider 唯一，retry/reconcile 稳定 |
| bridge/ledger 状态机 | `backend/scripts/antigravity_provider_server.py`、`backend/scripts/antigravity_provider_ledger.py` | spawn 边界、PID、typed terminal、unknown 与 request readback 闭合 |
| client/route 错误 | `backend/app/services/antigravity_provider_client.py`、`backend/app/services/task_center/ai_provider_candidate_runtime.py`、`backend/app/ai_gateway.py` | 共用 typed-code；pre-call 才切换；started/unknown hold |
| Schema/capability/health | `backend/app/services/task_center/antigravity_schemas.py`、Provider create/update/check API 与前端表单 | canonical v2 Schema、参数显式、分项 health、内网 URL/model/token 校验 |
| host 生命周期 | `deploy/antigravity-slot-release-plan.sh`、`deploy/check-antigravity-slot-install-state.sh`、`deploy/install-antigravity-provider-runtime.sh`、`deploy/migrate-antigravity-provider-ledger.py`、`deploy/install-antigravity-provider-slot.sh`、`deploy/restart-antigravity-provider-slots.sh`、`deploy/server-install-release.sh`、`docker-compose.server.yml` | 共享 host lock、发布变更前 unit 状态门禁、安装 exact inactive/PID 门禁、root-owned SHA runtime、authority-aware WAL-safe HOME ledger 原子迁移、Python/CLI secret 隔离、enabled-only restart+probe+全 unit rollback、容器到 host 内网边界 |
| guarded 配置/发布 | `backend/scripts/configure_antigravity_providers.py`、`backend/scripts/configure_ai_provider_failover.py`、`.github/workflows/production-ai-provider-failover.yml` | 单事务全 route、完整 SHA/fingerprint/allowlist/no-op/readback/rollback |
| QA 与索引 | Antigravity provider/bridge/route/provider API/deploy script 测试；`docs/00-index/project-structure-index.md` | 覆盖第 14.4 节全部矩阵；由 dev 同步删除项与 current 唯一入口 |

实现顺序按 B1～B7；B2+B3 共享 request/terminal 状态机，必须由同一 merge owner 合并。dev 不得把旧 wrapper 改名后保留，也不得通过 catch-all、mock success、自动重放或放宽 Schema 绕过失败。

### 14.8 CI Quick Fix：首次完整分片暴露的测试同步与导入缺口

正式发布 run `33327782377` 在服务器部署前被质量门禁阻断，生产没有写入。失败归为同一小批次：迁移 `0182_ai_provider_request_id` 已成为唯一 Alembic head，但两个全仓/blank-PostgreSQL 验收仍把 head 写死为 `0181_runtime_storage_clone_merge`；同时 `ai_provider_candidate_runtime.py` 的 candidate request ID 新增 SHA-256 计算却遗漏 `hashlib` 导入，导致两个真实 AI 活群 PostgreSQL 流程在生成前抛出 `NameError`。

Mini Bug Card 冻结为：只补缺失标准库导入，并把两个陈旧 head 断言同步到已存在且已由 Alembic 验证的 `0182`；不得改写迁移 revision/down_revision、业务请求身份算法、route、Provider 或生产配置。定向验收必须覆盖 merge integrity、blank PostgreSQL migration、candidate request identity 和两个失败的 AI 活群 workflow；随后重新跑 Provider 聚焦回归、独立 QA 和完整 Actions。任一测试仍失败则保持 `production_status=blocked`，不得手动绕过部署。

### 14.9 Production Quick Fix：Docker host gateway 地址漂移

发布 `034664bbaa3804d14eb6b473b51bb1d575964e3e`、slot-01 OAuth、原生双模型 POC 和 host bridge 双模型探针均成功后，正式 Provider check 得到 `all_healthy=false`：backend 容器把 `host.docker.internal:host-gateway` 解析为默认 `docker0` gateway `172.17.0.1`，而 slot service 按 `infra_default` 实际 gateway `172.19.0.1` 监听，导致两个 Provider 都以 `antigravity_bridge_unreachable_pre_call` 失败。由于直接访问 `172.19.0.1:18101` 已到达 bridge 并返回预期 401，该故障冻结为容器地址注入错误，不归因于 OAuth、模型、token 或 bridge 进程。

Mini Bug Card 冻结为：`deploy/docker-env.sh` 在任何 Compose mutation 前从 `${INFRA_NETWORK_NAME:-infra_default}` 动态读取唯一 IPv4 gateway，导出 `ANTIGRAVITY_DOCKER_GATEWAY`；`docker-compose.server.yml` 只允许 backend 与三个 AI generation worker 将 `host.docker.internal` 映射到该精确值。slot install/restart 使用同一网络名和读取算法。不得把 bridge 改为公网/`0.0.0.0` 监听，不得放宽 Antigravity URL SSRF allowlist，不得手工修改运行中容器 `/etc/hosts` 充当完成。

定向 QA 必须证明：裸 `host-gateway` 已从这四个容器删除；网络不存在、gateway 为空或非 IPv4 时发布在 Compose 前失败；自定义 `INFRA_NETWORK_NAME` 被同一算法使用；现有 slot restart/rollback 与 Compose 合同回归通过。修复重新走 dev -> QA -> product accepted -> master/release 同 SHA -> 完整 Actions；生产验收必须重新取得 backend/三个 worker 的目标 DNS、Provider `all_healthy=true`、双模型 Gateway confirmed、六条 generation route 原子 readback 和无 open/unknown ledger。此前绿色 workflow 只证明命令执行成功，不覆盖其 JSON 业务结果。

本轮 dev 后 QA 已取得 147 条 Antigravity、slot lifecycle、release/worker 合同回归通过，另有 Compose config、Bash 语法和 diff check 通过；Product Design Complete 复核确认该修复不改变 Provider 身份、模型顺序、生成 Schema、route 或 unknown 语义，`qa_status=pass`、`product_status=accepted`。新 SHA 的完整 Actions、容器地址读回和本节生产调用尚未完成，故 `production_status=blocked` 不变。

### 14.10 Production Quick Fix：冻结 slot 未进入 Antigravity draft Schema

`21ab17a2642737bdb361df58c24907802fb40105` 已完整发布，backend 与三个 AI generation worker 通过 `infra_default` 精确 gateway 到达 slot，双 Provider health、双模型结构化 Gateway 探针及六条 generation route readback 均通过。route apply 后真实 `group_ai_chat` GenerationJob 进入 Provider，但 `AiProviderAttempt` 仍终结为 `RuntimeError`，Action 明确记录 `fixed_slot_contract=slot_mapping`；对应 bridge ledger 为 confirmed，解密后的安全结构检查显示 `drafts[0]` 只有 `content`，没有业务冻结的 `slot_id`。因此首个失败边界是 Gateway draft Schema 与既有固定 slot parser 不一致，不是 OAuth、网络、配额、CLI 或 Telegram。

Mini Bug Card 冻结为：Antigravity legacy draft 调用必须从当前 prompt 的 `generation_slots`/固定 slot 段解析预期 slot IDs，并据此生成调用级 JSON Schema；每个 draft 必须包含 `content` 与非空 `slot_id`，`slot_id` 只能取冻结集合，draft 数量必须等于冻结 slot 数。无固定 slot 的旧调用继续使用原通用 draft Schema。返回后仍由既有顺序精确匹配闸门校验，不得自动补 slot、按位置猜测、放宽 parser 或把 malformed 结果当成功。

定向 QA 必须覆盖：单/多固定 slot 的 required/enum/minItems/maxItems；无固定 slot 的旧 Schema 不变；Provider 即使返回错误顺序、重复或缺失 slot 仍显式失败；真实生产 canary 必须形成 `AiProviderAttempt=success`、非空 token usage、GenerationJob candidate/ready 或后续合法质量终态，且 bridge ledger 无无主 `claimed|started|unknown`。Telegram Action/ExecutionAttempt/typed remote fact 继续作为独立 E4，不以 Provider confirmed 代替。

## 15. 多模型业务回复 POC

2026-08-30 已使用独立 `slot-01` 对 `gemini-3.5-flash-medium`、`gemini-3.6-flash-medium`、`gemini-3.7-flash-medium` 执行固定 AI 活群与频道评论合成样本。六个候选均形成终态；样本内跨场景质量为 3.6 最稳，3.7 活群质量和时延最佳但评论被当前地点/敏感交易 cleaner 拒绝，并触发事实边界硬失败。完整输入、候选、评分、运行异常与证据边界见 `docs/05-implementation/antigravity-cli-model-reply-poc-20260830.md`。

## 16. 成人方向 route-shaped POC

2026-08-30 又以同一独立 `slot-01`、三个 Medium 模型执行 7 个成人方向合成场景，共 21 个正式调用，覆盖 `adult_visual`、`adult_product`、`adult_service_inquiry`、`adult_service_sensory`、成人双关设计缺口、PII/性交易执行静默和弱词不得强转成人 route。

样本内，3.5 是唯一 7/7 通过结构与独立事实/安全硬门的模型；3.7 在多数正向文案中自然度最高，但 sensory 连续拒答且没有 `structured_output`；3.6 全部结构成功，但 visual 新增了证据未支持的“腿型”。因此当前单一基础候选为 3.5，3.7 只能作为经过 mode 路由后的风格候选，不能承接 sensory。成人双关仍不是当前 MessageBrief v2 正式 mode，PII/精确地址/代约意图必须在 Provider 前静默。

完整输入边界、候选、当前代码闸、盲评、耗时和未证明项见 `docs/05-implementation/antigravity-cli-adult-routes-poc-20260830.md`。本结果仍不证明 Gateway、远程 bridge、五账号、Telegram 发送或生产 E4。

同日补测 `gemini-3.1-flash-medium`：authenticated `agy models` 不提供 3.1 Flash；带 effort 和去掉 effort 的两次探针均在模型调用前失败，后者明确为 model not recognized，且两次都是 zero turn/usage。当前只有 3.1 Pro High/Low，不能替代为 3.1 Flash 结果。

用户明确改为 `gemini-3.1-pro-low` 后，按同一 7 场景和 Schema 补测：7/7 形成结构化终态，当前四个正式成人 v2 mode 为 4/4 parser/gate 通过，sensory 可用，两个安全边界均静默；成人双关的“最费腰”因新增源证据没有的身体效果被独立事实审查拒绝。其平均 CLI duration 为 11.22 秒，是四模型最慢，因此只形成可行 Pro 候选，不改变当前 3.5 单模型基础候选判断。
