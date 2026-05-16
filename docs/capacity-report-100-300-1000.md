# TG 运营管理平台容量报告：100 / 300 / 1000 账号

> 生成时间：2026-05-16T10:01:21.198737+00:00
> 口径：本报告由 mock gateway 容量模型生成，用于固化压测参数和风险边界；不是线上 Telegram API 实测结论。

## 结论

- 100 账号 / 5 任务：按当前角色拆分和连接池预算，可以作为试运行容量。
- 300 账号 / 10 任务：需要 4 个 Dispatcher worker，并保持 Redis token bucket 与账号 in-flight 开启。
- 1000 账号 / 20-30 任务：需要 8 个 Dispatcher worker 起步，且必须用真实 PostgreSQL / Redis 压测复核 TG 延迟、限流和 backlog。
- 重复发送验收口径为 `duplicate_sends = 0`；如果真实压测出现非 0，必须先修 claim / lease / account lock。

## 明细

| 场景 | Gateway | 生成 Action | 处理 Action | 吞吐/分钟 | Backlog | Oldest pending P95 | unknown_after_send | 重复发送 | Dispatcher | 并发 | Claim limit | DB pool | 瓶颈 | 单机账号边界 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| 100:5 | fast | 522 | 522 | 17.4 | 0 | 0.0s | 0 | 0 | 2 | 5 | 50 | 20+20 | generated_demand | 900 |
| 100:5 | slow | 522 | 522 | 17.4 | 0 | 0.0s | 0 | 0 | 2 | 5 | 50 | 20+20 | generated_demand | 250 |
| 100:5 | flood_wait | 522 | 522 | 17.4 | 0 | 0.0s | 0 | 0 | 2 | 5 | 50 | 20+20 | generated_demand | 375 |
| 100:5 | slowmode | 522 | 522 | 17.4 | 0 | 0.0s | 0 | 0 | 2 | 5 | 50 | 20+20 | generated_demand | 500 |
| 100:5 | unknown_after_send | 522 | 522 | 17.4 | 0 | 0.0s | 5 | 0 | 2 | 5 | 50 | 20+20 | generated_demand | 400 |
| 300:10 | fast | 1044 | 1044 | 34.8 | 0 | 0.0s | 0 | 0 | 4 | 7 | 56 | 20+20 | generated_demand | 900 |
| 300:10 | slow | 1044 | 1044 | 34.8 | 0 | 0.0s | 1 | 0 | 4 | 7 | 56 | 20+20 | generated_demand | 700 |
| 300:10 | flood_wait | 1044 | 1044 | 34.8 | 0 | 0.0s | 1 | 0 | 4 | 7 | 56 | 20+20 | generated_demand | 900 |
| 300:10 | slowmode | 1044 | 1044 | 34.8 | 0 | 0.0s | 1 | 0 | 4 | 7 | 56 | 20+20 | generated_demand | 900 |
| 300:10 | unknown_after_send | 1044 | 1044 | 34.8 | 0 | 0.0s | 10 | 0 | 4 | 7 | 56 | 20+20 | generated_demand | 900 |
| 1000:30 | fast | 3132 | 3132 | 104.4 | 0 | 0.0s | 0 | 0 | 8 | 11 | 176 | 20+20 | generated_demand | 900 |
| 1000:30 | slow | 3132 | 3132 | 104.4 | 0 | 0.0s | 3 | 0 | 8 | 11 | 176 | 20+20 | generated_demand | 900 |
| 1000:30 | flood_wait | 3132 | 3132 | 104.4 | 0 | 0.0s | 3 | 0 | 8 | 11 | 176 | 20+20 | generated_demand | 900 |
| 1000:30 | slowmode | 3132 | 3132 | 104.4 | 0 | 0.0s | 3 | 0 | 8 | 11 | 176 | 20+20 | generated_demand | 900 |
| 1000:30 | unknown_after_send | 3132 | 3132 | 104.4 | 0 | 0.0s | 31 | 0 | 8 | 11 | 176 | 20+20 | generated_demand | 900 |

## 验收要求

- PostgreSQL-only：最终验收必须使用 `postgresql+psycopg://...`，不能用 SQLite 代替。
- Redis 打开：目标场景必须开启 token bucket 和 account in-flight；Redis 不可用场景必须 fail-closed。
- worker 异常退出：应通过 claim/recovery 看到 pending 恢复，不能重复发送。
- DB 紧张场景：Dispatcher 并发必须被连接池预算压住，不能无限调大。
- 真实任务验证：至少创建 AI 活跃、转发监听、频道浏览、频道点赞、频道评论各 1 个任务，确认 Action 创建、claim、执行和 metrics 快照均有记录。

## 后续实测命令

```bash
cd /Users/xida/PycharmProjects/tg-yunying/backend
APP_ENV=test \
DATABASE_URL='postgresql+psycopg://...' \
TEST_DATABASE_URL='postgresql+psycopg://...' \
REDIS_URL='redis://...' \
TG_GATEWAY_MODE=mock \
.venv/bin/python scripts/run_capacity_benchmark.py \
  --scenario 100:5 --scenario 300:10 --scenario 1000:30 \
  --gateway-mode fast,slow,flood_wait,slowmode,unknown_after_send \
  --output ../reports/capacity/latest.json \
  --markdown ../docs/capacity-report-100-300-1000.md
```
