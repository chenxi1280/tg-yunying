# 账号在线保活全量处理设计

## 1. 背景与问题

生产环境的应上线账号池已达到数百个账号。外层 `account-online` worker 虽然配置了较大的单轮 drain 数量，在线状态服务内部仍把每轮探测数量截断为 20；Telegram 健康探测并发也固定为 4。结果是 worker 心跳正常、主机总资源看似仍有余量，但真实探测吞吐追不上账号池规模，账号在得到下一次真实探测前先超过 `stale_after`，形成大量假离线并阻塞群活跃覆盖。

本设计采用方案 A：取消隐藏的账号处理上限，让全部应上线账号进入保活处理；分页大小和 Telegram 网络探测并发保留为显式容量参数，但不得被解释为账号上线名额限制。

## 2. 产品口径

- `desired_online=true` 的账号全部进入保活状态机，不设置账号总量准入上限，也不按前 N 个账号截断。
- 分页是调度机制。单页未覆盖的账号必须由后续页或后续 drain 继续处理，不能永久饥饿。
- Telegram 探测并发是网络容量控制。它只决定同一时刻发起多少个真实探测，不改变应处理账号集合。
- `login_required`、session 失效、`AuthKeyDuplicated` 等真实登录问题不得伪装成 online；系统保留失败状态、原因和下次处理时间。
- 不新增静默降级、假成功或自动绕过 Telegram 风控的路径。

## 3. 配置与批次设计

新增显式配置 `ACCOUNT_ONLINE_PROBE_CONCURRENCY`，生产默认值为 32；新增健康探测专用 `ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS`，生产默认值为 30 秒，避免通用 Telegram 操作 300 秒超时把整个账号池的探测周期拖过 stale 窗口。两项配置必须为正数；非法值直接使服务启动失败并暴露配置错误，不静默回退。

`ACCOUNT_ONLINE_DRAIN_LIMIT` 继续表示单次 drain 的分页数量，生产默认值调整为 1000，以覆盖当前账号池并为增长留出空间。在线状态服务必须完整使用调用方传入的 limit，不再执行内部 `min(..., 20)` 截断。该数值不是账号上线总量上限；账号池超过单页时，调度器继续按到期时间分页处理。

当本轮真实 probe 数量达到 drain limit 时，本轮继续延后批量 stale 标记，避免尚未轮到探测的健康账号被误判离线。

## 4. 并发探测与线程边界

数据库 Session、ORM 对象读取和在线状态落库全部保留在 worker 主线程：

1. 主线程分页读取到期在线状态、账号和凭证，生成不可变探测任务。
2. 线程池只执行 `TelegramGateway.check_account_health` 网络调用，返回不可变结果，不携带 ORM 对象；健康探测使用独立的 30 秒超时，不继承普通 Telegram 业务操作的 300 秒超时。
3. 探测结果必须按完成顺序流式返回；主线程每收到一个结果就立即更新并提交 `online`、`login_required`、`blocked`、`failure_detail`、`last_probe_at`、`stale_after_at` 和 `next_probe_at`，不得等待整页全部网络调用完成后才集中落库。

线程池实际并发取 `min(ACCOUNT_ONLINE_PROBE_CONCURRENCY, 本页任务数)`。不得在子线程读取或提交数据库 Session。

## 5. 状态与失败处理

- 探测成功：刷新 `online`、`last_seen_at`、`last_probe_at` 和 `stale_after_at`，并释放由账号离线产生的覆盖阻塞。
- 网络超时或代理失败：写入 `blocked`、原始失败详情和明确的 `next_probe_at`，按现有 backoff 重试。
- 登录或 session 失败：写入 `login_required` 和原始失败详情，保留 `desired_online` 投影，等待真实修复或重新登录。
- 只有真实探测成功才续期在线状态；reconcile 和 worker 心跳不得替代真实探测。
- 批次异常必须显式记录 worker 错误，不吞掉未处理账号。

## 6. 验收口径

### 6.1 自动化验收

- 传入 drain limit 500 时，服务实际向探测层传入 500，而不是 20。
- 显式配置并发 N 时，探测线程池最多同时执行 N 个网络探测。
- 快速探测结果必须在慢探测仍未完成时先返回并落库；单个慢探测使用独立的 30 秒超时。
- 数据库读取与状态落库仍在主线程，子线程只执行 Gateway 健康检查。
- 批次打满 limit 时不批量执行 stale 标记。
- 非法并发配置导致显式配置错误。

### 6.2 生产 E4 验收

- GitHub Actions 按 `master -> release -> Deploy Production` 完成，线上版本与目标提交一致。
- `account-online` worker 心跳正常，持续无 drain 级异常。
- 所有 `desired_online=true` 账号均有明确状态且无 missing；除按策略尚未到期的 `login_required` 外，所有到期、可探测账号在 15 分钟窗口内完成一轮真实探测。
- 分别统计 `online`、`stale`、`login_required`、`blocked` 及原始失败原因，不能用 worker 存活替代账号覆盖证明。
- 四个群的每日发言覆盖持续增长；剩余未完成项按真实 `login_required`、权限、Telegram/代理失败等原因分层，不能再由内部 20 个账号截断造成。

## 7. 发布与回滚

发布前运行定向单元测试、相关在线状态回归和配置检查。发布沿用标准 GitHub Actions 流程。若显式并发 32 造成 Telegram、代理或主机压力异常，只回调 `ACCOUNT_ONLINE_PROBE_CONCURRENCY` 并重新部署；若健康探测出现正常网络抖动，可显式调整 `ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS`，但不得恢复 300 秒通用超时、隐藏账号截断或应上线账号总量限制。
